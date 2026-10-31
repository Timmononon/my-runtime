"""Offline scenarios for opt-in readiness, continuation and bounded execution."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from aster.domain import Message, ModelTurn, ToolCall, ToolResult, ToolSpec, Usage
from aster.engine import Engine, FakeModel, RunConfig
from aster.engine.task_control import POLICY, TaskController
from aster.policy import PermissionPolicy
from aster.service.coordinator import _SYSTEM_PREAMBLE, RunCoordinator
from aster.state import ReconciliationRequired, Store
from aster.tools.registry import Registry


class Operation:
    def __init__(self, name="deliver", *, uncertain=False):
        self.spec = ToolSpec(name, "Perform the requested operation", {
            "type": "object", "properties": {"value": {"type": "string"}},
            "required": ["value"], "additionalProperties": False,
        }, risk="write")
        self.calls, self.uncertain = [], uncertain

    async def execute(self, arguments, context):
        self.calls.append(arguments["value"])
        if self.uncertain:
            return ToolResult("Delivery may have occurred", True, "transport", "unknown")
        return ToolResult(json.dumps({"delivered": arguments["value"], "id": "result-17"}))


def decision(kind, *, reply="", pending=(), next_tools=(), sources=(), goal="Deliver the requested item"):
    return ModelTurn(json.dumps({
        "decision": kind, "goal": goal, "pending_fields": list(pending),
        "parameter_sources": list(sources), "completed_steps": ["Requested operation completed"] if kind == "complete" else [],
        "remaining_steps": ["Perform the requested operation"] if kind == "execute" else [],
        "next_tools": list(next_tools), "reply": reply,
    }), usage=Usage(input_tokens=12, output_tokens=4))


def action(name="deliver", value="Michael", cid="action-1"):
    return ModelTurn(tool_calls=[ToolCall(cid, name, {"value": value})], finish_reason="tool_calls",
                     usage=Usage(input_tokens=20, output_tokens=5))


@pytest.fixture
def runtime(tmp_path):
    store = Store(tmp_path / "runtime.db")
    sid = store.create_session(tmp_path)["id"]
    yield store, sid
    store.close()


def engine(store, model, tools, **changes):
    return Engine(model, Registry(tools), store,
        config=RunConfig(task_control=POLICY, semantic_compaction=False, model_retries=0, **changes),
        policy=PermissionPolicy(allowed_tools={tool.spec.name for tool in tools}))


async def test_missing_recipient_asks_before_any_preparatory_operation(runtime):
    store, sid = runtime
    tool = Operation()
    model = FakeModel([decision("clarify", reply="Who should receive it?", pending=["recipient"])])
    result = await engine(store, model, [tool]).run(sid, "Deliver the update to my friend")
    assert result.status == "completed" and result.text == "Who should receive it?"
    assert tool.calls == [] and len(model.calls) == 1 and model.calls[0]["tools"] == []
    assert [message.role for message in store.history(sid)] == ["user", "assistant"]
    state = store.get_checkpoint(sid)["metadata"]["task_control"]
    assert state["current_state"]["pending_fields"] == ["recipient"]
    assert state["controller_requests"] == 1 and state["main_requests"] == 0
    assert store.run_budget(result.run_id)["iterations"] == store.run_budget(result.run_id)["model_requests"] == 1


async def test_reply_resumes_pending_goal_and_retains_auditable_parameter_source(runtime):
    store, sid = runtime
    tool = Operation()

    def resume(messages, tools):
        payload = json.loads(messages[0].content)
        assert tools == []
        assert payload["previous_state_unverified"]["pending_fields"] == ["recipient"]
        source = next(item["ref"] for item in reversed(payload["evidence"]) if item["role"] == "user")
        return decision("execute", next_tools=["deliver"], sources=[{"parameter": "recipient", "source_refs": [source]}])

    model = FakeModel([decision("clarify", reply="Who should receive it?", pending=["recipient"]),
                       resume, action(), decision("complete", reply="Delivered to Michael.")])
    runner = engine(store, model, [tool])
    first = await runner.run(sid, "Deliver the update to my friend")
    original = [(message.role, message.content) for message in store.history(sid)]
    second = await runner.run(sid, "Michael")
    assert first.status == second.status == "completed" and tool.calls == ["Michael"]
    assert [(message.role, message.content) for message in store.history(sid)][:2] == original
    state = store.get_checkpoint(sid)["metadata"]["task_control"]
    assert state["controller_requests"] == 2 and state["main_requests"] == 1
    assert len(store.usage(second.run_id)) == 3
    assert store.run_budget(second.run_id)["iterations"] == 3
    assert model.calls[2]["tools"][0].name == "deliver"


async def test_new_capability_invalidates_previous_unsupported_note(runtime):
    store, sid = runtime
    old = Operation("inspect")
    first_model = FakeModel([decision("unsupported", reply="The creation tool is unavailable.")])
    await engine(store, first_model, [old]).run(sid, "Create the requested item")
    created = Operation("create")

    def resumed(messages, tools):
        payload = json.loads(messages[0].content)
        assert payload["schema_changed"] and payload["added_tools"] == ["create"]
        assert payload["previous_state_unverified"]["decision"] == "unsupported"
        return decision("execute", next_tools=["create"])

    model = FakeModel([resumed, action("create", "item"), decision("complete", reply="Created.")])
    result = await engine(store, model, [old, created]).run(sid, "The creation tool is now available")
    assert result.status == "completed" and created.calls == ["item"] and old.calls == []


async def test_observed_completion_stops_optional_followup_mutation(runtime):
    store, sid = runtime
    start, cleanup = Operation("start"), Operation("cleanup")
    model = FakeModel([decision("execute", next_tools=["start"]), action("start", "engine"),
                       decision("complete", reply="The engine has started.")])
    result = await engine(store, model, [start, cleanup]).run(sid, "Start the engine")
    assert result.status == "completed" and start.calls == ["engine"] and cleanup.calls == []
    assert [spec.name for spec in model.calls[1]["tools"]] == ["start"]
    assert len(model.calls) == 3


async def test_textual_action_success_claim_returns_to_control_before_completion(runtime):
    store, sid = runtime
    tool = Operation()

    def recheck(messages, tools):
        evidence = json.loads(messages[0].content)["evidence"]
        assert tools == [] and evidence[-1]["role"] == "assistant"
        assert evidence[-1]["content"] == "The update was delivered."
        assert not any(item["role"] == "tool" for item in evidence)
        return decision("execute", next_tools=["deliver"])

    model = FakeModel([decision("execute", next_tools=["deliver"]), ModelTurn("The update was delivered."),
        recheck, action(), decision("complete", reply="Delivered, confirmed by the tool result.")])
    result = await engine(store, model, [tool]).run(sid, "Deliver the update to Michael")
    assert result.status == "completed" and tool.calls == ["Michael"]
    assert result.text == "Delivered, confirmed by the tool result." and len(model.calls) == 5
    history = store.history(sid)
    assert history[1].content == "The update was delivered." and not history[1].tool_calls
    trace = store.get_checkpoint(sid)["metadata"]["task_control"]
    assert trace["controller_requests"] == 3 and trace["main_requests"] == 2
    assert store.run_budget(result.run_id)["iterations"] == 5


async def test_hallucinated_registered_but_unselected_tool_cannot_execute(runtime):
    store, sid = runtime
    intended, other = Operation("intended"), Operation("other")
    model = FakeModel([decision("execute", next_tools=["intended"]), action("other", "wrong"),
                       decision("clarify", reply="Which operation is intended?", pending=["operation"])])
    result = await engine(store, model, [intended, other]).run(sid, "Do the intended operation")
    assert result.status == "completed" and not intended.calls and not other.calls
    proposed = next(message for message in store.history(sid) if message.tool_calls)
    assert proposed.tool_calls[0].name == "other"
    rejected = next(message for message in store.history(sid) if message.role == "tool")
    assert rejected.metadata["error_category"] == "task_control"


async def test_controller_and_native_action_share_twenty_request_and_iteration_steps(runtime):
    store, sid = runtime
    tool = Operation()
    native_count = 0

    def respond(messages, tools):
        nonlocal native_count
        if not tools:
            return decision("execute", next_tools=["deliver"])
        native_count += 1
        return action(value=str(native_count), cid=f"native-{native_count}")

    model = FakeModel([respond] * 25)
    result = await engine(store, model, [tool], max_iterations=20, max_model_requests=200, duplicate_limit=100).run(sid, "Continue")
    assert result.status == "budget_exhausted"
    assert len(model.calls) == len(store.usage(result.run_id)) == 20 and len(tool.calls) == 10
    assert store.run_budget(result.run_id)["model_requests"] == store.run_budget(result.run_id)["iterations"] == 20
    state = store.get_checkpoint(sid)["metadata"]["task_control"]
    assert state["controller_requests"] == state["main_requests"] == 10


async def test_source_reference_is_validated_but_derived_values_are_not_keyword_matched(runtime):
    store, sid = runtime
    tool = Operation()

    def planned(messages, tools):
        source = next(item["ref"] for item in json.loads(messages[0].content)["evidence"] if item["role"] == "user")
        return decision("execute", next_tools=["deliver"], sources=[{"parameter": "value", "source_refs": [source]}])

    model = FakeModel([planned, action(value="0.6096"), decision("complete", reply="Converted and recorded.")])
    result = await engine(store, model, [tool]).run(sid, "Record two feet in metres")
    assert result.status == "completed" and tool.calls == ["0.6096"]


@pytest.mark.parametrize("bad", [ModelTurn("not json"), decision("execute", next_tools=["not_offered"]),
    decision("execute", next_tools=["deliver"], sources=[{"parameter": "value", "source_refs": ["invented-ref"]}])])
async def test_invalid_control_response_fails_without_tool_effects(runtime, bad):
    store, sid = runtime
    tool = Operation()
    result = await engine(store, FakeModel([bad]), [tool]).run(sid, "Deliver")
    assert result.status == "failed" and result.error.startswith("TaskControlError:") and tool.calls == []
    assert len(store.usage(result.run_id)) == 1
    trace = store.get_checkpoint(sid)["metadata"]["task_control"]
    assert trace["controller_requests"] == 1 and trace["main_requests"] == 0
    assert trace["failures"][0]["type"] == "TaskControlError"


async def test_private_control_text_is_not_telemetry_and_notes_redact_credentials(runtime):
    store, sid = runtime
    secret = "private-test-value"
    model = FakeModel([decision("clarify", goal=f"Authenticate with {secret}",
        pending=["recipient"], reply=f"Who should receive it? Do not repeat {secret}.")])
    result = await engine(store, model, []).run(sid, f"Use password '{secret}' to deliver")
    assert result.status == "completed" and secret not in result.text
    assert secret not in json.dumps(store.events(run_id=result.run_id))
    assert secret not in json.dumps(store.get_checkpoint(sid)["metadata"]["task_control"])
    assert secret in store.history(sid)[0].content


def test_json_user_and_tool_credentials_are_redacted_without_rewriting_evidence():
    user_secret, tool_secret, nested_secret = "user-private-value", "tool-private-value", "nested-private-value"
    messages = [Message("user", json.dumps({"password": user_secret,
        "notes": f"access token '{nested_secret}'"})),
        Message("tool", json.dumps({"result": {"access_token": tool_secret}}), tool_call_id="prior-call")]
    before = [message.content for message in messages]
    control = TaskController("session", "run")
    control.request(messages, [])
    state = control.accept(decision("clarify", pending=["recipient"],
        goal=f"Use {user_secret} and {tool_secret}", reply=f"Provide recipient for {nested_secret}."))
    saved = json.dumps(state)
    assert all(secret not in saved for secret in (user_secret, tool_secret, nested_secret))
    assert [message.content for message in messages] == before


async def test_cancelled_control_call_never_dispatches_and_unknown_effect_is_not_replayed(runtime):
    store, sid = runtime
    tool = Operation()
    entered = asyncio.Event()

    async def wait(messages, tools):
        entered.set()
        await asyncio.Event().wait()

    runner = engine(store, FakeModel([wait]), [tool])
    running = asyncio.create_task(runner.run(sid, "Deliver"))
    await entered.wait()
    rid = next(iter(runner._active))
    runner.cancel(rid)
    assert (await running).status == "cancelled" and not tool.calls

    uncertain = Operation(uncertain=True)
    model = FakeModel([decision("execute", next_tools=["deliver"]), action()])
    runner = engine(store, model, [uncertain])
    result = await runner.run(sid, "Deliver once")
    assert result.status == "needs_reconciliation" and len(model.calls) == 2 and len(uncertain.calls) == 1
    with pytest.raises(ReconciliationRequired):
        await runner.run(sid, "Try again")
    assert len(model.calls) == 2


async def test_off_mode_retains_legacy_model_sequence_and_no_control_checkpoint(runtime):
    store, sid = runtime
    tool = Operation()
    model = FakeModel([action(), ModelTurn("Done")])
    result = await Engine(model, Registry([tool]), store,
        policy=PermissionPolicy(allowed_tools={"deliver"}), config=RunConfig(task_control="off")).run(sid, "Deliver")
    assert result.status == "completed" and len(model.calls) == 2
    assert [item["raw"]["purpose"] for item in store.usage(result.run_id)] == ["main", "main"]
    assert "task_control" not in store.get_checkpoint(sid)["metadata"]


def test_coordinator_policy_prompt_is_opt_in_and_does_not_advertise_unavailable_discovery(tmp_path):
    coordinator = object.__new__(RunCoordinator)
    coordinator.data_dir = tmp_path
    off = SimpleNamespace(data={"runtime": {"task_control": "off"}}, instructions="Operator instructions")
    enabled = SimpleNamespace(data={"runtime": {"task_control": POLICY}}, instructions="Operator instructions")
    assert coordinator._system(off) == _SYSTEM_PREAMBLE + "Operator instructions"
    text = coordinator._system(enabled)
    assert "Use tool_search to discover" not in text
    assert "currently offered and permitted" in text and text.endswith("Operator instructions")
    with pytest.raises(ValueError, match="task control"):
        RunConfig(task_control="unknown")
