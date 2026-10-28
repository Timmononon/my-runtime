"""Compaction configuration and continuity through actual runtime admission."""

import pytest
from test_provider_profiles import SECRET, service

from aster.domain import Message, ModelTurn, ToolCall, ToolResult, Usage, to_dict
from aster.engine import Engine, FakeModel, RunConfig
from aster.engine.compaction import calibration_fingerprint
from aster.providers.deepseek import ModelCapabilities, ProviderError
from aster.state import Store
from aster.state.compaction import compaction_source_digest
from aster.tools.registry import Registry


@pytest.mark.parametrize("explicit,expected", [(None, 1_000_000), (80_000, 80_000), (2_000_000, 1_000_000)])
async def test_catalogue_context_replaces_seed_but_respects_operator_cap(tmp_path, monkeypatch, explicit, expected):
    class CatalogueModel:
        def __init__(self, key, **options):
            self.model = options["model"]
            self.max_output_tokens = options["max_output_tokens"]

        async def refresh_capabilities(self, **_):
            return {self.model: ModelCapabilities(self.model, 1_000_000, 16_384, ("text",), ("high",), "fixture", {})}

        async def generate(self, *_, **__):
            return ModelTurn("catalogue admitted offline")

        async def aclose(self):
            pass

    monkeypatch.setattr("aster.service.coordinator.DeepSeekResponsesClient", CatalogueModel)
    async with service(tmp_path, fake=False) as (coordinator, _, vault):
        config = "model: catalogue-fixture\nmax_output_tokens: 12000\n"
        if explicit is not None:
            config += f"runtime:\n  context_tokens: {explicit}\n"
        (tmp_path / "config.yaml").write_text(config, encoding="utf-8")
        vault.set_password("aster", "deepseek", SECRET)
        sid = coordinator.create_session(tmp_path)["id"]
        run = await coordinator.submit(sid, "Inspect context configuration")
        result = await coordinator.wait(run["id"], timeout=3)
        assert result.status == "completed", result.error
        saved = coordinator.store.get_run(run["id"])["config"]
        assert saved["context_tokens"] == expected
        assert saved["reserve_tokens"] == 12000


@pytest.mark.parametrize("changed_identity", [False, True])
async def test_calibration_survives_disk_reopen_only_for_same_provider_identity(tmp_path, changed_identity):
    path = tmp_path / "runtime.db"
    config = RunConfig(context_tokens=20000, reserve_tokens=1000, semantic_compaction=False)
    store = Store(path)
    try:
        sid = store.create_session(tmp_path)["id"]
        first = FakeModel([ModelTurn("initial evidence", usage=Usage(input_tokens=7000, output_tokens=10))])
        first.model, first.identity = "stable-model", {"provider": "fixture", "revision": 1}
        assert (await Engine(first, Registry(), store, config=config).run(sid, "Keep the source intact")).status == "completed"
        previous = store.get_checkpoint(sid)["metadata"]
        assert previous["overhead_tokens"] > 6000
        store.close()
        store = Store(path)
        second = FakeModel([ModelTurn("continued without measured usage")])
        second.model = "stable-model"
        second.identity = {"provider": "fixture", "revision": 2 if changed_identity else 1}
        result = await Engine(second, Registry(), store, config=config).run(sid, "Continue")
        assert result.status == "completed", result.error
        current = store.get_checkpoint(sid)["metadata"]
        assert current["overhead_tokens"] == (0 if changed_identity else previous["overhead_tokens"])
        assert (current["calibration_fingerprint"] == previous["calibration_fingerprint"]) is not changed_identity
    finally:
        store.close()


async def test_same_run_recovery_preserves_reactive_latch_and_does_not_retry_overflow(tmp_path):
    store = Store(tmp_path / "runtime.db")
    try:
        config = RunConfig(context_tokens=20000, reserve_tokens=1000, semantic_compaction=False)
        sid = store.create_session(tmp_path)["id"]
        model = FakeModel([ProviderError("synthetic overflow", category="context_length_exceeded")])
        engine = Engine(model, Registry(), store, config=config)
        run, _ = store.create_run(sid, config={**to_dict(config), "_runtime_identity": engine._runtime_identity()})
        messages = [Message("user", "A task recovered after its one reactive retry"),
                    Message("assistant", "Earlier state was already compacted")]
        for message in messages:
            store.append_message(sid, run["id"], message)
        store.checkpoint(sid, run["id"], messages, metadata={"compaction_state": {
            "version": 1, "run_id": run["id"], "reactive_used": True, "cold_prefix_applied": True},
            "calibration_fingerprint": calibration_fingerprint(engine._runtime_identity(), [], ""),
            "overhead_tokens": 0})
        result = await engine.recover(run["id"])
        assert result.status == "failed" and len(model.calls) == 1
        assert not any(event["type"] == "context.compacting" and event["payload"]["phase"] == "reactive"
                       for event in store.events(run_id=run["id"]))
    finally:
        store.close()


@pytest.mark.parametrize("damaged_checkpoint", [False, True])
def test_recovery_repairs_missing_result_and_preserves_control_metadata(tmp_path, damaged_checkpoint):
    store = Store(tmp_path / "runtime.db")
    try:
        sid = store.create_session(tmp_path)["id"]
        run, _ = store.create_run(sid)
        call = ToolCall("committed", "read", {})
        store.append_message(sid, run["id"], Message("user", "Original evidence"))
        store.append_message(sid, run["id"], Message("assistant", tool_calls=[call]))
        execution = store.prepare_tool(sid, run["id"], call, risk="read", replay_safe=True)
        store.mark_dispatching(execution["id"])
        store.commit_tool(execution["id"], ToolResult("Committed before archive save"))
        metadata = {"compaction_state": {"version": 1, "run_id": run["id"], "reactive_used": True,
                    "summary_failures": 3, "summary_backoff_until": 8}, "overhead_tokens": 1234,
                    "calibration_fingerprint": "fixture-identity"}
        prefix = ([Message("tool", "orphan result", tool_call_id="missing-call")] if damaged_checkpoint else
                  [Message("user", "Semantic prefix", metadata={"compaction_summary": True, "semantic": True}),
                   Message("assistant", tool_calls=[call])])
        store.checkpoint(sid, run["id"], prefix, metadata=metadata)
        boundary = store.get_checkpoint(sid)["archive_through"]
        before = store.history(sid)
        store.recover_run(run["id"])
        current = store.get_checkpoint(sid)
        active = store.active_history(sid)
        assert current["metadata"] == metadata
        assert active[-1].role == "tool" and active[-1].tool_call_id == call.id
        assert active[-1].content == "Committed before archive save"
        assert store.history(sid)[:len(before)] == before
        assert sum(message.role == "tool" for message in store.history(sid)) == 1
        assert any(message.metadata.get("semantic") for message in active) is not damaged_checkpoint
        assert (current["archive_through"] == boundary) is not damaged_checkpoint
        from aster.providers.deepseek import encode_messages
        encode_messages(active)
    finally:
        store.close()


async def test_cancel_from_force_stop_callback_leaves_a_terminal_run(tmp_path):
    store = Store(tmp_path / "runtime.db")
    try:
        config = RunConfig(max_iterations=1, context_tokens=10000, reserve_tokens=1000,
                           semantic_compaction=False, retained_context_rounds=1)
        sid = store.create_session(tmp_path)["id"]
        model = FakeModel([ModelTurn("x" * 40000, tool_calls=[ToolCall("unknown", "missing", {})])])
        engine = Engine(model, Registry(), store, config=config)

        async def cancel(kind, payload):
            if kind == "context.compacting" and payload["phase"] == "force_stop":
                engine.cancel(payload["run_id"])

        result = await engine.run(sid, "Exercise a deterministic budget stop", emit=cancel)
        assert result.status in {"cancelled", "budget_exhausted"}
        assert store.get_run(result.run_id)["status"] == result.status
        assert result.run_id not in engine._active
    finally:
        store.close()


async def test_steered_summary_usage_is_counted_before_any_following_model_request(tmp_path):
    store = Store(tmp_path / "runtime.db")
    try:
        config = RunConfig(context_tokens=10000, reserve_tokens=1000, retained_context_rounds=1,
                           summary_input_cap_tokens=1400, max_total_tokens=2000, max_iterations=5)
        sid = store.create_session(tmp_path)["id"]
        for index in range(20):
            store.append_message(sid, None, Message("assistant", f"Earlier observation {index}: " + "x" * 1000))
        summary = ModelTurn("<summary>## Current task & next steps\nContinue after steering.</summary>",
                            usage=Usage(input_tokens=1500, output_tokens=700))
        model = FakeModel([summary, ModelTurn("This second request should not be sent")])
        engine = Engine(model, Registry(), store, config=config)
        steered = False

        async def steer(kind, payload):
            nonlocal steered
            if kind == "context.summary.progress" and not steered:
                steered = True
                engine.inject(sid, "New user direction must remain available after the budget stop")
                engine.steer(payload["run_id"])

        result = await engine.run(sid, "Continue the existing task", emit=steer)
        assert steered
        assert result.status == "budget_exhausted"
        assert len(model.calls) == 1
        assert sum((item["input_tokens"] or 0) + (item["output_tokens"] or 0)
                   for item in store.usage(result.run_id)) == 2200
        assert any("New user direction" in message.content for message in store.history(sid)) or store.list_mailbox(sid)
    finally:
        store.close()


async def test_fresh_automation_cannot_inherit_source_only_compaction_notes(tmp_path):
    async with service(tmp_path) as (coordinator, _, _):
        source = coordinator.create_session(tmp_path)["id"]
        run, _ = coordinator.store.create_run(source)
        coordinator.store.append_message(source, run["id"], Message("user", "A private source-only decision"))
        note = "SOURCE_ONLY_COMPACTION_NOTE"
        coordinator.store.persist_compaction_learnings(source, run["id"],
            source_digest=compaction_source_digest(coordinator.store.history(source)), content="- " + note)
        coordinator.store.finish_run(run["id"], "completed")
        captured = coordinator.capture_automation_execution(source)
        assert note in captured["system"]
        fresh = coordinator.store.automation_execution_session(source, "audit-job", "audit-trigger", "fresh")["session_id"]
        assert coordinator.store.compact_learnings(fresh) == ""
        received = await coordinator.submit(fresh, "Independent automated task", execution_snapshot=captured)
        assert note not in received["system"]
        assert (await coordinator.wait(received["id"], timeout=3)).status == "completed"
