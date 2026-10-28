"""Offline integration of independent compaction gates and stale-result fences."""
import asyncio
import contextlib
import copy
from types import SimpleNamespace

import pytest

from aster.domain import Message, ModelTurn, ToolCall, ToolContext, Usage, to_dict
from aster.engine.compaction import LEARNINGS_SYSTEM, CompactionController
from aster.engine.context import ContextBudgetExceeded, estimate_tokens
from aster.engine.loop import RunConfig, SteeringInterrupt
from aster.engine.observations import ObservationManager
from aster.state import Store
from aster.state.compaction import compaction_source_digest


def history(*, rounds=24, width=2000):
    return [Message("user", "Complete the current project task and preserve its evidence."),
            *[Message("assistant", f"Observed round {index}: " + "x" * width) for index in range(rounds)]]


@pytest.fixture
def harness(tmp_path):
    owned = []

    def make(messages=None, *, memory=False, helper=None, emit=None, config=None, store=None,
             context=None, checkpoint=None):
        if store is None:
            store = Store(tmp_path / f"data-{len(owned)}" / "runtime.db")
            owned.append(store)
        if context is None:
            sid = store.create_session(tmp_path, project_id="fixture-project")["id"]
            run, _ = store.create_run(sid)
            store.start_budget(run["id"], 3600)
            context = ToolContext(sid, run["id"], tmp_path, tmp_path / "artifacts", asyncio.Event(),
                                  services={"steering": asyncio.Event()})
            if memory:
                context.services["memory"] = object()
            for message in messages or []:
                store.append_message(sid, run["id"], message)
        config = config or RunConfig(context_tokens=100000, reserve_tokens=1000,
            retained_context_rounds=3, semantic_compaction=False, file_restore_max_files=0)
        observer = ObservationManager(store, context, restore_files=config.file_restore_max_files,
            restore_chars=config.file_restore_chars, restore_tokens=config.file_restore_tokens,
            restore_per_file_tokens=config.file_restore_per_file_tokens,
            browser_keep=config.browser_observation_keep, image_keep=config.image_keep,
            browser_image_keep=config.browser_image_keep, browser_chars=config.browser_observation_chars)
        calls, events = [], []

        async def model(source, system, purpose):
            calls.append({"source": copy.deepcopy(source), "system": system, "purpose": purpose})
            if helper:
                return await helper(source, system, purpose, context)
            raise AssertionError("Unexpected compaction helper request")

        async def event(kind, payload):
            events.append((kind, copy.deepcopy(payload)))
            if emit:
                await emit(kind, payload, context)

        controller = CompactionController(store, context, config, observer, system="Stable task instructions",
            tools=list, helper=model, emit=event, checkpoint=checkpoint)
        return SimpleNamespace(store=store, context=context, config=config, observer=observer,
                               controller=controller, calls=calls, events=events)

    yield make
    for store in owned:
        store.close()


def phases(item):
    return [payload["phase"] for kind, payload in item.events if kind == "context.compacting"]


async def valid_summary(source, system, purpose, context):
    return ModelTurn("<analysis>Not persisted in the live summary.</analysis><summary>"
                     "## Current task & next steps\nContinue the current task using the saved evidence."
                     "</summary>")


async def test_actual_usage_proactive_uses_input_plus_output_and_does_not_retrigger_stale_usage(harness):
    messages = history(width=1200)
    item = harness(messages)
    estimate = estimate_tokens(messages, [], item.controller.system)
    assert estimate < item.controller.manager.proactive_threshold()
    item.controller.observe_usage(messages, Usage(input_tokens=89950, output_tokens=100))
    shaped = await item.controller.prepare(messages)
    assert phases(item) == ["proactive"]
    assert estimate_tokens(shaped, [], item.controller.system) < estimate
    assert item.controller.real_tokens is None
    assert item.store.history(item.context.session_id) == messages
    after = await item.controller.prepare(shaped)
    assert phases(item) == ["proactive"] and after is shaped
    assert item.calls == []


async def test_cold_estimate_uses_proactive_but_hot_estimate_requires_preflight(harness):
    moderate = history(width=7600)
    item = harness(moderate)
    cost = estimate_tokens(moderate, [], item.controller.system)
    assert item.controller.manager.proactive_threshold() <= cost < item.controller.manager.preflight_threshold()
    assert await item.controller.prepare(moderate) is not moderate
    assert phases(item) == ["proactive"]

    hot = harness(moderate)
    short = [Message("user", "new small task")]
    assert await hot.controller.prepare(short) is short
    assert await hot.controller.prepare(moderate) is moderate
    assert phases(hot) == []
    over = history(width=8000)
    assert estimate_tokens(over, [], hot.controller.system) >= hot.controller.manager.preflight_threshold()
    assert await hot.controller.prepare(over) is not over
    assert phases(hot) == ["preflight"]


async def test_reactive_floor_uses_rejected_input_skips_restoration_and_latches_across_restart(harness):
    messages = history(width=1600)
    item = harness(messages)
    rejected_estimate = estimate_tokens(messages, [], item.controller.system)

    async def must_not_restore(*args, **kwargs):
        raise AssertionError("Reactive recovery must not restore files")

    item.observer.restore = must_not_restore
    shaped = await item.controller.prepare(messages, force=True, phase="reactive")
    assert item.controller.manager.overhead_tokens == item.config.context_tokens - rejected_estimate + 1
    assert item.controller.reactive_used
    assert estimate_tokens(shaped, [], item.controller.system) < rejected_estimate
    assert not any(message.metadata.get("file_restoration") for message in shaped)
    # No usage after rejection: its floor must not cascade into estimated preflight.
    item.controller.manager.overhead_tokens = item.config.context_tokens
    assert await item.controller.prepare(shaped) is shaped
    with pytest.raises(ContextBudgetExceeded, match="already used"):
        await item.controller.prepare(shaped, force=True, phase="reactive")
    state = item.controller.state()
    restored = harness(store=item.store, context=item.context, config=item.config, checkpoint=state)
    assert restored.controller.reactive_used and not restored.controller._cold
    # A fresh measured response still enables proactive independently of the latch.
    restored.controller.observe_usage(messages, Usage(input_tokens=91000))
    await restored.controller.prepare(messages)
    assert phases(restored) == ["proactive"]


async def test_no_memory_service_never_adds_a_learning_request(harness):
    messages = history()
    config = RunConfig(context_tokens=100000, reserve_tokens=1000, retained_context_rounds=3,
                       semantic_compaction=True, file_restore_max_files=0)
    item = harness(messages, helper=valid_summary, config=config)
    shaped = await item.controller.prepare(messages, force=True)
    assert shaped is not messages
    assert [call["purpose"] for call in item.calls] == ["compaction"]
    assert item.store.compaction_learnings(item.context.session_id) == []


async def test_none_extraction_receipt_prevents_a_paid_repeat_after_restart(harness):
    messages = history()

    async def no_new(source, system, purpose, context):
        assert purpose == "compaction_learning"
        return ModelTurn("NONE")

    item = harness(messages, memory=True, helper=no_new)
    await item.controller.prepare(messages, force=True)
    digest = compaction_source_digest(messages)
    assert len(item.calls) == 1
    assert item.store.has_compaction_learnings_source(item.context.session_id, digest)
    assert item.store.compaction_learnings(item.context.session_id) == []
    path, context, config = item.store.path, item.context, item.config
    item.store.close()
    # Crash before a new context checkpoint: the same original source is loaded.
    with contextlib.closing(Store(path)) as reopened:
        recovered = harness(store=reopened, context=context, config=config)
        await recovered.controller.prepare(reopened.active_history(context.session_id), force=True)
        assert recovered.calls == []
        assert reopened.compact_learnings(context.session_id) == ""


async def test_unverified_notes_only_become_bounded_background_without_extra_verification(harness):
    messages = history()

    async def learn(source, system, purpose, context):
        assert purpose == "compaction_learning" and system == LEARNINGS_SYSTEM
        assert "Existing unverified notes" in source[0].content
        assert "Previous project decision." in source[0].content
        return ModelTurn("- A newly observed preference.")

    item = harness(messages, memory=True, helper=learn)
    item.store.persist_compaction_learnings(item.context.session_id, item.context.run_id,
        source_digest=compaction_source_digest([Message("user", "prior source")]), content="- Previous project decision.")
    await item.controller.prepare(messages, force=True)
    assert [call["purpose"] for call in item.calls] == ["compaction_learning"]
    assert len(item.store.compaction_learnings(item.context.session_id)) == 2
    assert all(not note["verified"] for note in item.store.compaction_learnings(item.context.session_id))
    assert not item.store.connection.execute("SELECT 1 FROM sqlite_master WHERE name='memory_facts'").fetchone()


@pytest.mark.parametrize("signal", ["cancel", "steering"])
@pytest.mark.parametrize("purpose", ["compaction", "compaction_learning"])
async def test_control_signal_after_helper_reply_discards_stale_result_and_receipt(harness, signal, purpose):
    messages = history()

    async def stale(source, system, called_purpose, context):
        assert called_purpose == purpose
        await asyncio.sleep(0)
        (context.cancel if signal == "cancel" else context.services["steering"]).set()
        return ModelTurn("NONE" if purpose == "compaction_learning" else "<summary>## Current task & next steps\nSTALE</summary>")

    config = RunConfig(context_tokens=100000, reserve_tokens=1000, retained_context_rounds=3,
                       semantic_compaction=purpose == "compaction", file_restore_max_files=0)
    item = harness(messages, memory=purpose == "compaction_learning", helper=stale, config=config)
    before = copy.deepcopy(messages)
    state = item.observer.state()
    with pytest.raises(asyncio.CancelledError if signal == "cancel" else SteeringInterrupt):
        await item.controller.prepare(messages, force=True)
    assert messages == before and item.store.history(item.context.session_id) == before
    assert item.store.compaction_snapshots(item.context.session_id) == []
    assert not item.store.has_compaction_learnings_source(item.context.session_id, compaction_source_digest(messages))
    assert item.observer.state() == state and item.controller._cold
    assert item.controller.manager._summary_cache == ""


async def test_steering_rolls_back_pending_observation_rewrites_then_defers_helpers(harness):
    messages = [Message("user", "Read the pages")]
    for index in range(7):
        messages.extend([Message("assistant", tool_calls=[ToolCall(f"page-{index}", "browser_snapshot", {})]),
                         Message("tool", "snapshot body " * 150, tool_call_id=f"page-{index}")])

    async def stale(source, system, purpose, context):
        context.services["steering"].set()
        return await valid_summary(source, system, purpose, context)

    config = RunConfig(context_tokens=100000, reserve_tokens=1000, retained_context_rounds=3,
                       semantic_compaction=True, file_restore_max_files=0)
    item = harness(messages, helper=stale, config=config)
    with pytest.raises(SteeringInterrupt):
        await item.controller.prepare(messages, force=True)
    assert item.observer.state()["replacements"] == {}
    assert not any(message.metadata.get("local_compaction") for message in messages)
    item.context.services["steering"].clear()
    direction = Message("user", "Stop the old page investigation; answer the new question.")
    item.store.append_message(item.context.session_id, item.context.run_id, direction)
    item.controller.protect([direction])
    updated = [*messages, direction]
    result = await item.controller.prepare(updated, force=True, allow_helpers=False)
    assert len(item.calls) == 1
    assert any(direction.content in message.content for message in result)
    assert item.store.compaction_snapshots(item.context.session_id)[-1]["messages"] == to_dict(updated)


@pytest.mark.parametrize("signal", ["cancel", "steering"])
async def test_control_signal_during_file_restore_cannot_commit_old_context(harness, signal):
    messages = history()
    item = harness(messages)

    async def stale_restore(*args, **kwargs):
        await asyncio.sleep(0)
        (item.context.cancel if signal == "cancel" else item.context.services["steering"]).set()
        return [Message("user", "STALE RESTORED BODY", metadata={"file_restoration": [{"path": "x"}]})]

    item.observer.restore = stale_restore
    with pytest.raises(asyncio.CancelledError if signal == "cancel" else SteeringInterrupt):
        await item.controller.prepare(messages, force=True)
    assert item.store.compaction_snapshots(item.context.session_id) == []
    assert item.store.active_history(item.context.session_id) == messages
    assert item.controller._cold


async def test_restoration_and_reanchor_use_measured_final_wire_budget(harness):
    messages = history(width=1500)
    config = RunConfig(context_tokens=30000, reserve_tokens=1000, retained_context_rounds=3,
                       semantic_compaction=False, file_restore_max_files=2)
    item = harness(messages, config=config)
    headrooms = []

    async def oversized_restore(current, *, headroom_tokens, headroom_chars):
        headrooms.append(headroom_tokens)
        return [Message("user", "\n" * 100000, metadata={"file_restoration": [{"path": "large.txt"}]})]

    item.observer.restore = oversized_restore
    shaped = await item.controller.prepare(messages, force=True)
    assert headrooms and headrooms[0] >= 0
    assert not any(message.metadata.get("file_restoration") for message in shaped)
    assert any(message.metadata.get("compaction_reanchor") for message in shaped)
    assert estimate_tokens(shaped, [], item.controller.system) + item.controller.manager.overhead_tokens < item.controller.manager.proactive_threshold()
    assert not any(kind == "context.files_restored" for kind, _ in item.events)


async def test_learning_request_budgets_existing_notes_framing_and_cjk_together(harness):
    messages = history(rounds=24, width=1000)
    messages[1].content = "包含转义和中文的证据\n\"" * 800

    async def no_new(source, system, purpose, context):
        assert purpose == "compaction_learning"
        assert estimate_tokens(source, [], system) <= 1600
        return ModelTurn("NONE")

    config = RunConfig(context_tokens=5000, reserve_tokens=700, retained_context_rounds=1,
                       semantic_compaction=False, summary_input_cap_tokens=1600, file_restore_max_files=0)
    item = harness(messages, memory=True, helper=no_new, config=config)
    item.store.persist_compaction_learnings(item.context.session_id, item.context.run_id,
        source_digest=compaction_source_digest([Message("user", "older note source")]), content="- 中文偏好\n" * 1500)
    await item.controller.prepare(messages, force=True)
    assert len(item.calls) == 1


async def test_local_cold_prefix_rewrite_snapshots_original_once_and_preserves_hot_prefix(harness):
    messages = [Message("user", "Inspect pages")]
    for index in range(6):
        messages.extend([Message("assistant", tool_calls=[ToolCall(f"page-{index}", "browser_snapshot", {})]),
                         Message("tool", "Small page observation" * 40, tool_call_id=f"page-{index}")])
    item = harness(messages)
    shaped = await item.controller.prepare(messages)
    assert phases(item) == [] and shaped is not messages
    first = item.store.compaction_snapshots(item.context.session_id)[0]
    assert first["phase"] == "observations" and first["messages"] == to_dict(messages)
    next_turn = [*shaped, Message("assistant", tool_calls=[ToolCall("page-new", "browser_snapshot", {})]),
                 Message("tool", "One newly observed page", tool_call_id="page-new")]
    assert await item.controller.prepare(next_turn) is next_turn
    assert item.store.compaction_snapshots(item.context.session_id)[0]["id"] == first["id"]
    assert len([kind for kind, _ in item.events if kind == "context.cache_rewrite"]) == 1
    assert item.calls == []


async def test_snapshot_and_restore_errors_do_not_discard_successful_compaction(harness, monkeypatch):
    messages = history()
    item = harness(messages)

    def fail_snapshot(*args, **kwargs):
        raise OSError("synthetic snapshot failure")

    async def fail_restore(*args, **kwargs):
        raise OSError("synthetic file refresh failure")

    monkeypatch.setattr(item.store, "save_compaction_snapshot", fail_snapshot)
    item.observer.restore = fail_restore
    shaped = await item.controller.prepare(messages, force=True)
    assert shaped is not messages
    assert {"context.file_restore_failed", "context.snapshot_failed"} <= {kind for kind, _ in item.events}
    assert all("synthetic" not in str(payload) for _, payload in item.events)
    item.store.checkpoint(item.context.session_id, item.context.run_id, shaped)
    assert item.store.active_history(item.context.session_id) == shaped
    assert item.store.history(item.context.session_id) == messages


async def test_three_failed_summaries_back_off_five_main_iterations_and_survive_restart(harness):
    messages = history()
    attempted = []

    async def failing(source, system, purpose, context):
        attempted.append(purpose)
        if len(attempted) <= 3:
            raise RuntimeError("synthetic helper unavailable")
        return await valid_summary(source, system, purpose, context)

    config = RunConfig(context_tokens=100000, reserve_tokens=1000, retained_context_rounds=3,
                       semantic_compaction=True, file_restore_max_files=0)
    item = harness(messages, helper=failing, config=config)
    for _ in range(3):
        item.store.reserve_iteration(item.context.run_id, 100)
        assert await item.controller.prepare(messages, force=True) is not messages
    assert len(attempted) == 3
    state = item.controller.state()
    assert state["compaction_state"]["summary_failures"] == 3
    restored = harness(store=item.store, context=item.context, helper=failing, config=config, checkpoint=state)
    for _ in range(5):
        item.store.reserve_iteration(item.context.run_id, 100)
        assert await restored.controller.prepare(messages, force=True) is not messages
    assert len(attempted) == 3
    item.store.reserve_iteration(item.context.run_id, 100)
    shaped = await restored.controller.prepare(messages, force=True)
    assert shaped is not messages and len(attempted) == 4
    assert restored.controller.state()["compaction_state"]["summary_failures"] == 0
    assert len([kind for kind, _ in restored.events if kind == "context.summary_backoff"]) == 5


async def test_valid_but_nonreducing_summary_counts_towards_backoff(harness):
    messages = history(rounds=12, width=20)

    async def too_long(source, system, purpose, context):
        return ModelTurn("<summary>## Current task & next steps\n" + "Verbose redundant prose. " * 500 + "</summary>")

    config = RunConfig(context_tokens=100000, reserve_tokens=1000, retained_context_rounds=3,
                       semantic_compaction=True, file_restore_max_files=0)
    item = harness(messages, helper=too_long, config=config)
    assert await item.controller.prepare(messages, force=True) is messages
    assert item.controller.state()["compaction_state"]["summary_failures"] == 1
    assert any(kind == "context.summary_fallback" and payload["reason"] == "no_reduction"
               for kind, payload in item.events)
    assert item.store.compaction_snapshots(item.context.session_id) == []
