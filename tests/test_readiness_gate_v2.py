"""Readiness-v2 wiring/safety; scripted verdicts do not prove model semantics."""
from __future__ import annotations

import asyncio
import hashlib
import json

import pytest
from test_capability_gate import Operation, action, purposes, state, verdict

from aster.domain import Message, ModelTurn, Usage
from aster.engine import Engine, FakeModel, RunConfig
from aster.engine.capability_gate import POLICY as V1_POLICY
from aster.engine.capability_gate import CapabilityGate, digest
from aster.engine.readiness_gate_v2 import CHECK_SYSTEM, POLICY, ReadinessGate, policy_identity
from aster.engine.readiness_prompt import POLICY as NATIVE_POLICY
from aster.engine.readiness_prompt import SYSTEM_PREAMBLE as NATIVE_SYSTEM
from aster.policy import PermissionPolicy
from aster.state import ReconciliationRequired, Store
from aster.tools.registry import Registry


@pytest.fixture
def runtime(tmp_path):
    store = Store(tmp_path / "runtime.db")
    yield store, store.create_session(tmp_path)["id"]
    store.close()


def runner(store, model, tools=(), **changes):
    config = RunConfig(capability_gate=POLICY, prompt_policy=NATIVE_POLICY,
                       semantic_compaction=False, model_retries=0, **changes)
    return Engine(model, Registry(tools), store, config=config,
                  policy=PermissionPolicy(allowed_tools={tool.spec.name for tool in tools}))


def test_new_policy_is_independent_and_does_not_expand_v1_contract():
    assert RunConfig().capability_gate == "off"
    assert RunConfig(capability_gate=POLICY, prompt_policy=NATIVE_POLICY).task_control == "off"
    with pytest.raises(ValueError, match="requires"):
        RunConfig(capability_gate=POLICY)
    assert policy_identity() == {
        "policy": POLICY,
        "gate_prompt_sha256": hashlib.sha256(CHECK_SYSTEM.encode()).hexdigest(),
        "native_prompt_policy": NATIVE_POLICY,
        "native_prompt_sha256": hashlib.sha256(NATIVE_SYSTEM.encode()).hexdigest(),
        "check_frequency": "once_per_user_turn", "parse_failure_policy": "fallback_to_native",
    }
    v1 = CapabilityGate("session", "run")
    v1.prepare([Message("user", "Submit a report")], [], [])
    status, reply = v1.accept(verdict("needs_clarification", "Which report?"), request_number=1)
    assert status == "fallback_to_native" and reply == ""
    assert v1.snapshot([])["policy"] == V1_POLICY


@pytest.mark.parametrize("status,question", [
    ("needs_clarification", "Who should receive the report?"),
    ("missing_capability", "The delivery capability is unavailable."),
])
async def test_terminal_verdict_has_one_tool_free_request_no_preparation_and_no_json_in_history(runtime, status, question):
    store, sid = runtime
    lookup, delivery = Operation("lookup"), Operation("deliver")

    def check(messages, tools):
        assert tools == []
        raw = messages[0].content
        payload = json.loads(raw)
        assert set(payload) == {"policy", "conversation", "tools", "tools_sha256"}
        assert payload["policy"] == POLICY
        assert payload["conversation"][-1]["content"] == "Deliver the report to my colleague"
        assert {tool["name"] for tool in payload["tools"]} == {"lookup", "deliver"}
        assert payload["tools_sha256"] == digest(payload["tools"])
        assert raw.index('"conversation"') < raw.index('"tools"')
        return verdict(status, question)

    model = FakeModel([check])
    result = await runner(store, model, [lookup, delivery]).run(
        sid, "Deliver the report to my colleague", system=NATIVE_SYSTEM)
    assert result.status == "completed" and result.text == question
    assert lookup.calls == delivery.calls == []
    assert purposes(store, result.run_id) == ["capability_check"]
    assert model.calls[0]["system"] == NATIVE_SYSTEM + "\n\n" + CHECK_SYSTEM
    assert [(message.role, message.content) for message in store.history(sid)] == [
        ("user", "Deliver the report to my colleague"), ("assistant", question)]
    snapshot = state(store, sid)
    assert snapshot["policy"] == POLICY and snapshot["current"]["status"] == status
    assert snapshot["current"]["parse_status"] == "ok"
    assert snapshot["check_requests"] == 1 and snapshot["main_requests"] == 0
    assert store.run_budget(result.run_id)["iterations"] == store.run_budget(result.run_id)["model_requests"] == 1


async def test_supplied_choice_resumes_original_goal_then_native_uses_derived_id(runtime):
    store, sid = runtime
    lookup, delivery = Operation("lookup"), Operation("deliver")
    first = await runner(store, FakeModel([verdict("needs_clarification", "Which colleague?")]),
                         [lookup, delivery]).run(sid, "Deliver the report to my colleague", system=NATIVE_SYSTEM)

    def recheck(messages, tools):
        payload = json.loads(messages[0].content)
        assert payload["conversation"][0]["content"] == "Deliver the report to my colleague"
        assert payload["conversation"][-1]["content"] == "Michael, using the existing directory entry"
        return verdict()

    def derived(messages, tools):
        assert any(message.role == "tool" and '"accepted": "Michael"' in message.content for message in messages)
        return action("deliver", "derived-contact-id", "deliver-1")

    model = FakeModel([recheck, action("lookup", "Michael", "lookup-1"), derived, ModelTurn("Delivered")])
    second = await runner(store, model, [lookup, delivery]).run(
        sid, "Michael, using the existing directory entry", system=NATIVE_SYSTEM)
    assert first.status == second.status == "completed"
    assert lookup.calls == ["Michael"] and delivery.calls == ["derived-contact-id"]
    assert purposes(store, second.run_id) == ["capability_check", "main", "main", "main"]
    assert all(call["system"] == NATIVE_SYSTEM for call in model.calls[1:])
    assert len(state(store, sid)["checks"]) == 1


async def test_authorized_creative_partial_work_still_uses_native_without_another_check(runtime):
    store, sid = runtime
    draft = Operation("draft")
    model = FakeModel([verdict(), action("draft", "newly composed content"), ModelTurn("Draft created")])
    result = await runner(store, model, [draft]).run(
        sid, "Write a short report. If delivery is unavailable, just draft it now.", system=NATIVE_SYSTEM)
    assert result.status == "completed" and draft.calls == ["newly composed content"]
    assert purposes(store, result.run_id) == ["capability_check", "main", "main"]
    assert len(state(store, sid)["checks"]) == 1


async def test_tools_added_next_turn_are_checked_without_inheriting_old_unavailability(runtime):
    store, sid = runtime
    first = await runner(store, FakeModel([verdict("missing_capability", "Submission is unavailable.")])).run(
        sid, "Submit the report", system=NATIVE_SYSTEM)
    submit = Operation()

    def restored(messages, tools):
        payload = json.loads(messages[0].content)
        assert [tool["name"] for tool in payload["tools"]] == ["submit"]
        assert payload["conversation"][0]["content"] == "Submit the report"
        return verdict()

    model = FakeModel([restored, action(), ModelTurn("Submitted")])
    second = await runner(store, model, [submit]).run(sid, "Submission is now available", system=NATIVE_SYSTEM)
    assert first.status == second.status == "completed" and submit.calls == ["report"]
    assert state(store, sid)["run_id"] == second.run_id


@pytest.mark.parametrize("bad", [
    ModelTurn("not JSON"),
    ModelTurn('{"status":"needs_clarification","reply":""}'),
    ModelTurn('{"status":"needs_clarification","status":"ready","reply":"Which report?"}'),
    ModelTurn('{"status":"needs_clarification","reply":"Which report?"}', finish_reason="length"),
    action("unoffered_private_proposal", cid="private-gate-proposal"),
])
async def test_malformed_verdict_spends_slot_and_falls_back_to_native_not_abstention(runtime, bad):
    store, sid = runtime
    submit = Operation()
    model = FakeModel([bad, action(), ModelTurn("Submitted")])
    result = await runner(store, model, [submit]).run(sid, "Submit the report", system=NATIVE_SYSTEM)
    assert result.status == "completed" and submit.calls == ["report"]
    assert purposes(store, result.run_id) == ["capability_check", "main", "main"]
    assert state(store, sid)["current"]["status"] == "fallback_to_native"
    assert state(store, sid)["current"]["parse_status"] != "ok"
    assert not any(call.id == "private-gate-proposal" for message in store.history(sid) for call in message.tool_calls)


async def test_new_gate_and_main_share_twenty_requests_with_nineteen_native_steps(runtime):
    store, sid = runtime
    submit = Operation()
    model = FakeModel([verdict(), *[action(value=str(index), cid=f"call-{index}") for index in range(25)]])
    result = await runner(store, model, [submit], max_iterations=20, max_model_requests=200,
                          duplicate_limit=100).run(sid, "Continue submitting", system=NATIVE_SYSTEM)
    assert result.status == "budget_exhausted" and len(submit.calls) == 19
    assert purposes(store, result.run_id) == ["capability_check"] + ["main"] * 19
    budget = store.run_budget(result.run_id)
    assert budget["iterations"] == budget["model_requests"] == 20


async def test_real_helper_token_exhaustion_stops_before_native_dispatch(runtime):
    store, sid = runtime
    checked = verdict()
    checked.usage = Usage(input_tokens=300000, output_tokens=10)
    submit, model = Operation(), FakeModel([checked, action()])
    result = await runner(store, model, [submit]).run(sid, "Submit the report", system=NATIVE_SYSTEM)
    assert result.status == "budget_exhausted" and submit.calls == [] and len(model.calls) == 1


async def test_cancelled_check_is_not_clarification_fallback(runtime):
    store, sid = runtime
    entered = asyncio.Event()

    async def pending(messages, tools):
        entered.set()
        await asyncio.Event().wait()

    model, submit = FakeModel([pending, action()]), Operation()
    engine = runner(store, model, [submit])
    task = asyncio.create_task(engine.run(sid, "Submit the report", system=NATIVE_SYSTEM))
    await entered.wait()
    engine.cancel(next(iter(engine._active)))
    result = await task
    assert result.status == "cancelled" and submit.calls == [] and len(model.calls) == 1
    assert state(store, sid)["current"]["status"] == "pending"


async def test_transport_failure_remains_failed_without_helper_retry_or_native_fallback(runtime):
    store, sid = runtime

    class RetryableTransportError(RuntimeError):
        retryable, category = True, "transport"

    def failed(messages, tools):
        raise RetryableTransportError("Synthetic transport failure")

    submit, model = Operation(), FakeModel([failed, action()])
    engine = runner(store, model, [submit])
    engine.config.model_retries = 2
    result = await engine.run(sid, "Submit the report", system=NATIVE_SYSTEM)
    assert result.status == "failed" and len(model.calls) == 1 and submit.calls == []
    assert state(store, sid)["current"]["status"] == "pending"
    assert store.usage(result.run_id)[0]["raw"]["status"] == "outcome_unknown"


async def test_steering_after_a_verdict_discards_stale_question(runtime):
    store, sid = runtime
    model = FakeModel([verdict("needs_clarification", "Which report?"), verdict(), ModelTurn("Explanation")])
    engine = runner(store, model)
    steered = False

    async def sink(kind, payload):
        nonlocal steered
        if kind == "capability.check.decision" and not steered:
            steered = True
            engine.inject(sid, "Cancel submission and explain the report instead")
            assert engine.steer(next(iter(engine._active)))

    result = await engine.run(sid, "Submit the report", system=NATIVE_SYSTEM, emit=sink)
    assert result.status == "completed" and result.text == "Explanation"
    assert purposes(store, result.run_id) == ["capability_check", "capability_check", "main"]
    assert not any(message.role == "assistant" and message.content == "Which report?" for message in store.history(sid))
    assert len(state(store, sid)["checks"]) == 2


async def test_unknown_business_effect_still_prevents_followup_or_replay(runtime):
    store, sid = runtime
    submit = Operation(unknown=True)
    model = FakeModel([verdict(), action(), action(cid="do-not-replay")])
    engine = runner(store, model, [submit])
    result = await engine.run(sid, "Submit once", system=NATIVE_SYSTEM)
    assert result.status == "needs_reconciliation" and submit.calls == ["report"]
    with pytest.raises(ReconciliationRequired):
        await engine.run(sid, "Try again", system=NATIVE_SYSTEM)
    assert len(model.calls) == 2


async def test_manual_recovery_reuses_finished_ready_verdict_without_duplicate_helper(runtime):
    store, sid = runtime
    submit = Operation()
    first = await runner(store, FakeModel([verdict(), action(), RuntimeError("Synthetic native failure")]), [submit]).run(
        sid, "Submit once", system=NATIVE_SYSTEM)
    assert first.status == "failed" and submit.calls == ["report"]
    replacement = FakeModel([ModelTurn("Submission confirmed")])
    restored = await runner(store, replacement, [submit]).recover(first.run_id)
    assert restored.status == "completed" and submit.calls == ["report"]
    assert len(replacement.calls) == purposes(store, first.run_id).count("capability_check") == 1


def test_finished_question_survives_checkpoint_and_generated_user_context_without_rechecking():
    gate = ReadinessGate("session", "run")
    user = Message("user", "Submit the report")
    gate.prepare([user], [], [])
    gate.accept(verdict("needs_clarification", "Which report?"), request_number=1)
    snapshot = gate.snapshot([])
    restored = ReadinessGate("session", "run", previous=snapshot)
    summary = Message("user", "Generated summary", metadata={"compaction_summary": True})
    reanchor = Message("user", "Generated requirements", metadata={"compaction_reanchor": True})
    assert restored.prepare([summary, user, reanchor], [], []) is None
    assert restored.current["status"] == "needs_clarification" and len(restored.checks) == 1
    fresh = ReadinessGate("session", "new-run", previous=snapshot)
    assert fresh.prepare([user], [], []) is not None


async def test_private_json_and_known_credentials_do_not_enter_telemetry_or_question(runtime):
    store, sid = runtime
    marker = "opaque-fixture-a"
    checked = verdict("needs_clarification", "Which recipient? " + marker)
    model = FakeModel([checked])
    result = await runner(store, model).run(sid, "Use password '" + marker + "' to deliver", system=NATIVE_SYSTEM)
    assert result.status == "completed"
    assert not any(marker in value for value in [result.text, json.dumps(store.events(run_id=result.run_id)),
                                                json.dumps(state(store, sid))])
    assert checked.text not in json.dumps(store.events(run_id=result.run_id))
    assert marker in store.history(sid)[0].content
