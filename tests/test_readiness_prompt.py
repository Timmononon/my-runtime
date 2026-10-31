"""Offline wiring/safety scenarios; scripted decisions do not measure model skill."""
from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest
import yaml

from aster.domain import ModelTurn, ToolCall, ToolResult, ToolSpec, Usage
from aster.engine import Engine, FakeModel, RunConfig
from aster.engine.readiness_prompt import POLICY, SYSTEM_PREAMBLE, policy_identity
from aster.engine.task_control import CONTROL_SYSTEM
from aster.engine.task_control import POLICY as CONTROL_POLICY
from aster.engine.task_control import SYSTEM_PREAMBLE as CONTROL_PREAMBLE
from aster.policy import PermissionPolicy
from aster.service.coordinator import _SYSTEM_PREAMBLE, RunCoordinator
from aster.state import ReconciliationRequired, Store
from aster.tools.registry import Registry


class Operation:
    def __init__(self, name="deliver", *, risk="write", uncertain=False):
        self.spec = ToolSpec(name, "Controlled fixture operation", {
            "type": "object", "properties": {"value": {"type": "string"}},
            "required": ["value"], "additionalProperties": False,
        }, risk=risk)
        self.calls, self.uncertain = [], uncertain

    async def execute(self, arguments, context):
        self.calls.append(arguments["value"])
        if self.uncertain:
            return ToolResult("Outcome cannot be determined", True, "transport", "unknown")
        return ToolResult(json.dumps({"value": arguments["value"], "id": "contact-17"}))


def action(name="deliver", value="Michael", cid="action-1"):
    return ModelTurn(tool_calls=[ToolCall(cid, name, {"value": value})], finish_reason="tool_calls",
                     usage=Usage(input_tokens=20, output_tokens=5))


@pytest.fixture
def runtime(tmp_path):
    store = Store(tmp_path / "runtime.db")
    yield store, store.create_session(tmp_path)["id"]
    store.close()


def engine(store, model, tools, **changes):
    return Engine(model, Registry(tools), store,
        config=RunConfig(prompt_policy=POLICY, semantic_compaction=False, model_retries=0, **changes),
        policy=PermissionPolicy(allowed_tools={tool.spec.name for tool in tools}))


def assert_native_requests(store, run_id, count):
    assert [item["raw"]["purpose"] for item in store.usage(run_id)] == ["main"] * count
    budget = store.run_budget(run_id)
    assert budget["model_requests"] == budget["iterations"] == count


def test_policy_is_explicit_mutually_exclusive_and_legacy_prefixes_remain_exact(tmp_path):
    coordinator = object.__new__(RunCoordinator)
    coordinator.data_dir = tmp_path
    suffix = "Operator instructions remain unchanged"
    for settings, expected in [({}, _SYSTEM_PREAMBLE),
            ({"runtime": {"prompt_policy": "off"}}, _SYSTEM_PREAMBLE),
            ({"runtime": {"task_control": CONTROL_POLICY}}, CONTROL_PREAMBLE),
            ({"runtime": {"prompt_policy": POLICY}}, SYSTEM_PREAMBLE)]:
        assert coordinator._system(SimpleNamespace(data=settings, instructions=suffix)) == expected + suffix
    assert RunConfig().prompt_policy == "off"
    for value in ("unknown", True, None):
        with pytest.raises(ValueError, match="prompt policy"):
            RunConfig(prompt_policy=value)
    with pytest.raises(ValueError, match="combined"):
        RunConfig(prompt_policy=POLICY, task_control=CONTROL_POLICY)
    assert policy_identity() == {"policy": POLICY,
        "prompt_sha256": hashlib.sha256(SYSTEM_PREAMBLE.encode("utf-8")).hexdigest()}


@pytest.mark.parametrize("runtime_settings,expected_prefix", [
    ({}, _SYSTEM_PREAMBLE),
    ({"prompt_policy": POLICY}, SYSTEM_PREAMBLE),
    ({"task_control": CONTROL_POLICY}, CONTROL_PREAMBLE),
])
async def test_actual_coordinator_pins_prefix_and_uses_one_expected_request(tmp_path, runtime_settings, expected_prefix):
    runtime_settings = {"semantic_compaction": False, "model_retries": 0, **runtime_settings}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"runtime": runtime_settings}), encoding="utf-8")
    question = "Who should receive the update?"
    response = ModelTurn(question)
    if runtime_settings.get("task_control") == CONTROL_POLICY:
        response = ModelTurn(json.dumps({"decision": "clarify", "goal": "Deliver the update",
            "pending_fields": ["recipient"], "parameter_sources": [], "completed_steps": [],
            "remaining_steps": [], "next_tools": [], "reply": question}))
    model = FakeModel([response])
    coordinator = RunCoordinator(tmp_path, tools=[], model_factory=lambda _: model)
    try:
        sid = coordinator.create_session(tmp_path)["id"]
        run = await coordinator.submit(sid, "Deliver the update to my friend")
        result = await coordinator.wait(run["id"], timeout=3)
        assert result.status == "completed" and result.text == question
        assert len(model.calls) == 1 and model.calls[0]["system"].startswith(expected_prefix)
        captured = coordinator.store.get_run(run["id"])
        assert captured["system"].startswith(expected_prefix)
        assert captured["config"]["prompt_policy"] == runtime_settings.get("prompt_policy", "off")
        expected_purpose = "task_control" if runtime_settings.get("task_control") == CONTROL_POLICY else "main"
        assert [item["raw"]["purpose"] for item in coordinator.store.usage(run["id"])] == [expected_purpose]
        if expected_purpose == "main":
            assert model.calls[0]["system"] == captured["system"]
            assert "task_control" not in coordinator.store.get_checkpoint(sid)["metadata"]
        else:
            assert model.calls[0]["system"] == captured["system"] + "\n\n" + CONTROL_SYSTEM
    finally:
        await coordinator.close()


async def test_plain_clarification_keeps_native_schemas_and_original_history(runtime):
    store, sid = runtime
    lookup, deliver = Operation("lookup", risk="read"), Operation()
    model = FakeModel([ModelTurn("Who should receive it?")])
    result = await engine(store, model, [lookup, deliver]).run(
        sid, "Deliver to my friend", system=SYSTEM_PREAMBLE)
    assert result.status == "completed" and lookup.calls == deliver.calls == []
    assert {spec.name for spec in model.calls[0]["tools"]} == {"lookup", "deliver"}
    assert [(item.role, item.content) for item in store.history(sid)] == [
        ("user", "Deliver to my friend"), ("assistant", "Who should receive it?")]
    assert_native_requests(store, result.run_id, 1)
    assert "task_control" not in store.get_checkpoint(sid)["metadata"]


async def test_supplied_choice_can_use_tool_derived_identifier_without_a_new_controller(runtime):
    store, sid = runtime
    lookup, deliver = Operation("lookup", risk="read"), Operation()

    def derived(messages, tools):
        observed = json.loads(next(item.content for item in reversed(messages) if item.role == "tool"))
        assert {tool.name for tool in tools} == {"lookup", "deliver"}
        return action(value=observed["id"], cid="deliver-derived")

    model = FakeModel([ModelTurn("Who should receive it?"), action("lookup"), derived, ModelTurn("Delivered")])
    runner = engine(store, model, [lookup, deliver])
    first = await runner.run(sid, "Deliver the update to my friend", system=SYSTEM_PREAMBLE)
    before = [(message.role, message.content) for message in store.history(sid)]
    second = await runner.run(sid, "Michael", system=SYSTEM_PREAMBLE)
    assert first.status == second.status == "completed"
    assert lookup.calls == ["Michael"] and deliver.calls == ["contact-17"]
    assert [(message.role, message.content) for message in store.history(sid)][:2] == before
    assert_native_requests(store, first.run_id, 1)
    assert_native_requests(store, second.run_id, 3)
    assert all(call["system"] == SYSTEM_PREAMBLE for call in model.calls)


async def test_authorized_partial_work_is_not_blocked_by_a_semantic_gate(runtime):
    store, sid = runtime
    lookup = Operation("lookup", risk="read")
    model = FakeModel([action("lookup", "available detail"), ModelTurn("The lookup is complete; delivery remains unavailable.")])
    result = await engine(store, model, [lookup]).run(sid,
        "Delivery is not available. I authorize just the lookup now.", system=SYSTEM_PREAMBLE)
    assert result.status == "completed" and lookup.calls == ["available detail"]
    assert_native_requests(store, result.run_id, 2)


async def test_existing_tool_permissions_still_reject_a_native_proposal(runtime):
    store, sid = runtime
    tool = Operation()
    model = FakeModel([action(), ModelTurn("Operation denied")])
    runner = Engine(model, Registry([tool]), store, config=RunConfig(prompt_policy=POLICY,
        semantic_compaction=False, model_retries=0), policy=PermissionPolicy(denied_tools={"deliver"}))
    result = await runner.run(sid, "Deliver the update", system=SYSTEM_PREAMBLE)
    assert result.status == "completed" and tool.calls == []
    assert any(message.role == "assistant" and message.tool_calls for message in store.history(sid))
    assert any(message.role == "tool" and message.metadata["is_error"] for message in store.history(sid))
    assert_native_requests(store, result.run_id, 2)


async def test_native_calls_keep_the_existing_twenty_step_budget(runtime):
    store, sid = runtime
    tool = Operation()
    model = FakeModel([action(value=str(index), cid=f"action-{index}") for index in range(25)])
    result = await engine(store, model, [tool], max_iterations=20, max_model_requests=20,
                          duplicate_limit=100).run(sid, "Continue", system=SYSTEM_PREAMBLE)
    assert result.status == "budget_exhausted" and len(model.calls) == len(tool.calls) == 20
    assert_native_requests(store, result.run_id, 20)


async def test_unknown_effect_stops_without_replay(runtime):
    store, sid = runtime
    tool = Operation(uncertain=True)
    model = FakeModel([action(), action(cid="must-not-run")])
    runner = engine(store, model, [tool])
    result = await runner.run(sid, "Deliver once", system=SYSTEM_PREAMBLE)
    assert result.status == "needs_reconciliation" and len(model.calls) == len(tool.calls) == 1
    with pytest.raises(ReconciliationRequired):
        await runner.run(sid, "Repeat", system=SYSTEM_PREAMBLE)
    assert len(model.calls) == len(tool.calls) == 1


async def test_cancellation_before_model_completion_cannot_dispatch(runtime):
    store, sid = runtime
    tool, entered = Operation(), asyncio.Event()

    async def pending(messages, tools):
        entered.set()
        await asyncio.Event().wait()
        return action()

    model = FakeModel([pending])
    runner = engine(store, model, [tool])
    task = asyncio.create_task(runner.run(sid, "Deliver", system=SYSTEM_PREAMBLE))
    await entered.wait()
    run_id = next(iter(runner._active))
    runner.cancel(run_id)
    result = await task
    assert result.status == "cancelled" and tool.calls == [] and len(model.calls) == 1


async def test_captured_automation_keeps_policy_after_global_setting_changes(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"runtime": {"prompt_policy": POLICY,
        "semantic_compaction": False, "compaction_persist_learnings": False}}), encoding="utf-8")
    model = FakeModel([ModelTurn("Which recipient?")])
    coordinator = RunCoordinator(tmp_path, tools=[], model_factory=lambda _: model)
    try:
        sid = coordinator.create_session(tmp_path)["id"]
        captured = coordinator.capture_automation_execution(sid)
        assert captured["system"].startswith(SYSTEM_PREAMBLE)
        config_path.write_text(yaml.safe_dump({"runtime": {"prompt_policy": "off"}}), encoding="utf-8")
        # Exercise older captured jobs without the explicit notes-free field.
        captured.pop("system_without_session_notes")
        run = await coordinator.submit(sid, "Deliver to my friend", execution_snapshot=captured)
        assert (await coordinator.wait(run["id"], timeout=3)).status == "completed"
        assert model.calls[0]["system"].startswith(SYSTEM_PREAMBLE)
        assert coordinator.store.get_run(run["id"])["config"]["prompt_policy"] == POLICY
    finally:
        await coordinator.close()
