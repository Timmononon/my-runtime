import asyncio
from pathlib import Path

import pytest

from aster.domain import Message, ModelTurn, ToolCall, ToolContext, ToolResult, ToolSpec, Usage
from aster.engine import Engine, FakeModel, RunConfig
from aster.engine.context import ContextManager
from aster.engine.dispatcher import Dispatcher
from aster.policy import ApprovalBroker, PermissionPolicy
from aster.state import ReconciliationRequired, SessionBusy, Store
from aster.tools.registry import Registry


class CountingTool:
    def __init__(self, name="count", risk="read", *, safe=False, delay=0):
        self.spec = ToolSpec(name, "Test controlled operation", {"type": "object", "properties": {"value": {"type": "string"}},
                                                              "required": ["value"], "additionalProperties": False},
                             risk=risk, concurrency_safe=safe, replay_safe=risk == "read")
        self.calls = []
        self.delay = delay
        self.current = 0
        self.max_current = 0

    async def execute(self, arguments, context):
        self.calls.append(arguments["value"])
        self.current += 1
        self.max_current = max(self.max_current, self.current)
        await asyncio.sleep(self.delay)
        self.current -= 1
        return ToolResult(arguments["value"])


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "state.db")
    yield value
    value.close()


def session(store, tmp_path):
    return store.create_session(tmp_path)["id"]


def toolturn(cid="call_1", value="hello", name="count"):
    return ModelTurn(tool_calls=[ToolCall(cid, name, {"value": value})], finish_reason="tool_calls")


async def test_complete_model_tool_feedback_archive_and_checkpoint(store, tmp_path):
    sid = session(store, tmp_path)
    tool = CountingTool()
    model = FakeModel([toolturn(), ModelTurn("finished", usage=Usage(input_tokens=10, output_tokens=2))])
    engine = Engine(model, Registry([tool]), store)
    result = await engine.run(sid, "run the task")
    assert result.status == "completed"
    assert result.text == "finished"
    assert tool.calls == ["hello"]
    assert [m.role for m in store.history(sid)] == ["user", "assistant", "tool", "assistant"]
    assert model.calls[1]["messages"][-1].tool_call_id == "call_1"
    assert store.tool_executions(result.run_id)[0]["state"] == "checkpointed"
    events = store.events(run_id=result.run_id)
    assert [event["event_id"] for event in events] == sorted(event["event_id"] for event in events)
    assert store.usage(result.run_id)[0]["input_tokens"] is None
    assert store.usage(result.run_id)[1]["input_tokens"] == 10


async def test_missing_required_field_never_dispatches_explicit_empty_allowed(store, tmp_path):
    sid = session(store, tmp_path)
    tool = CountingTool(risk="write")
    model = FakeModel([ModelTurn(tool_calls=[ToolCall("missing", "count", {})]), toolturn("empty", ""), ModelTurn("done")])
    result = await Engine(model, Registry([tool]), store, policy=PermissionPolicy(allowed_tools={"count"})).run(sid, "write")
    assert result.status == "completed"
    assert tool.calls == [""]
    first_result = next(m for m in store.history(sid) if m.role == "tool")
    assert first_result.metadata["error_category"] == "validation"
    assert first_result.metadata["outcome"] == "not_executed"


async def test_permission_deny_wins_and_unknown_tool_is_paired(store, tmp_path):
    sid = session(store, tmp_path)
    tool = CountingTool()
    turn = ModelTurn(tool_calls=[ToolCall("denied", "count", {"value": "x"}), ToolCall("unknown", "missing", {})])
    model = FakeModel([turn, ModelTurn("cannot execute")])
    policy = PermissionPolicy(allowed_tools={"count"}, denied_tools={"count"})
    result = await Engine(model, Registry([tool]), store, policy=policy).run(sid, "try")
    assert result.status == "completed" and not tool.calls
    replies = [m for m in store.history(sid) if m.role == "tool"]
    assert [m.tool_call_id for m in replies] == ["denied", "unknown"]
    assert all(m.metadata["outcome"] == "not_executed" for m in replies)


async def test_approval_once_and_late_decision(store, tmp_path):
    sid = session(store, tmp_path)
    tool = CountingTool(risk="write")
    broker = ApprovalBroker(store, timeout_seconds=1)
    decisions = []

    async def emit(kind, payload):
        if kind == "approval.waiting":
            decisions.append(broker.resolve(payload["request_id"], True))
            decisions.append(broker.resolve(payload["request_id"], True))

    result = await Engine(FakeModel([toolturn(), ModelTurn("done")]), Registry([tool]), store,
                          approvals=broker).run(sid, "write", emit=emit)
    assert result.status == "completed"
    assert decisions == [True, False]
    assert tool.calls == ["hello"]
    assert store.pending_requests(result.run_id) == []


async def test_cancel_while_approval_waiting_cannot_late_execute(store, tmp_path):
    sid = session(store, tmp_path)
    tool = CountingTool(risk="write")
    broker = ApprovalBroker(store)
    engine = Engine(FakeModel([toolturn()]), Registry([tool]), store, approvals=broker)
    pending_ids = []

    async def emit(kind, payload):
        if kind == "approval.waiting":
            pending_ids.append(payload["request_id"])
            engine.cancel(payload["run_id"])

    result = await engine.run(sid, "write", emit=emit)
    assert result.status == "cancelled"
    assert not broker.resolve(pending_ids[0], True)
    assert not tool.calls


async def test_safe_batches_order_barriers_and_concurrency_cap(store, tmp_path):
    sid = session(store, tmp_path)
    read = CountingTool("read", safe=True, delay=0.02)
    write = CountingTool("write", "write")
    run, _ = store.create_run(sid)
    calls = [ToolCall(f"r{i}", "read", {"value": str(i)}) for i in range(5)]
    calls += [ToolCall("write", "write", {"value": "w"}), ToolCall("r6", "read", {"value": "6"})]
    context = ToolContext(sid, run["id"], tmp_path, tmp_path, asyncio.Event())
    dispatcher = Dispatcher(Registry([read, write]), store, PermissionPolicy(allowed_tools={"write"}), max_concurrency=2)
    result = await dispatcher.execute(calls, context)
    assert [item.content for item in result] == ["0", "1", "2", "3", "4", "w", "6"]
    assert read.max_current == 2
    assert write.calls == ["w"]


async def test_unknown_effect_stops_following_tool_and_future_runs(store, tmp_path):
    class Uncertain(CountingTool):
        async def execute(self, args, ctx):
            self.calls.append(args["value"])
            return ToolResult("Connection lost after submit", True, "transport", "unknown")

    sid = session(store, tmp_path)
    uncertain = Uncertain("submit", "external")
    next_tool = CountingTool("after", "write")
    turn = ModelTurn(tool_calls=[ToolCall("a", "submit", {"value": "submit"}), ToolCall("b", "after", {"value": "after"})])
    engine = Engine(FakeModel([turn]), Registry([uncertain, next_tool]), store,
                    policy=PermissionPolicy(allowed_tools={"submit", "after"}))
    result = await engine.run(sid, "submit")
    assert result.status == "needs_reconciliation"
    assert not next_tool.calls
    assert len([m for m in store.history(sid) if m.role == "tool"]) == 2
    with pytest.raises(ReconciliationRequired):
        await engine.run(sid, "new request does not clear uncertainty")


async def test_incomplete_response_never_runs_tools(store, tmp_path):
    sid = session(store, tmp_path)
    tool = CountingTool()
    turn = toolturn()
    turn.finish_reason = "length"
    result = await Engine(FakeModel([turn]), Registry([tool]), store).run(sid, "try")
    assert result.status == "failed"
    assert not tool.calls


async def test_duplicate_tool_id_not_reexecuted(store, tmp_path):
    sid = session(store, tmp_path)
    tool = CountingTool()
    result = await Engine(FakeModel([toolturn(), toolturn()]), Registry([tool]), store).run(sid, "try")
    assert result.status == "failed"
    assert tool.calls == ["hello"]


async def test_loop_guard_limits_repeated_identical_results(store, tmp_path):
    sid = session(store, tmp_path)
    tool = CountingTool()
    model = FakeModel([toolturn(str(i)) for i in range(8)])
    result = await Engine(model, Registry([tool]), store, config=RunConfig(duplicate_limit=2)).run(sid, "try")
    assert result.status == "budget_exhausted"
    assert len(tool.calls) == 2
    assert "no-progress" in result.error


async def test_follow_up_queued_during_generation_is_not_lost(store, tmp_path):
    sid = session(store, tmp_path)
    engine = None

    async def first(messages, tools):
        engine.inject(sid, "additional constraint")
        return ModelTurn("first answer")

    model = FakeModel([first, ModelTurn("answer with constraint")])
    engine = Engine(model, Registry(), store)
    result = await engine.run(sid, "initial")
    assert result.text == "answer with constraint"
    assert model.calls[1]["messages"][-1].content == "additional constraint"
    assert [m.content for m in store.history(sid) if m.role == "user"] == ["initial", "additional constraint"]


async def test_single_session_run_owner(store, tmp_path):
    sid = session(store, tmp_path)
    entered, finish = asyncio.Event(), asyncio.Event()

    async def waiting(messages, tools):
        entered.set()
        await finish.wait()
        return ModelTurn("done")

    engine = Engine(FakeModel([waiting]), Registry(), store)
    task = asyncio.create_task(engine.run(sid, "one"))
    await entered.wait()
    with pytest.raises(SessionBusy):
        await engine.run(sid, "two")
    with pytest.raises(SessionBusy):
        store.create_run(sid)
    finish.set()
    assert (await task).status == "completed"


async def test_model_cancel_and_sink_failure_are_distinct(store, tmp_path):
    sid = session(store, tmp_path)
    entered = asyncio.Event()

    async def wait_forever(messages, tools):
        entered.set()
        await asyncio.Event().wait()

    engine = Engine(FakeModel([wait_forever]), Registry(), store)
    task = asyncio.create_task(engine.run(sid, "one"))
    await entered.wait()
    rid = store.list_runs(sid)[0]["id"]
    assert engine.cancel(rid)
    assert (await task).status == "cancelled"

    async def broken_sink(kind, payload):
        raise ConnectionError("UI disconnected")

    result = await Engine(FakeModel([ModelTurn("still completes")]), Registry(), store).run(sid, "two", emit=broken_sink)
    assert result.status == "completed"


async def test_output_budget_preserves_original_and_image_references(store, tmp_path):
    class BigTool(CountingTool):
        async def execute(self, args, context):
            return ToolResult("X" * 3000, artifacts=[{"artifact_id": "pic", "path": str(tmp_path / "pic.png")}],
                              data={"content_parts": [{"type": "input_image", "path": str(tmp_path / "pic.png"), "artifact_id": "pic"}]})

    sid = session(store, tmp_path)
    model = FakeModel([toolturn(), ModelTurn("done")])
    result = await Engine(model, Registry([BigTool()]), store, config=RunConfig(tool_output_chars=1000)).run(sid, "get output")
    assert result.status == "completed"
    raw = next(m for m in store.history(sid) if m.role == "tool")
    live = model.calls[1]["messages"][-1]
    assert len(raw.content) == 3000
    assert "truncated" in live.content
    assert raw.content in Path(live.metadata["output_artifact"]["path"]).read_text()
    assert any(part["type"] == "input_image" for part in live.metadata["content_parts"])
    assert live.metadata["content_parts"][0]["text"] == live.content
    assert "data" not in live.metadata


def test_checkpoint_compaction_keeps_full_archive_and_safe_tool_groups(store, tmp_path):
    sid = session(store, tmp_path)
    run, _ = store.create_run(sid)
    messages = [Message("user", "Original objective keep this exact path D:/data/input.csv")]
    for i in range(12):
        messages.append(Message("assistant", tool_calls=[ToolCall(str(i), "read", {"path": f"file{i}"})]))
        messages.append(Message("tool", "Z" * 1000, tool_call_id=str(i)))
    for message in messages:
        store.append_message(sid, run["id"], message)
    manager = ContextManager(context_tokens=5000, reserve_tokens=500, retain_turns=2)
    shaped = manager.compact(messages, [], "")
    assert len(shaped) < len(messages)
    assert "Original objective" in shaped[0].content
    pending = set()
    for message in shaped:
        pending.update(call.id for call in message.tool_calls)
        if message.role == "tool":
            assert message.tool_call_id in pending
            pending.remove(message.tool_call_id)
    assert not pending
    store.checkpoint(sid, run["id"], shaped)
    assert len(store.history(sid)) == len(messages)
    assert len(store.active_history(sid)) == len(shaped)


async def test_recovery_committed_result_is_not_reexecuted(store, tmp_path):
    sid = session(store, tmp_path)
    run, _ = store.create_run(sid, config=RunConfig().__dict__)
    call = ToolCall("write1", "count", {"value": "already wrote"})
    store.append_message(sid, run["id"], Message("user", "write"))
    store.append_message(sid, run["id"], Message("assistant", tool_calls=[call]))
    entry = store.prepare_tool(sid, run["id"], call, risk="write", replay_safe=False)
    store.mark_dispatching(entry["id"])
    store.commit_tool(entry["id"], ToolResult("actual write complete"))
    tool = CountingTool(risk="write")
    model = FakeModel([ModelTurn("confirmed from saved result")])
    outcome = await Engine(model, Registry([tool]), store).recover(run["id"])
    assert outcome.status == "completed"
    assert not tool.calls
    assert model.calls[0]["messages"][-1].content == "actual write complete"


async def test_crashed_dispatch_is_unknown_then_manual_reconciliation_preserves_archive(store, tmp_path):
    sid = session(store, tmp_path)
    run, _ = store.create_run(sid, config=RunConfig().__dict__)
    call = ToolCall("submit1", "count", {"value": "submit"})
    store.append_message(sid, run["id"], Message("assistant", tool_calls=[call]))
    entry = store.prepare_tool(sid, run["id"], call, risk="external", replay_safe=False)
    store.mark_dispatching(entry["id"])
    engine = Engine(FakeModel([ModelTurn("verified")]), Registry([CountingTool()]), store)
    outcome = await engine.recover(run["id"])
    assert outcome.status == "needs_reconciliation"
    unknown = store.history(sid)[-1].content
    store.reconcile_tool(entry["id"], ToolResult("Submitted exactly once, verified externally",
                         error_category="verified", data={"version": 123}), evidence="External receipt 123 verified by user")
    outcome = await engine.recover(run["id"])
    assert outcome.status == "completed"
    assert unknown in [message.content for message in store.history(sid)]
    corrected = next(message for message in store.active_history(sid) if message.role == "tool")
    assert "Submitted exactly once" in corrected.content and '"version": 123' in corrected.content
    assert corrected.metadata["error_category"] == "verified"


def test_sqlite_reopen_backup_search_scope_and_atomic_event(tmp_path):
    path = tmp_path / "state.db"
    with_store = Store(path)
    a = with_store.create_session(tmp_path, agent_id="a", project_id="p")["id"]
    b = with_store.create_session(tmp_path, agent_id="b", project_id="q")["id"]
    with_store.append_message(a, None, Message("user", "中文文档 data analysis"))
    with_store.append_message(b, None, Message("user", "中文文档 other project"))
    assert len(with_store.search_messages("中文文档", agent_id="a", project_id="p")) == 1
    assert with_store.search_messages("中文", agent_id="b") == []
    assert len(with_store.search_messages("中文", agent_id="b", project_id="q")) == 1
    before = len(with_store.events())
    with pytest.raises(RuntimeError), with_store.transaction() as db:
        with_store._event(db, a, None, "must.rollback", {})
        raise RuntimeError("crash boundary")
    assert len(with_store.events()) == before
    with_store.backup(tmp_path / "backup.db")
    with_store.close()
    reopened = Store(path)
    backup = Store(tmp_path / "backup.db")
    assert reopened.history(a)[0].content == backup.history(a)[0].content
    reopened.close()
    backup.close()


async def test_idempotent_client_request_does_not_start_second_run(store, tmp_path):
    sid = session(store, tmp_path)
    model = FakeModel([ModelTurn("done")])
    engine = Engine(model, Registry(), store)
    first = await engine.run(sid, "task", request_id="stable-id")
    second = await engine.run(sid, "task", request_id="stable-id")
    assert first.run_id == second.run_id
    assert len(model.calls) == 1


async def test_semantic_summary_preserves_middle_user_constraint_and_exact_reference():
    messages = [Message("user", "Initial task")]
    for i in range(12):
        if i == 5:
            messages.append(Message("user", "Never delete original measurements. Keep D:/critical/middle.csv unchanged."))
        messages.extend([Message("assistant", tool_calls=[ToolCall(str(i), "read", {"value": str(i)})]),
                         Message("tool", "Large scientific evidence " * 50, tool_call_id=str(i))])
    calls = []

    async def summarize(source):
        calls.append(source)
        # Deliberately omit the crucial middle constraint: mechanical protection must keep it.
        return ModelTurn("# Goal\nAnalyze data\n# Constraints\nBe careful\n# Completed evidence\nRead files\n# Open work\nContinue\n# References\nFiles")

    manager = ContextManager(context_tokens=6000, reserve_tokens=500, retain_turns=2)
    shaped = await manager.compact_with_summary(messages, [], "", summarize)
    assert calls
    assert shaped[0].metadata.get("semantic") is True
    assert "Never delete original measurements" in shaped[0].content
    assert "D:/critical/middle.csv" in shaped[0].content
    assert len(messages) == 26


async def test_summary_usage_is_accounted_and_no_tool_schema_sent(store, tmp_path):
    sid = session(store, tmp_path)
    prior, _ = store.create_run(sid, config=RunConfig().__dict__)
    store.append_message(sid, prior["id"], Message("user", "Keep originals at D:/data/original.csv"))
    for i in range(10):
        store.append_message(sid, prior["id"], Message("assistant", tool_calls=[ToolCall(str(i), "count", {"value": str(i)})]))
        store.append_message(sid, prior["id"], Message("tool", "x" * 1000, tool_call_id=str(i)))
    store.set_run_status(prior["id"], "completed")

    def respond(messages, tools):
        if not tools:
            return ModelTurn("Goal: finish\nConstraints: preserve originals\nCompleted evidence: files read\nOpen work: answer\nReferences: D:/data/original.csv",
                             usage=Usage(input_tokens=500, output_tokens=60))
        return ModelTurn("Done", usage=Usage(input_tokens=100, output_tokens=5))

    model = FakeModel([respond] * 10)
    result = await Engine(model, Registry([CountingTool()]), store,
                          config=RunConfig(context_tokens=6500, reserve_tokens=500, retained_context_rounds=2)).run(sid, "Continue")
    assert result.status == "completed", result.error
    assert any(not call["tools"] for call in model.calls)
    usage = store.usage(result.run_id)
    assert any(item["raw"].get("purpose") == "compaction" for item in usage)
    assert store.get_checkpoint(sid)["metadata"]["calibration_fingerprint"]
    assert len(store.history(sid)) == 23


def test_usage_calibration_corrects_underestimated_prompt():
    manager = ContextManager(context_tokens=10000, reserve_tokens=1000)
    messages = [Message("user", "small")]
    manager.observe_usage(messages, [], "", 7000)
    assert manager.overhead_tokens > 6000
    manager.observe_usage(messages, [], "", None)
    assert manager.overhead_tokens > 6000  # Unknown usage is not a zero sample.


async def test_timeout_requests_cooperative_cancel_without_task_cancel(store, tmp_path):
    class Cooperative(CountingTool):
        async def execute(self, args, context):
            try:
                await context.cancel.wait()
            except asyncio.CancelledError:
                pytest.fail("Dispatcher directly cancelled a tool task")
            return ToolResult("Stopped by owned cancellation signal", True, "cancelled", "known")

    sid = session(store, tmp_path)
    run, _ = store.create_run(sid)
    tool = Cooperative()
    tool.spec.timeout_seconds = 0.01
    dispatcher = Dispatcher(Registry([tool]), store, PermissionPolicy(), cancellation_grace=0.1)
    result = await dispatcher.execute([ToolCall("c", "count", {"value": "x"})], ToolContext(sid, run["id"], tmp_path, tmp_path, asyncio.Event()))
    assert result[0].error_category == "cancelled"
    assert not dispatcher.background_tasks


async def test_tool_exception_after_material_dispatch_is_unknown(store, tmp_path):
    class Exploding(CountingTool):
        async def execute(self, args, context):
            self.calls.append(args["value"])
            raise ConnectionError("Response lost")

    sid = session(store, tmp_path)
    tool = Exploding(risk="external")
    result = await Engine(FakeModel([toolturn()]), Registry([tool]), store,
                          policy=PermissionPolicy(allowed_tools={"count"})).run(sid, "submit")
    assert result.status == "needs_reconciliation"
    assert store.tool_executions(result.run_id)[0]["state"] == "outcome_unknown"


async def test_retryable_model_error_does_not_replay_previous_local_tool(store, tmp_path):
    class RetryableError(Exception):
        retryable = True
        category = "rate_limit"

    sid = session(store, tmp_path)
    tool = CountingTool()
    result = await Engine(FakeModel([toolturn(), RetryableError(), ModelTurn("done")]), Registry([tool]), store,
                          config=RunConfig(retry_base_seconds=0.001)).run(sid, "try")
    assert result.status == "completed"
    assert tool.calls == ["hello"]


async def test_skill_restriction_checked_at_runtime(store, tmp_path):
    class Restricted:
        def check_tool(self, name):
            return False

    sid = session(store, tmp_path)
    tool = CountingTool()
    result = await Engine(FakeModel([toolturn(), ModelTurn("blocked")]), Registry([tool]), store,
                          services={"skill_scope": Restricted()}).run(sid, "try")
    assert result.status == "completed" and not tool.calls
    assert any("skill restricts" in m.content.lower() for m in store.history(sid) if m.role == "tool")


async def test_independent_hook_journal_blocks_recovery_without_model_call(store, tmp_path):
    sid = session(store, tmp_path)
    run, _ = store.create_run(sid, config=RunConfig().__dict__)
    hook = ToolCall("hook-start", "hook:SessionStart", {"executable": "trusted-hook"})
    entry = store.prepare_tool(sid, run["id"], hook, risk="execute", replay_safe=False)
    store.mark_dispatching(entry["id"])
    model = FakeModel([ModelTurn("must not run")])
    result = await Engine(model, Registry(), store).recover(run["id"])
    assert result.status == "needs_reconciliation"
    assert not model.calls
    assert store.tool_execution(entry["id"])["state"] == "outcome_unknown"


async def test_recovery_cannot_reset_cumulative_iteration_budget(store, tmp_path):
    sid = session(store, tmp_path)
    tool = CountingTool()
    first = Engine(FakeModel([toolturn(), RuntimeError("offline provider failure")]), Registry([tool]), store,
                   config=RunConfig(max_iterations=2))
    failed = await first.run(sid, "bounded work")
    assert failed.status == "failed"
    assert store.run_budget(failed.run_id)["iterations"] == 2
    replacement = FakeModel([ModelTurn("must not request again")])
    resumed = await Engine(replacement, Registry([tool]), store).recover(failed.run_id)
    assert resumed.status == "budget_exhausted"
    assert resumed.iterations == 2
    assert not replacement.calls
    assert store.run_budget(failed.run_id)["model_requests"] == 2


async def test_recovery_keeps_original_deadline_and_failed_usage_unknown(store, tmp_path):
    import time
    sid = session(store, tmp_path)
    failed = await Engine(FakeModel([ConnectionError("uncertain network request")]), Registry(), store).run(sid, "bounded")
    usage = store.usage(failed.run_id)
    assert len(usage) == 1 and usage[0]["input_tokens"] is None and usage[0]["output_tokens"] is None
    assert usage[0]["raw"]["status"] == "outcome_unknown"
    with store.transaction() as db:
        db.execute("UPDATE run_budgets SET deadline_at=? WHERE run_id=?", (time.time() - 1, failed.run_id))
    model = FakeModel([ModelTurn("must not resume expired run")])
    outcome = await Engine(model, Registry(), store).recover(failed.run_id)
    assert outcome.status == "timed_out"
    assert not model.calls
    assert len(store.usage(failed.run_id)) == 1


async def test_total_model_request_budget_includes_retry_and_rejects_zero(store, tmp_path):
    class Retryable(Exception):
        retryable = True
        category = "network"

    with pytest.raises(ValueError):
        RunConfig(max_model_requests=0)
    sid = session(store, tmp_path)
    model = FakeModel([Retryable(), ModelTurn("must not issue second request")])
    outcome = await Engine(model, Registry(), store, config=RunConfig(max_model_requests=1, retry_base_seconds=0.001)).run(sid, "one request")
    assert outcome.status == "budget_exhausted"
    assert len(model.calls) == 1
    assert store.run_budget(outcome.run_id)["model_requests"] == 1
    assert store.usage(outcome.run_id)[0]["input_tokens"] is None


async def test_recovery_reapplies_wire_budget_and_preserves_raw_mixed_result(store, tmp_path):
    from PIL import Image

    from aster.domain import to_dict
    from aster.providers.deepseek import encode_messages

    image = tmp_path / "fixture.png"
    Image.new("RGB", (2, 2), "white").save(image, "PNG")
    text = "first-evidence-" + "x" * 20000 + "-last-evidence"

    class MixedTool(CountingTool):
        async def execute(self, args, context):
            return ToolResult(text, data={"sha256": "a" * 64, "version": 71, "content_parts": [
                {"type": "input_text", "text": text},
                {"type": "input_image", "path": str(image), "artifact_id": "fixture", "mime_type": "image/png"}]},
                artifacts=[{"artifact_id": "fixture", "path": str(image), "mime_type": "image/png"}])

    sid = session(store, tmp_path)
    tools = Registry([MixedTool()])
    initial = FakeModel([toolturn(), RuntimeError("interrupted model request")])
    outcome = await Engine(initial, tools, store, config=RunConfig(tool_output_chars=1000)).run(sid, "preserve source")
    assert outcome.status == "failed"
    raw_before = to_dict(next(item for item in store.history(sid) if item.role == "tool"))
    assert raw_before["content"] == text
    resumed = FakeModel([ModelTurn("done")])
    assert (await Engine(resumed, tools, store).recover(outcome.run_id)).status == "completed"
    raw_after = to_dict(next(item for item in store.history(sid) if item.role == "tool"))
    assert raw_before == raw_after
    for messages in (initial.calls[1]["messages"], resumed.calls[0]["messages"]):
        encoded = encode_messages(messages, tmp_path)
        output = next(item["output"] for item in encoded if item["type"] == "function_call_output")
        visible = "\n".join(part["text"] for part in output if part["type"] == "input_text")
        assert len(visible) <= 1000
        assert "truncated" in visible and "a" * 64 in visible
        assert any(part["type"] == "input_image" for part in output)
        live = next(item for item in messages if item.role == "tool")
        assert "data" not in live.metadata
        assert text in Path(live.metadata["output_artifact"]["path"]).read_text()
