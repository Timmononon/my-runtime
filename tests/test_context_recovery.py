"""Sustained compaction through real checkpoints and offline run recovery."""
import copy

import pytest

from aster.domain import Message, ModelTurn, ToolCall, ToolResult, ToolSpec, to_dict
from aster.engine import Engine, FakeModel, RunConfig
from aster.engine.context import ContextManager, estimate_tokens
from aster.providers.deepseek import encode_messages
from aster.state import Store
from aster.tools.registry import Registry

CONSTRAINT = "Keep original measurements unchanged at D:/data/original.csv."
SUMMARY = "Goal: analyze\nConstraints: preserve\nCompleted evidence: reviewed\nOpen work: continue\nReferences: archive"


class LongObservation:
    spec = ToolSpec("observation", "Read numbered synthetic evidence", {
        "type": "object", "properties": {"number": {"type": "integer"}},
        "required": ["number"], "additionalProperties": False,
    }, risk="read", replay_safe=True)

    def __init__(self):
        self.executed = []

    async def execute(self, arguments, context):
        self.executed.append(arguments["number"])
        return ToolResult(f"observation {arguments['number']} " + "X" * 10000)


class SustainedModel:
    model = "offline-context-fixture"

    def __init__(self, start, rounds, *, fail=False):
        self.start, self.rounds, self.fail = start, rounds, fail
        self.requests = 0
        self.summary_sizes = []

    async def generate(self, messages, tools, **kwargs):
        encode_messages(messages)
        assert CONSTRAINT in "\n".join(message.content for message in messages)
        summaries = [message for message in messages if message.metadata.get("compaction_summary")]
        assert len(summaries) <= 1
        if summaries:
            self.summary_sizes.append(len(summaries[0].content))
            assert summaries[0].metadata["protected_requests"].count(CONSTRAINT) == 1
        self.requests += 1
        if self.requests <= self.rounds:
            number = self.start + self.requests
            return ModelTurn(tool_calls=[ToolCall(f"read-{number}", "observation", {"number": number})],
                             finish_reason="tool_calls")
        if self.fail:
            raise RuntimeError("Synthetic stopped worker; recover from the persisted checkpoint")
        return ModelTurn("Analysis complete")


async def test_sustained_local_compaction_across_runs_disk_reopen_and_recovery(tmp_path):
    path = tmp_path / "runtime.db"
    store = Store(path)
    sid = store.create_session(tmp_path)["id"]
    tool = LongObservation()
    config = RunConfig(max_iterations=120, max_tool_calls=120, semantic_compaction=False,
                       file_restore_max_files=0)
    models = []
    try:
        first = SustainedModel(0, 60)
        models.append(first)
        assert (await Engine(first, Registry([tool]), store, config=config).run(sid, CONSTRAINT)).status == "completed"
        prefix = copy.deepcopy(to_dict(store.history(sid)))
        second = SustainedModel(60, 60, fail=True)
        models.append(second)
        interrupted = await Engine(second, Registry([tool]), store, config=config).run(sid, "Continue analysis")
        assert interrupted.status == "failed"
        assert to_dict(store.history(sid)[:len(prefix)]) == prefix
        durable = copy.deepcopy(to_dict(store.history(sid)))
        store.close()
        store = Store(path)  # Recovery must use persisted constraints, not the old manager.
        third = SustainedModel(120, 30)
        models.append(third)
        recovered = await Engine(third, Registry([tool]), store, config=config).recover(interrupted.run_id)
        assert recovered.status == "completed", recovered.error
        assert to_dict(store.history(sid)[:len(durable)]) == durable
        fourth = SustainedModel(150, 60)
        models.append(fourth)
        assert (await Engine(fourth, Registry([tool]), store, config=config).run(sid, "Continue analysis")).status == "completed"
        archive = store.history(sid)
        assert tool.executed == list(range(1, 211))
        assert len([message for message in archive if message.role == "tool"]) == 210
        assert all(len(message.content) > 10000 for message in archive if message.role == "tool")
        assert not any(message.metadata.get("compaction_summary") for message in archive)
        assert to_dict(archive[:len(durable)]) == durable
        assert len(store.active_history(sid)) < len(archive)
        assert sum(event["type"] == "context.compacted" for event in store.events(session_id=sid)) >= 8
        # Bound state size across 210 rounds; the old implementation fails around round 46.
        sizes = [size for model in models for size in model.summary_sizes]
        assert sizes and max(sizes) < 18000
    finally:
        store.close()


async def test_local_and_semantic_reduction_preserve_large_valid_user_requirements():
    # The previous local-excerpt ceiling rejected this valid original request,
    # even though both a local state and a semantic state fit the model window.
    request = "Original requirement " + "R" * 9000
    messages = [Message("user", request)]
    for number in range(6):
        messages.extend([Message("assistant", tool_calls=[ToolCall(str(number), "read", {})]),
                         Message("tool", "Z" * 2500, tool_call_id=str(number))])
    original = copy.deepcopy(messages)
    manager = ContextManager(context_tokens=18000, reserve_tokens=1000, retain_turns=1)
    local = manager.compact(messages, [], "", force=True)
    assert request in local[0].content
    assert estimate_tokens(local) <= manager.landing_threshold()
    assert messages == original
    calls = []

    async def summarize(source):
        calls.append(source)
        return ModelTurn(SUMMARY)

    shaped = await manager.compact_with_summary(messages, [], "", summarize, force=True)
    assert calls and shaped[0].metadata["semantic"] is True
    assert request in shaped[0].content
    assert estimate_tokens(shaped) <= manager.landing_threshold()
    assert messages == original


@pytest.mark.parametrize("semantic", [False, True])
async def test_original_requests_are_deduplicated_separately_from_latest_state(semantic):
    manager = ContextManager(context_tokens=18000, reserve_tokens=1000, retain_turns=1)
    messages = [Message("user", CONSTRAINT), Message("user", CONSTRAINT)]
    summaries = []

    async def summarize(source):
        return ModelTurn(SUMMARY)

    for number in range(40):
        messages.extend([Message("assistant", tool_calls=[ToolCall(str(number), "read", {})]),
                         Message("tool", "E" * 3000, tool_call_id=str(number))])
        if number % 4 == 3:
            messages = (await manager.compact_with_summary(messages, [], "", summarize, force=True)
                        if semantic else manager.compact(messages, [], "", force=True))
            summaries.append(messages[0])
            # Reconstructing the manager models a checkpoint reload, preserving
            # the mechanical ledger while dropping any in-memory state.
            manager = ContextManager(context_tokens=18000, reserve_tokens=1000, retain_turns=1)
    assert all(message.metadata["protected_requests"] == [CONSTRAINT] for message in summaries)
    assert all(message.content.count("Original user requests (verbatim):") == 1 for message in summaries)
    assert max(len(message.content) for message in summaries) < 6000


def test_repeated_user_direction_retains_latest_occurrence_order():
    old, reversal = "Use CSV output", "Use JSON output instead of CSV"
    messages = [Message("user", old), Message("user", reversal), Message("user", old)]
    manager = ContextManager()
    assert manager._ledger(messages)["protected_requests"] == [reversal, old]
    # A legacy summary's stale order cannot supersede the newest raw user turn.
    manager.protect_requests([old, reversal, old], from_archive=True)
    stale = Message("user", "previous state", metadata={"compaction_summary": True,
                    "protected_requests": [old, reversal]})
    assert manager._ledger([stale, Message("user", reversal)])["protected_requests"] == [reversal, old]
    manager.protect_requests([reversal])  # Newly drained steering/input remains latest.
    assert manager._ledger([stale])["protected_requests"] == [old, reversal]


async def test_legacy_recursive_checkpoint_reloads_constraints_in_raw_archive_order(tmp_path):
    store = Store(tmp_path / "runtime.db")
    old, reversal = "Use CSV output", "Use JSON output instead of CSV"
    try:
        sid = store.create_session(tmp_path)["id"]
        prior, _ = store.create_run(sid)
        raw = [Message("user", old), Message("user", reversal), Message("user", old)]
        for number in range(10):
            raw.extend([Message("assistant", tool_calls=[ToolCall(str(number), "read", {})]),
                        Message("tool", "E" * 1000, tool_call_id=str(number))])
        for message in raw:
            store.append_message(sid, prior["id"], message)
        store.set_run_status(prior["id"], "completed")
        original = copy.deepcopy(to_dict(store.history(sid)))
        legacy = Message("user", "Original user requests and prior state summaries (verbatim):\n" + "stale state " * 3000,
                         metadata={"compaction_summary": True, "source": "runtime"})
        store.checkpoint(sid, prior["id"], [legacy, *raw[-4:]])
        store.close()
        store = Store(tmp_path / "runtime.db")
        model = FakeModel([ModelTurn("done")])
        result = await Engine(model, Registry(), store, config=RunConfig(
            context_tokens=10000, reserve_tokens=1000, retained_context_rounds=1,
            semantic_compaction=False, file_restore_max_files=0)).run(sid, "Continue")
        assert result.status == "completed", result.error
        summary = model.calls[0]["messages"][0]
        assert summary.metadata["protected_requests"] == [reversal, old, "Continue"]
        assert summary.content.index(reversal) < summary.content.index(old)
        assert len(summary.content) < 6000
        assert to_dict(store.history(sid)[:len(original)]) == original
    finally:
        store.close()
