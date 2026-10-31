"""Offline gate wiring and safety; scripted verdicts do not measure semantics."""
from __future__ import annotations

import asyncio
import json

import pytest

from aster.domain import Message, ModelTurn, ToolCall, ToolResult, ToolSpec, Usage
from aster.engine import Engine, FakeModel, RunConfig
from aster.engine.capability_gate import CHECK_SYSTEM, POLICY, CapabilityGate, digest, policy_identity
from aster.engine.readiness_prompt import POLICY as NATIVE_POLICY
from aster.engine.readiness_prompt import SYSTEM_PREAMBLE as NATIVE_SYSTEM
from aster.policy import PermissionPolicy
from aster.state import ReconciliationRequired, Store
from aster.tools.registry import Registry


class Operation:
    def __init__(self, name="submit", *, unknown=False):
        self.spec = ToolSpec(name, "Perform the requested operation", {
            "type": "object", "properties": {"value": {"type": "string"}},
            "required": ["value"], "additionalProperties": False,
        }, risk="write")
        self.calls, self.unknown = [], unknown

    async def execute(self, arguments, context):
        self.calls.append(arguments["value"])
        return (ToolResult("Uncertain delivery", True, "transport", "unknown") if self.unknown
                else ToolResult(json.dumps({"accepted": arguments["value"]})))


def verdict(status="ready", reply=""):
    return ModelTurn(json.dumps({"status": status, "reply": reply}),
                     usage=Usage(input_tokens=30, output_tokens=8))


def action(name="submit", value="report", cid="call-1"):
    return ModelTurn(tool_calls=[ToolCall(cid, name, {"value": value})], finish_reason="tool_calls",
                     usage=Usage(input_tokens=20, output_tokens=5))


def runner(store, model, tools=(), **changes):
    config = RunConfig(capability_gate=POLICY, prompt_policy=NATIVE_POLICY,
                       semantic_compaction=False, model_retries=0, **changes)
    return Engine(model, Registry(tools), store, config=config,
                  policy=PermissionPolicy(allowed_tools={tool.spec.name for tool in tools}))


@pytest.fixture
def runtime(tmp_path):
    store = Store(tmp_path / "runtime.db")
    yield store, store.create_session(tmp_path)["id"]
    store.close()


def purposes(store, run_id):
    return [row["raw"]["purpose"] for row in store.usage(run_id)]


def state(store, sid):
    return store.get_checkpoint(sid)["metadata"]["capability_gate"]


def test_configuration_is_opt_in_and_cannot_be_mixed_with_old_control():
    assert RunConfig().capability_gate == "off"
    for value in (None, True, "unknown"):
        with pytest.raises(ValueError, match="capability gate"):
            RunConfig(capability_gate=value)
    with pytest.raises(ValueError, match="requires"):
        RunConfig(capability_gate=POLICY)
    assert RunConfig(capability_gate=POLICY, prompt_policy=NATIVE_POLICY).task_control == "off"
    assert policy_identity()["native_prompt_policy"] == NATIVE_POLICY


async def test_missing_capability_ends_with_no_business_call_or_private_transcript(runtime):
    store, sid = runtime
    query = Operation("query")

    def check(messages, tools):
        payload = json.loads(messages[0].content)
        assert tools == [] and payload["tools"][0]["name"] == "query"
        assert payload["tools_sha256"] == digest(payload["tools"])
        assert payload["conversation"][0]["content"] == "Prepare and submit the report"
        return verdict("missing_capability", "The submission capability is unavailable.")

    model = FakeModel([check])
    result = await runner(store, model, [query]).run(sid, "Prepare and submit the report", system=NATIVE_SYSTEM)
    assert result.status == "completed" and query.calls == []
    assert model.calls[0]["system"] == NATIVE_SYSTEM + "\n\n" + CHECK_SYSTEM
    assert purposes(store, result.run_id) == ["capability_check"]
    assert [(m.role, m.content) for m in store.history(sid)] == [
        ("user", "Prepare and submit the report"), ("assistant", result.text)]
    saved = state(store, sid)
    assert saved["check_requests"] == 1 and saved["main_requests"] == 0
    assert saved["current"]["status"] == "missing_capability"
    assert saved["current"]["parse_status"] == "ok" and saved["current"]["request_number"] == 1


async def test_ready_enters_unchanged_native_loop_and_does_not_recheck_each_tool(runtime):
    store, sid = runtime
    tool = Operation()
    model = FakeModel([verdict(), action(), action(value="appendix", cid="call-2"), ModelTurn("Submitted")])
    result = await runner(store, model, [tool]).run(sid, "Submit report and appendix", system=NATIVE_SYSTEM)
    assert result.status == "completed" and tool.calls == ["report", "appendix"]
    assert purposes(store, result.run_id) == ["capability_check", "main", "main", "main"]
    assert all(call["system"] == NATIVE_SYSTEM for call in model.calls[1:])
    assert all([spec.name for spec in call["tools"]] == ["submit"] for call in model.calls[1:])
    assert len(state(store, sid)["checks"]) == 1


@pytest.mark.parametrize("bad", [
    ModelTurn("not JSON"),
    ModelTurn('{"status":"ready","status":"missing_capability","reply":"missing"}'),
    ModelTurn('{"status":"ready","reply":""}', finish_reason="length"),
    action(name="private_check_proposal", cid="private-gate"),
    ModelTurn("x" * 6001),
])
async def test_bad_verdict_falls_back_to_native_instead_of_zero_call_abstention(runtime, bad):
    store, sid = runtime
    tool = Operation()
    model = FakeModel([bad, action(), ModelTurn("Submitted")])
    result = await runner(store, model, [tool]).run(sid, "Submit report", system=NATIVE_SYSTEM)
    assert result.status == "completed" and tool.calls == ["report"]
    assert purposes(store, result.run_id) == ["capability_check", "main", "main"]
    saved = state(store, sid)["current"]
    assert saved["status"] == "fallback_to_native" and saved["parse_status"] != "ok"
    if bad.text:
        assert not any(m.content == bad.text for m in store.history(sid))
    assert not any(call.id == "private-gate" for message in store.history(sid) for call in message.tool_calls)


async def test_current_schemas_are_rechecked_on_new_user_turn_after_tools_restore(runtime):
    store, sid = runtime
    query = Operation("query")
    first = await runner(store, FakeModel([verdict("missing_capability", "Submission is unavailable.")]), [query]).run(
        sid, "Submit the report", system=NATIVE_SYSTEM)
    tool = Operation()

    def recheck(messages, tools):
        payload = json.loads(messages[0].content)
        assert tools == [] and {item["name"] for item in payload["tools"]} == {"query", "submit"}
        assert payload["conversation"][0]["content"] == "Submit the report"
        assert payload["conversation"][-1]["content"] == "The submission tool is now available"
        return verdict()

    model = FakeModel([recheck, action(), ModelTurn("Submitted")])
    second = await runner(store, model, [query, tool]).run(sid, "The submission tool is now available", system=NATIVE_SYSTEM)
    assert first.status == second.status == "completed" and tool.calls == ["report"]
    assert state(store, sid)["run_id"] == second.run_id and len(state(store, sid)["checks"]) == 1


async def test_partial_authorization_and_missing_information_still_use_native_decisions(runtime):
    store, sid = runtime
    query = Operation("query")
    model = FakeModel([verdict(), action("query"), ModelTurn("Prepared; submission is unavailable.")])
    first = await runner(store, model, [query]).run(
        sid, "If submission is unavailable, prepare just the report now", system=NATIVE_SYSTEM)
    assert first.status == "completed" and query.calls == ["report"]
    question = FakeModel([verdict(), ModelTurn("Which report should I submit?")])
    second = await runner(store, question, [Operation()]).run(sid, "Submit the report", system=NATIVE_SYSTEM)
    assert second.status == "completed" and second.text == "Which report should I submit?"
    assert purposes(store, second.run_id) == ["capability_check", "main"]


async def test_gate_and_main_share_twenty_requests_and_iterations(runtime):
    store, sid = runtime
    tool = Operation()
    model = FakeModel([verdict(), *[action(value=str(i), cid=f"c-{i}") for i in range(25)]])
    result = await runner(store, model, [tool], max_iterations=20, max_model_requests=200,
                          duplicate_limit=100).run(sid, "Continue submitting", system=NATIVE_SYSTEM)
    assert result.status == "budget_exhausted" and len(tool.calls) == 19
    assert purposes(store, result.run_id) == ["capability_check"] + ["main"] * 19
    assert store.run_budget(result.run_id)["iterations"] == store.run_budget(result.run_id)["model_requests"] == 20


async def test_measured_helper_tokens_can_prevent_native_dispatch(runtime):
    store, sid = runtime
    checked = verdict()
    checked.usage = Usage(input_tokens=300000, output_tokens=10)
    model = FakeModel([checked, action()])
    tool = Operation()
    result = await runner(store, model, [tool]).run(sid, "Submit report", system=NATIVE_SYSTEM)
    assert result.status == "budget_exhausted" and tool.calls == [] and len(model.calls) == 1
    assert purposes(store, result.run_id) == ["capability_check"]


async def test_effective_denied_tools_do_not_appear_in_check_payload(runtime):
    store, sid = runtime
    tool = Operation()

    def check(messages, tools):
        assert json.loads(messages[0].content)["tools"] == []
        return verdict("missing_capability", "Submission is unavailable.")

    model = FakeModel([check])
    engine = runner(store, model, [tool])
    engine.policy.denied_tools.add("submit")
    result = await engine.run(sid, "Submit report", system=NATIVE_SYSTEM)
    assert result.status == "completed" and tool.calls == []


async def test_cancelled_check_does_not_fall_back_or_dispatch(runtime):
    store, sid = runtime
    entered = asyncio.Event()

    async def pending(messages, tools):
        entered.set()
        await asyncio.Event().wait()

    tool, model = Operation(), FakeModel([pending, action()])
    engine = runner(store, model, [tool])
    task = asyncio.create_task(engine.run(sid, "Submit report", system=NATIVE_SYSTEM))
    await entered.wait()
    engine.cancel(next(iter(engine._active)))
    result = await task
    assert result.status == "cancelled" and len(model.calls) == 1 and tool.calls == []
    assert state(store, sid)["current"]["status"] == "pending"


async def test_steering_rechecks_new_direction_without_applying_old_verdict(runtime):
    store, sid = runtime
    entered = asyncio.Event()

    async def pending(messages, tools):
        entered.set()
        await asyncio.Event().wait()

    def new_check(messages, tools):
        assert json.loads(messages[0].content)["conversation"][-1]["content"] == "Only explain the report"
        return verdict()

    model = FakeModel([pending, new_check, ModelTurn("Explanation")])
    tool, engine = Operation(), None
    engine = runner(store, model, [tool])
    task = asyncio.create_task(engine.run(sid, "Submit report", system=NATIVE_SYSTEM))
    await entered.wait()
    rid = next(iter(engine._active))
    engine.inject(sid, "Only explain the report")
    assert engine.steer(rid)
    result = await task
    assert result.status == "completed" and result.text == "Explanation" and tool.calls == []
    assert purposes(store, rid) == ["capability_check", "capability_check", "main"]
    assert len(state(store, sid)["checks"]) == 2


async def test_unknown_business_outcome_still_blocks_followup_and_replay(runtime):
    store, sid = runtime
    tool = Operation(unknown=True)
    model = FakeModel([verdict(), action(), action(cid="must-not-run")])
    engine = runner(store, model, [tool])
    result = await engine.run(sid, "Submit once", system=NATIVE_SYSTEM)
    assert result.status == "needs_reconciliation" and tool.calls == ["report"]
    with pytest.raises(ReconciliationRequired):
        await engine.run(sid, "Try again", system=NATIVE_SYSTEM)
    assert len(model.calls) == 2


async def test_manual_recovery_reuses_finished_gate_without_replaying_effect(runtime):
    store, sid = runtime
    tool = Operation()

    def fail(messages, tools):
        raise RuntimeError("Synthetic failed native response")

    first = await runner(store, FakeModel([verdict(), action(), fail]), [tool]).run(
        sid, "Submit once", system=NATIVE_SYSTEM)
    assert first.status == "failed" and tool.calls == ["report"]
    replacement = FakeModel([ModelTurn("Submission confirmed")])
    restored = await runner(store, replacement, [tool]).recover(first.run_id)
    assert restored.status == "completed" and len(replacement.calls) == 1 and tool.calls == ["report"]
    assert purposes(store, first.run_id).count("capability_check") == 1


async def test_transport_failure_is_not_a_format_fallback_or_an_automatic_retry(runtime):
    store, sid = runtime

    class RetryableTransportError(RuntimeError):
        retryable, category = True, "transport"

    def failed_transport(messages, tools):
        raise RetryableTransportError("Synthetic transport failed")

    model, tool = FakeModel([failed_transport, action()]), Operation()
    engine = runner(store, model, [tool])
    engine.config.model_retries = 2
    result = await engine.run(sid, "Submit report", system=NATIVE_SYSTEM)
    assert result.status == "failed" and len(model.calls) == 1 and tool.calls == []
    assert purposes(store, result.run_id) == ["capability_check"]
    assert state(store, sid)["current"]["status"] == "pending"
    assert store.usage(result.run_id)[0]["raw"]["status"] == "outcome_unknown"


async def test_manual_recovery_with_lost_verdict_uses_native_fallback_without_rechecking(runtime, monkeypatch):
    store, sid = runtime
    original_checkpoint = store.checkpoint

    def fail_after_verdict(session_id, run_id, messages, *, metadata=None):
        current = (metadata or {}).get("capability_gate", {}).get("current") or {}
        if current.get("status") == "ready":
            raise RuntimeError("Synthetic loss before verdict checkpoint")
        return original_checkpoint(session_id, run_id, messages, metadata=metadata)

    monkeypatch.setattr(store, "checkpoint", fail_after_verdict)
    first = await runner(store, FakeModel([verdict()])).run(sid, "Explain the report", system=NATIVE_SYSTEM)
    assert first.status == "failed" and state(store, sid)["current"]["status"] == "pending"
    assert store.usage(first.run_id)[0]["raw"]["status"] == "response_received"
    monkeypatch.setattr(store, "checkpoint", original_checkpoint)
    replacement = FakeModel([ModelTurn("Native explanation")])
    restored = await runner(store, replacement).recover(first.run_id)
    assert restored.status == "completed" and len(replacement.calls) == 1
    assert purposes(store, first.run_id) == ["capability_check", "main"]
    assert state(store, sid)["current"]["parse_status"] == "recovered_without_decision"


def test_reply_redacts_known_credentials_and_checkpoint_does_not_store_request_text():
    gate = CapabilityGate("session", "run")
    secret = "fixture-private-password"
    messages = [Message("user", json.dumps({"password": secret})),
                Message("tool", '[Tool metadata]\n{"data":{"auth_token":"fixture-private-token"}}\n\n{}')]
    gate.prepare(messages, [], [])
    status, reply = gate.accept(verdict("missing_capability", f"Missing submit; {secret} fixture-private-token"), request_number=1)
    assert status == "missing_capability" and secret not in reply and "fixture-private-token" not in reply
    serialized = json.dumps(gate.snapshot([]))
    assert secret not in serialized and "fixture-private-token" not in serialized
    assert "conversation" not in serialized


def test_generated_compaction_context_is_not_a_new_user_turn():
    gate = CapabilityGate("session", "run")
    raw_user = Message("user", "Submit the report")
    gate.prepare([raw_user], [], [])
    gate.accept(verdict(), request_number=1)
    summary = Message("user", "Unverified old request summary", metadata={"compaction_summary": True})
    reanchor = Message("user", "Runtime requirements projection", metadata={"compaction_reanchor": True})
    assert gate.prepare([summary, raw_user, reanchor], [], []) is None
    assert len(gate.checks) == 1
