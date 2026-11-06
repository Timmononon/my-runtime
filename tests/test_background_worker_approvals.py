"""Background task inboxes preserve human approval and trusted routing context."""
import asyncio
import os
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import yaml

from aster.api import create_app
from aster.domain import Message, ModelTurn, ToolCall, ToolResult, ToolSpec
from aster.engine import FakeModel
from aster.service import RunCoordinator
from aster.state.store import TERMINAL

TOKEN = "offline-background-task-approval-token-32"


class EmptyCredentials:
    def get(self, *args, **kwargs):
        return None

    def resolve(self, reference):
        raise ValueError("No test credentials")


class WriteProbe:
    spec = ToolSpec("write_probe", "Write one approved temporary artifact",
                    {"type": "object", "properties": {}, "additionalProperties": False}, risk="write")

    def __init__(self):
        self.calls = []

    async def execute(self, arguments, context):
        self.calls.append({"run_id": context.run_id, "unattended": context.services["unattended"]})
        (context.cwd / "approved.txt").write_text("approved once", encoding="utf-8")
        return ToolResult("Approved artifact written")


@asynccontextmanager
async def runtime(tmp_path, *, permissions=None, turns=None, tool=None):
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    if permissions:
        (state / "config.yaml").write_text(yaml.safe_dump({"permissions": permissions}), encoding="utf-8")
    tool = tool or WriteProbe()
    models = []

    def factory(settings):
        model = FakeModel(turns or [ModelTurn(tool_calls=[ToolCall("effect", tool.spec.name, {})]),
                                    ModelTurn("Checked actual result")])
        models.append(model)
        return model

    coordinator = RunCoordinator(state, tools=[tool], credentials=EmptyCredentials(), model_factory=factory,
                                 browser_enabled=False)
    app = create_app(coordinator, token=TOKEN, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost",
                                headers={"Authorization": "Bearer " + TOKEN}) as client:
        try:
            yield coordinator, client, tool, models
        finally:
            await coordinator.close()


def ticket(coordinator, tmp_path, *, role="worker", github=True):
    source = coordinator.create_session(tmp_path / "workspace")
    admission = None
    github_source = None
    if github:
        github_source = coordinator.github.create_source(source["id"], "owner/repository", enabled=True,
            mode="tasks", allowed_actors=["alice"])
        value = {"kind": "issue", "actor": "alice", "actor_type": "human", "numbers": [7],
                 "url": "https://github.com/owner/repository/issues/7", "state": "open"}
        admission = coordinator.github.admission(coordinator.github._lease(github_source["id"]), value, explicit=True)
    item = coordinator.tasks.create_ticket(source["id"], "Background work", "Write the assigned artifact",
        workers=[{"title": "Worker", "goal": "Write the assigned artifact", "role": role}],
        unattended=True, admission=admission)
    return source, github_source, item


async def pending(coordinator, item, kind="approval"):
    for _ in range(200):
        await coordinator.tasks.tick()
        current = coordinator.tasks.get_ticket(item["id"])
        requests = [entry for worker in current["workers"] for entry in worker["pending_requests"] if entry["kind"] == kind]
        if requests:
            return current, requests[0]
        if current["state"] in {"completed", "failed", "cancelled", "needs_reconciliation"}:
            raise AssertionError(current)
        await asyncio.sleep(0.01)
    raise AssertionError("Task did not reach its human inbox")


async def settle(coordinator, item):
    for _ in range(200):
        await coordinator.tasks.tick()
        current = coordinator.tasks.get_ticket(item["id"])
        if current["state"] in {"completed", "failed", "cancelled", "needs_reconciliation"}:
            return current
        await asyncio.sleep(0.01)
    raise AssertionError("Task did not settle")


async def test_github_background_write_waits_for_human_and_executes_once_without_grant_or_memory(tmp_path, monkeypatch):
    captures = []

    async def capture(*args):
        captures.append(args)

    monkeypatch.setattr("aster.service.auto_memory.capture_completed_turn", capture)
    async with runtime(tmp_path) as (coordinator, client, tool, models):
        _, source, item = ticket(coordinator, tmp_path)
        initial_permissions = coordinator.tasks.worker_execution(item["workers"][0]["session_id"])["snapshot"]["permissions"]
        waiting, request = await pending(coordinator, item)
        worker = waiting["workers"][0]
        assert waiting["state"] == "waiting" and not tool.calls
        assert coordinator.store.run_request(worker["run_id"])["unattended"] is True
        assert coordinator.store.tool_executions(worker["run_id"]) == []
        assert (await client.get("/tasks/overview")).json()["tickets"][0]["workers"][0]["pending_requests"][0]["id"] == request["id"]
        assert (await client.post(f"/approvals/{request['id']}/decision", json={"approved": True},
                                  headers={"Authorization": ""})).status_code == 401
        response = await client.post(f"/approvals/{request['id']}/decision", json={"approved": True})
        assert response.status_code == 200 and response.json()["accepted"]
        done = await settle(coordinator, item)
        assert done["state"] == "completed" and tool.calls == [{"run_id": worker["run_id"], "unattended": True}]
        assert (Path(worker["cwd"]) / "approved.txt").read_text() == "approved once"
        assert not (await client.post(f"/approvals/{request['id']}/decision", json={"approved": True})).json()["accepted"]
        assert len(coordinator.store.tool_executions(worker["run_id"])) == 1
        assert coordinator.tasks.worker_execution(worker["session_id"])["snapshot"]["permissions"] == initial_permissions
        assert not captures
        assert all(source["id"] in call["system"] for call in models[0].calls)
        assert source["id"] not in coordinator.store.get_run(worker["run_id"])["system"]


async def test_rejected_background_approval_preserves_incomplete_result_and_never_dispatches(tmp_path):
    async with runtime(tmp_path) as (coordinator, client, tool, _):
        _, _, item = ticket(coordinator, tmp_path)
        waiting, request = await pending(coordinator, item)
        assert (await client.post(f"/approvals/{request['id']}/decision", json={"approved": False})).json()["accepted"]
        done = await settle(coordinator, item)
        assert done["state"] == "failed" and not tool.calls
        assert coordinator.store.tool_executions(waiting["workers"][0]["run_id"]) == []
        await coordinator.tasks.tick()
        assert not coordinator.store.pending_requests()


@pytest.mark.parametrize("restriction", ["deny", "reader", "skill"])
async def test_explicit_denial_reader_and_selected_skill_never_become_pending_approval(tmp_path, restriction):
    permissions = {"denied_tools": ["write_probe"]} if restriction == "deny" else None
    async with runtime(tmp_path, permissions=permissions) as (coordinator, _, tool, _):
        if restriction == "skill":
            skill = coordinator.data_dir / "skills" / "reader"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("---\nname: reader\ndescription: Read only\nallowed-tools: [file_read]\n---\nRead only.\n", encoding="utf-8")
        source = coordinator.create_session(tmp_path / "workspace")
        if restriction == "skill":
            coordinator.select_session(source["id"], selected_skill="reader")
        item = coordinator.tasks.create_ticket(source["id"], "Restricted", "Inspect", unattended=True,
            workers=[{"title": "Worker", "goal": "Inspect", "role": "reader" if restriction == "reader" else "worker"}])
        assert (await settle(coordinator, item))["state"] == "failed"
        assert not tool.calls and coordinator.store.pending_requests() == []


async def test_changed_github_source_during_human_approval_never_executes(tmp_path):
    async with runtime(tmp_path) as (coordinator, client, tool, _):
        _, source, item = ticket(coordinator, tmp_path)
        waiting, request = await pending(coordinator, item)
        coordinator.github.update_source(source["id"], source["revision"], allowed_actors=[])
        assert (await client.post(f"/approvals/{request['id']}/decision", json={"approved": True})).json()["accepted"]
        await settle(coordinator, item)
        assert not tool.calls and coordinator.store.tool_executions(waiting["workers"][0]["run_id"]) == []


async def test_task_cancel_closes_human_inbox_without_replaying_old_approval(tmp_path):
    async with runtime(tmp_path) as (coordinator, client, tool, _):
        _, _, item = ticket(coordinator, tmp_path)
        _, request = await pending(coordinator, item)
        await coordinator.tasks.cancel_ticket(item["id"])
        assert (await settle(coordinator, item))["state"] == "cancelled"
        assert coordinator.store.get_pending(request["id"])["state"] == "cancelled"
        assert not (await client.post(f"/approvals/{request['id']}/decision", json={"approved": True})).json()["accepted"]
        assert not tool.calls


async def test_ordinary_unattended_run_keeps_no_approval_channel(tmp_path):
    async with runtime(tmp_path) as (coordinator, _, tool, _):
        source = coordinator.create_session(tmp_path / "workspace")
        run = await coordinator.submit(source["id"], "Background automation", unattended=True)
        await coordinator.wait(run["id"], timeout=5)
        assert not tool.calls and not coordinator.store.pending_requests()
        errors = [m for m in coordinator.store.history(source["id"]) if m.role == "tool"]
        assert any("no attended approval channel" in m.content for m in errors)


async def test_background_task_question_uses_human_inbox_and_preserves_unattended(tmp_path):
    turns = [ModelTurn(tool_calls=[ToolCall("question", "ask_user_question", {"question": "Which existing file?"})]),
             ModelTurn("Received the human answer")]
    async with runtime(tmp_path, turns=turns) as (coordinator, client, _, _):
        _, _, item = ticket(coordinator, tmp_path)
        waiting, request = await pending(coordinator, item, "question")
        assert coordinator.store.run_request(waiting["workers"][0]["run_id"])["unattended"]
        response = await client.post(f"/questions/{request['id']}/answer", json={"answer": "Existing app.py"})
        assert response.status_code == 200 and response.json()["accepted"]
        assert (await settle(coordinator, item))["state"] == "completed"


async def test_shutdown_restart_does_not_dispatch_or_reuse_old_pending_approval(tmp_path):
    async with runtime(tmp_path) as (coordinator, _, tool, _):
        _, _, item = ticket(coordinator, tmp_path)
        waiting, request = await pending(coordinator, item)
        run_id = waiting["workers"][0]["run_id"]
        await coordinator.close()
        assert not tool.calls
    async with runtime(tmp_path, tool=tool) as (reopened, _, _, models):
        assert reopened.store.get_pending(request["id"])["state"] == "cancelled"
        assert reopened.store.get_run(run_id)["status"] in TERMINAL
        assert not reopened.approvals.resolve(request["id"], True)
        await reopened.tasks.tick()
        assert not tool.calls and not models


async def test_runtime_context_matches_compaction_system_and_never_rewrites_captured_history(tmp_path, monkeypatch):
    from aster.engine.compaction import CompactionController

    systems = []
    original = CompactionController.__init__

    def capture(self, *args, **kwargs):
        systems.append(kwargs["system"])
        original(self, *args, **kwargs)

    monkeypatch.setattr(CompactionController, "__init__", capture)
    turns = [ModelTurn(tool_calls=[ToolCall("read", "task_list", {})]), ModelTurn("Inspected")]
    async with runtime(tmp_path, turns=turns) as (coordinator, _, _, models):
        source, github_source, item = ticket(coordinator, tmp_path)
        done = await settle(coordinator, item)
        worker = done["workers"][0]
        payload = coordinator.store.run_request(worker["run_id"])
        for system in [*systems, *(call["system"] for call in models[0].calls)]:
            assert github_source["id"] in system and source["cwd"] in system and worker["cwd"] in system
            assert "shell_exec_parameters" in system and "permissions remain unchanged" in system
        assert len(models[0].calls) == 2
        assert "task_runtime_context" not in payload["snapshot"]
        assert github_source["id"] not in coordinator.store.get_run(worker["run_id"])["system"]
        history = coordinator.store.history(worker["session_id"])
        assert all(m.role != "system" and "Trusted task routing context" not in m.content for m in history)


async def test_actual_compaction_keeps_trusted_routing_in_main_requests_and_lossless_history(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "config.yaml").write_text(yaml.safe_dump({"runtime": {
        "context_tokens": 24000, "reserve_tokens": 1024, "compaction_absolute_buffer_tokens": 6000,
        "retained_context_rounds": 2, "compaction_persist_learnings": False}}), encoding="utf-8")
    async with runtime(tmp_path) as (coordinator, _, _, _):
        calls = []

        class CompactingModel:
            async def generate(self, messages, tools, *, system, **kwargs):
                calls.append({"tools": tools, "system": system})
                return ModelTurn("Earlier completed discussion was summarized" if not tools else "Inspected the current task")

        coordinator.model_factory = lambda settings: CompactingModel()
        _, github_source, item = ticket(coordinator, tmp_path)
        session_id = item["workers"][0]["session_id"]
        original = []
        for index in range(20):
            for message in (Message("user", f"Prior question {index}"),
                            Message("assistant", (f"Older answer {index}. " + "Completed observation. " * 450))):
                coordinator.store.append_message(session_id, None, message)
                original.append((message.role, message.content))
        done = await settle(coordinator, item)
        assert done["state"] == "completed", done
        run_id = done["workers"][0]["run_id"]
        assert any(event["type"] == "context.compacted" for event in coordinator.store.events(run_id=run_id))
        main_requests = [entry for entry in calls if entry["tools"]]
        assert main_requests and all(github_source["id"] in entry["system"] for entry in main_requests)
        assert [(m.role, m.content) for m in coordinator.store.history(session_id)[:len(original)]] == original


@pytest.mark.windows
@pytest.mark.skipif(os.name != "nt" or not shutil.which("powershell"), reason="Windows PowerShell required")
async def test_real_windows_shell_waits_for_task_approval_before_touching_files(tmp_path):
    from aster.platform.processes import ProcessSupervisor
    from aster.tools.process import ProcessTool

    supervisor = ProcessSupervisor()
    tool = ProcessTool(supervisor, shell=True)
    turns = [ModelTurn(tool_calls=[ToolCall("shell", "shell_exec", {
        "backend": "powershell51", "script": "Set-Content -LiteralPath 'shell-approved.txt' -Value 'approved'"})]),
        ModelTurn("Verified shell artifact")]
    try:
        async with runtime(tmp_path, turns=turns, tool=tool) as (coordinator, client, _, _):
            _, _, item = ticket(coordinator, tmp_path)
            waiting, request = await pending(coordinator, item)
            target = Path(waiting["workers"][0]["cwd"]) / "shell-approved.txt"
            assert not target.exists()
            assert (await client.post(f"/approvals/{request['id']}/decision", json={"approved": True})).json()["accepted"]
            assert (await settle(coordinator, item))["state"] == "completed"
            assert target.read_text().strip() == "approved"
    finally:
        await supervisor.close()
