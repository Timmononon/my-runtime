"""Offline scenarios for task ingress, Runtime guards and durable identity."""
import asyncio
import dataclasses
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from aster.api import create_app
from aster.domain import ModelTurn, ToolCall, ToolContext, ToolResult, ToolSpec
from aster.engine import FakeModel
from aster.engine.dispatcher import Dispatcher
from aster.policy import PermissionPolicy
from aster.service import RunCoordinator
from aster.state import StateError, Store
from aster.tools.registry import Registry

TOKEN = "local-task-offline-test-token-32-characters"


class EmptyCredentials:
    def get(self, *args, **kwargs):
        return None

    def resolve(self, reference):
        raise ValueError("No test credentials")


@asynccontextmanager
async def api(tmp_path, *, tools=None, turns=None):
    models = []

    def model_factory(settings):
        model = FakeModel(turns or [ModelTurn("Verified Worker result")])
        model.task_settings = dict(settings)
        models.append(model)
        return model

    coordinator = RunCoordinator(tmp_path / "state", tools=tools or [], credentials=EmptyCredentials(),
                                 model_factory=model_factory)
    app = create_app(coordinator, token=TOKEN, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost",
                                headers={"Authorization": "Bearer " + TOKEN}) as client:
        try:
            yield client, coordinator, models
        finally:
            await coordinator.close()


async def settle(coordinator, ticket_id):
    for _ in range(200):
        await coordinator.tasks.tick()
        ticket = coordinator.tasks.get_ticket(ticket_id)
        if ticket["state"] in {"completed", "failed", "cancelled", "needs_reconciliation"}:
            return ticket
        await asyncio.sleep(0.01)
    raise AssertionError("Task did not settle")


async def test_task_management_reads_auth_validation_and_revision_are_observational(tmp_path):
    async with api(tmp_path) as (client, coordinator, models):
        assert (await client.get("/tasks", headers={"Authorization": ""})).status_code == 401
        assert (await client.get("/tasks/overview")).json()["counts"]["total"] == 0
        settings = (await client.get("/tasks/settings")).json()
        assert settings["mode"] == "observe"
        assert (await client.get("/tasks/candidates")).json() == {"candidates": []}
        body = {"expected_revision": settings["revision"], "mode": "auto",
                "rules": [{"id": "docs", "enabled": True, "contains": ["documentation"]}]}
        assert (await client.patch("/tasks/settings", json=body)).status_code == 200
        assert (await client.patch("/tasks/settings", json=body)).status_code == 409
        assert (await client.post("/tasks", json={"source_session_id": "fake", "title": "x", "goal": "x",
                                                  "execution_snapshot": {"_local_task": {}}})).status_code == 422
        assert models == []
        assert coordinator.store.list_runs() == []
        capabilities = (await client.get("/capabilities")).json()["capabilities"]
        assert "local_tasks_v1" in capabilities


async def test_api_task_workers_use_independent_empty_sessions_and_summarize_results(tmp_path):
    async with api(tmp_path) as (client, coordinator, models):
        session = coordinator.create_session(tmp_path / "work")
        response = await client.post("/tasks", json={"source_session_id": session["id"], "title": "Two readers",
            "goal": "Inspect disjoint material", "request_id": "one-ticket", "workers": [
                {"title": "Reader one", "goal": "Inspect one", "role": "reader"},
                {"title": "Reader two", "goal": "Inspect two", "role": "reader"}]})
        assert response.status_code == 201, response.text
        ticket = response.json()
        repeat = (await client.post("/tasks", json={"source_session_id": session["id"], "title": "Two readers",
            "goal": "Inspect disjoint material", "request_id": "one-ticket"})).json()
        assert repeat["id"] == ticket["id"]
        done = await settle(coordinator, ticket["id"])
        assert done["state"] == "completed", done
        assert len(done["workers"]) == 2
        assert len({row["session_id"] for row in done["workers"]}) == 2
        assert len({row["cwd"] for row in done["workers"]}) == 2
        assert all(Path(row["cwd"]).is_dir() for row in done["workers"])
        assert len(models) == 2
        assert "Verified Worker result" in done["summary"]
        assert coordinator.store.history(session["id"]) == []
        for worker in done["workers"]:
            with pytest.raises(StateError, match="task input"):
                await coordinator.submit(worker["session_id"], "bypass reader role")
        with pytest.raises(ValueError, match="Task bindings"):
            await coordinator.submit(session["id"], "forged", overrides={"_local_task": {"role": "worker"}})


async def test_task_delegate_is_journal_bound_session_scoped_and_nonblocking(tmp_path):
    async with api(tmp_path) as (_, coordinator, _):
        source = coordinator.create_session(tmp_path / "source")
        other = coordinator.create_session(tmp_path / "other")
        result = await coordinator.execute_session_tool(source["id"], "task_delegate", {
            "title": "Delegation", "goal": "Return a fact"}, approved=True)
        assert not result.is_error, result
        ticket = result.data
        assert ticket["source_session_id"] == source["id"]
        assert coordinator.store.tool_executions(ticket["parent_run_id"])[0]["state"] == "checkpointed"
        denied = await coordinator.execute_session_tool(other["id"], "task_get", {"ticket_id": ticket["id"]})
        assert denied.is_error and denied.error_category == "permission"
        listing = await coordinator.execute_session_tool(source["id"], "task_list", {})
        assert [item["id"] for item in listing.data["tickets"]] == [ticket["id"]]
        assert (await settle(coordinator, ticket["id"]))["state"] == "completed"


class ClassifiedTool:
    def __init__(self):
        self.calls = 0
        self.spec = ToolSpec("classified", "Read or write", {"type": "object", "properties": {
            "write": {"type": "boolean"}}, "required": ["write"], "additionalProperties": False}, risk="read")

    def classify(self, arguments):
        return dataclasses.replace(self.spec, risk="write" if arguments["write"] else "read")

    async def execute(self, arguments, context):
        self.calls += 1
        return ToolResult("mutated")


async def test_reader_guard_reclassifies_after_hook_mutates_read_into_write(tmp_path):
    store = Store(tmp_path / "runtime.db")
    session = store.create_session(tmp_path)
    run, _ = store.create_run(session["id"])
    tool = ClassifiedTool()

    class Hooks:
        async def run(self, event, payload, context):
            if event == "PreToolUse":
                payload["arguments"]["write"] = True
            return SimpleNamespace(allow=True)

    context = ToolContext(session["id"], run["id"], tmp_path, tmp_path, asyncio.Event(), services={
        "task_guard": lambda: {"role": "reader"}, "hooks": Hooks()})
    dispatcher = Dispatcher(Registry([tool]), store, PermissionPolicy(allowed_tools={"classified"}))
    try:
        result = (await dispatcher.execute([ToolCall("one", "classified", {"write": False})], context))[0]
        assert result.is_error and result.outcome == "not_executed"
        assert "Reader" in result.content
        assert tool.calls == 0 and store.tool_executions(run["id"]) == []
    finally:
        store.close()


async def test_task_guard_rechecks_after_await_before_tool_effect_and_uses_private_intent_context(tmp_path):
    store = Store(tmp_path / "runtime.db")
    session = store.create_session(tmp_path)
    run, _ = store.create_run(session["id"])
    tool = ClassifiedTool()
    revoked = False

    def guard():
        if revoked:
            raise StateError("Source has an unknown material effect")
        return {"role": "worker"}

    async def emit(event, data):
        nonlocal revoked
        if event == "tool.started":
            revoked = True

    services = {"task_guard": guard}
    context = ToolContext(session["id"], run["id"], tmp_path, tmp_path, asyncio.Event(), emit=emit, services=services)
    dispatcher = Dispatcher(Registry([tool]), store, PermissionPolicy())
    try:
        result = (await dispatcher.execute([ToolCall("one", "classified", {"write": False})], context))[0]
        assert result.is_error and result.outcome == "not_executed"
        assert tool.calls == 0
        assert store.tool_executions(run["id"])[0]["state"] == "failed_no_effect"
        assert "tool_execution_id" not in services
    finally:
        store.close()


async def test_manual_worker_actions_retain_reader_and_source_unknown_guards(tmp_path):
    tool = ClassifiedTool()
    async with api(tmp_path, tools=[tool]) as (_, coordinator, _):
        source = coordinator.create_session(tmp_path / "work")
        ticket = coordinator.tasks.create_ticket(source["id"], "Read only", "Inspect", workers=[{
            "title": "Reader", "goal": "Inspect", "role": "reader"}])
        done = await settle(coordinator, ticket["id"])
        worker_id = done["workers"][0]["session_id"]
        read = await coordinator.execute_session_tool(worker_id, "classified", {"write": False})
        assert not read.is_error, read
        denied = await coordinator.execute_session_tool(worker_id, "classified", {"write": True}, approved=True)
        assert denied.is_error and denied.outcome == "not_executed"
        assert tool.calls == 1
        parent, _ = coordinator.store.create_run(source["id"])
        entry = coordinator.store.prepare_tool(source["id"], parent["id"], ToolCall("uncertain", "write", {}),
                                               risk="write", replay_safe=False)
        coordinator.store.mark_dispatching(entry["id"])
        coordinator.store.commit_tool(entry["id"], ToolResult("Unknown write", True, "cancelled", "unknown"))
        coordinator.store.set_run_status(parent["id"], "needs_reconciliation")
        with pytest.raises(StateError, match="unresolved"):
            await coordinator.execute_session_tool(worker_id, "classified", {"write": False})
        assert tool.calls == 1


async def test_attended_workers_never_enter_automatic_memory_capture(tmp_path, monkeypatch):
    calls = []

    async def capture(*args):
        calls.append(args[1]["id"])

    monkeypatch.setattr("aster.service.auto_memory.capture_completed_turn", capture)
    async with api(tmp_path) as (_, coordinator, _):
        source = coordinator.create_session(tmp_path / "work")
        ticket = coordinator.tasks.create_ticket(source["id"], "Machine goal", "Machine authored task", unattended=False)
        assert (await settle(coordinator, ticket["id"]))["state"] == "completed"
        assert calls == []
        ordinary = await coordinator.submit(source["id"], "Actual human message")
        await coordinator.wait(ordinary["id"], timeout=5)
        for _ in range(20):
            if calls:
                break
            await asyncio.sleep(0.01)
        assert calls == [ordinary["id"]]


async def test_task_automation_observe_replays_candidate_without_model_or_fake_run(tmp_path):
    async with api(tmp_path) as (_, coordinator, models):
        source = coordinator.create_session(tmp_path / "work")
        settings = coordinator.tasks.settings()
        coordinator.tasks.update_settings(settings["revision"], "observe", [
            {"id": "inspect", "enabled": True, "contains": ["inspect"]}])
        automation = coordinator.automation
        job = automation.create_job("schedule", source["id"], "inspect the directory", {
            "cron": "* * * * *", "delivery": "task"})
        automation._claim(job, "receipt-one", automation.clock(), job["prompt"], automation.clock())
        await automation._deliver(automation.clock())
        receipt = automation.receipts(job["id"])[0]
        assert receipt["state"] == "completed" and receipt["last_status"] == "observed"
        assert receipt["candidate_id"] and receipt["run_id"] is None and receipt["ticket_id"] is None
        assert models == [] and coordinator.store.list_runs() == []
        # A lost receipt acknowledgement re-evaluates the same durable candidate.
        with coordinator.store.transaction() as db:
            db.execute("UPDATE automation_triggers SET state='prepared' WHERE id=?", (receipt["id"],))
        await automation._deliver(automation.clock())
        assert len(coordinator.tasks.list_candidates()) == 1
        assert automation.receipts(job["id"])[0]["candidate_id"] == receipt["candidate_id"]
        with pytest.raises(ValueError, match="delivery"):
            automation.update_job(job["id"], config={"delivery": "invalid"})


async def test_task_automation_pins_claim_delivery_and_model_choices(tmp_path):
    import yaml

    async with api(tmp_path) as (_, coordinator, models):
        source = coordinator.create_session(tmp_path / "work")
        settings = coordinator.tasks.settings()
        coordinator.tasks.update_settings(settings["revision"], "auto", [
            {"id": "inspect", "enabled": True, "contains": ["inspect"]}])
        automation = coordinator.automation
        job = automation.create_job("schedule", source["id"], "inspect the directory", {
            "cron": "* * * * *", "delivery": "task"})
        expected = coordinator.session_selection(source["id"])["model"]
        automation._claim(job, "one", automation.clock(), job["prompt"], automation.clock())
        automation.update_job(job["id"], config={"delivery": "run"})
        (coordinator.data_dir / "config.yaml").write_text(yaml.safe_dump({"model": "changed-after-claim"}), encoding="utf-8")
        await automation._deliver(automation.clock())
        receipt = automation.receipts(job["id"])[0]
        assert receipt["delivery"] == "task" and receipt["ticket_id"] and receipt["run_id"] is None
        automation._results(automation.clock())
        assert not [event for event in coordinator.store.events() if event["type"] == "automation.result"]
        done = await settle(coordinator, receipt["ticket_id"])
        assert done["state"] == "completed", done
        assert models[0].task_settings["model"] == expected
        automation._results(automation.clock())
        assert automation.receipts(job["id"])[0]["state"] == "completed"
        results = [event for event in coordinator.store.events() if event["type"] == "automation.result"]
        assert len(results) == 1 and results[0]["payload"]["ticket_id"] == receipt["ticket_id"]
        assert coordinator.store.list_runs(source["id"]) == []


async def test_task_worker_approval_wait_is_visible_and_uses_existing_endpoint(tmp_path):
    tool = ClassifiedTool()
    async with api(tmp_path, tools=[tool], turns=[ModelTurn(tool_calls=[
            ToolCall("write", "classified", {"write": True})]), ModelTurn("Verified")]) as (client, coordinator, _):
        source = coordinator.create_session(tmp_path / "work")
        ticket = coordinator.tasks.create_ticket(source["id"], "Needs approval", "Perform controlled write")
        for _ in range(200):
            await coordinator.tasks.tick()
            current = coordinator.tasks.get_ticket(ticket["id"])
            pending = [item for worker in current["workers"] for item in worker["pending_requests"]]
            if pending:
                break
            await asyncio.sleep(0.01)
        assert current["state"] == "waiting" and current["attention"]
        assert tool.calls == 0
        response = await client.post(f"/approvals/{pending[0]['id']}/decision", json={"approved": True})
        assert response.status_code == 200 and response.json()["accepted"]
        assert (await settle(coordinator, ticket["id"]))["state"] == "completed"
        assert tool.calls == 1


async def test_dynamic_skill_tool_ceiling_survives_delegation(tmp_path):
    import yaml

    tool = ClassifiedTool()
    tool.spec = dataclasses.replace(tool.spec, name="forbidden")

    def factory(settings):
        if settings.get("_local_task"):
            return FakeModel([ModelTurn(tool_calls=[ToolCall("child-write", "forbidden", {"write": True})]),
                              ModelTurn("The permitted work is finished")])
        return FakeModel([
            ModelTurn(tool_calls=[ToolCall("load", "use_skill", {"name": "restricted"})]),
            ModelTurn(tool_calls=[ToolCall("delegate", "task_delegate", {"title": "Scoped child", "goal": "Inspect"})]),
            ModelTurn("Delegated"),
        ])

    coordinator = RunCoordinator(tmp_path / "state", tools=[tool], credentials=EmptyCredentials(), model_factory=factory)
    try:
        (coordinator.data_dir / "config.yaml").write_text(yaml.safe_dump({"permissions": {
            "auto_approve_tools": ["task_delegate", "forbidden"]}}), encoding="utf-8")
        skill = coordinator.data_dir / "skills" / "restricted"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: restricted\nallowed-tools: [task_delegate]\n---\nInspect only.\n", encoding="utf-8")
        source = coordinator.create_session(tmp_path / "work")
        parent = await coordinator.submit(source["id"], "Delegate under the active Skill")
        await coordinator.wait(parent["id"], timeout=5)
        tickets = coordinator.tasks.list_tickets(source["id"])
        assert len(tickets) == 1
        done = await settle(coordinator, tickets[0]["id"])
        assert done["state"] == "failed" and done["attention"], done
        worker = done["workers"][0]
        assert worker["run_status"] == "completed" and worker["state"] == "failed"
        child = coordinator.store.run_request(worker["run_id"])
        assert "forbidden" not in child["snapshot"]["permissions"]["allowed_tools"]
        denied = [item for item in coordinator.store.history_records(worker["session_id"])
                  if item["role"] == "tool" and item["message"].get("metadata", {}).get("error_category") == "permission"]
        assert len(denied) == 1
        assert denied[0]["message"]["metadata"]["outcome"] == "not_executed"
        assert tool.calls == 0
    finally:
        await coordinator.close()


async def test_task_watcher_marks_registered_worker_outputs_before_next_scan(tmp_path):
    import yaml

    watched = tmp_path / "work"
    watched.mkdir()
    output = watched / "output.txt"

    class Produce:
        spec = ToolSpec("produce", "Create the requested local output", {
            "type": "object", "properties": {}, "additionalProperties": False}, risk="write")

        async def execute(self, arguments, context):
            output.write_text("verified output", encoding="utf-8")
            return ToolResult("created", artifacts=[{"path": str(output), "kind": "file", "filename": output.name}])

    async with api(tmp_path, tools=[Produce()], turns=[ModelTurn(tool_calls=[ToolCall("produce", "produce", {})]),
                                                     ModelTurn("Output verified")]) as (_, coordinator, _):
        (coordinator.data_dir / "config.yaml").write_text(yaml.safe_dump({"permissions": {
            "auto_approve_tools": ["produce"]}}), encoding="utf-8")
        source = coordinator.create_session(watched)
        settings = coordinator.tasks.settings()
        coordinator.tasks.update_settings(settings["revision"], "auto", [
            {"id": "produce", "enabled": True, "contains": ["produce"]}])
        automation = coordinator.automation
        job = automation.create_job("watcher", source["id"], "produce a requested output", {
            "path": str(watched), "pattern": "*.txt", "stable_seconds": 0,
            "debounce_seconds": 0, "delivery": "task"})
        automation._watch(job, automation.clock())
        automation._claim(job, "initial", automation.clock(), job["prompt"], automation.clock())
        await automation._deliver(automation.clock())
        receipt = automation.receipts(job["id"])[0]
        done = await settle(coordinator, receipt["ticket_id"])
        assert done["state"] == "completed", done
        automation._results(automation.clock())
        assert output.read_text(encoding="utf-8") == "verified output"
        automation._watch(automation.get_job(job["id"]), automation.clock() + 1)
        assert len(automation.receipts(job["id"])) == 1


async def test_configured_automatic_memory_factories_receive_no_worker_goals(tmp_path):
    import yaml

    state = tmp_path / "state"
    state.mkdir()
    (state / "config.yaml").write_text(yaml.safe_dump({"auto_memory": {"enabled": True}}), encoding="utf-8")

    class MemoryCredentials(EmptyCredentials):
        def get(self, name="deepseek", **kwargs):
            return "offline-" + name

        def resolve(self, reference):
            return "offline-memory-credential"

    calls = []

    def forbidden_factory(*args):
        calls.append(args)
        raise AssertionError("Worker goals must never reach automatic memory")

    coordinator = RunCoordinator(state, tools=[], credentials=MemoryCredentials(),
                                 model_factory=lambda settings: FakeModel([ModelTurn("Worker finished")]))
    coordinator.auto_memory_client_factory = forbidden_factory
    coordinator.auto_memory_model_factory = forbidden_factory
    try:
        source = coordinator.create_session(tmp_path / "work")
        ticket = coordinator.tasks.create_ticket(source["id"], "Machine task", "Model-authored requirement", unattended=False)
        done = await settle(coordinator, ticket["id"])
        assert done["state"] == "completed", done
        assert calls == []
        assert not [event for worker in done["workers"] for event in coordinator.store.events(run_id=worker["run_id"])
                    if event["type"] == "memory.capture"]
    finally:
        await coordinator.close()
