import asyncio
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
import yaml

from aster.api import create_app
from aster.config import load_config
from aster.domain import Message, ModelTurn, ToolCall, ToolResult
from aster.engine import FakeModel
from aster.maintenance import backup_bundle, registered_artifacts, restore_bundle
from aster.service import RunCoordinator
from aster.state import StateError, Store
from aster.state.store import SessionBusy


class NoCredentials:
    def get(self, *args, **kwargs):
        return None

    def resolve(self, reference):
        raise AssertionError("Lifecycle tests must not access real credentials")


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "runtime.db")
    yield value
    value.close()


@pytest.fixture
async def coordinator(tmp_path):
    value = RunCoordinator(tmp_path / "state", tools=[], credentials=NoCredentials(),
                           model_factory=lambda _: FakeModel([ModelTurn("offline answer")]))
    yield value
    await value.close()


@pytest.fixture
async def client(coordinator):
    token = "lifecycle-offline-test-token-long-enough"
    app = create_app(coordinator, token=token)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost",
                                headers={"Authorization": "Bearer " + token}) as value:
        yield value


def test_legacy_schema_two_migration_is_additive_and_persists(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY,cwd TEXT NOT NULL,agent_id TEXT NOT NULL,"
                   "project_id TEXT,title TEXT NOT NULL DEFAULT '',created_at REAL NOT NULL,updated_at REAL NOT NULL)")
        db.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?)", ("legacy", str(tmp_path), "default", None, "original", 1, 1))
        db.execute("PRAGMA user_version=2")
    migrated = Store(path)
    try:
        assert migrated.get_session("legacy")["archived_at"] is None
        migrated.append_message("legacy", None, Message("assistant", "original transcript"))
        archive = migrated.history_records("legacy")
        archived = migrated.change_session_lifecycle("legacy", "archive")
        # Positional legacy writers still have exactly seven session columns.
        assert len(migrated.connection.execute("PRAGMA table_info(sessions)").fetchall()) == 7
        assert migrated.connection.execute("PRAGMA user_version").fetchone()[0] == 2
    finally:
        migrated.close()
    reopened = Store(path)
    try:
        restored = reopened.get_session("legacy")
        assert restored["deleted_at"] is None and restored["archived_at"] == archived["archived_at"]
        assert reopened.history_records("legacy") == archive
        assert reopened.list_sessions() == []
        reopened.change_session_lifecycle("legacy", "unarchive")
        assert reopened.list_sessions()[0]["id"] == "legacy"
    finally:
        reopened.close()


async def test_api_archive_recovers_and_delete_permanently_removes_owned_evidence(client, coordinator, tmp_path):
    project = coordinator.workbench.projects.create("Preserved scope", str(tmp_path))
    session = coordinator.create_session(project_id=project["id"])
    sid = session["id"]
    attachment = coordinator.attachments.register_bytes(sid, "evidence.txt", b"original attachment")
    response = await client.post(f"/sessions/{sid}/runs", json={"text": "hello", "request_id": "same",
                                                              "attachments": [attachment["attachment_id"]]})
    assert response.status_code == 202
    rid = response.json()["id"]
    assert (await coordinator.wait(rid, timeout=3)).status == "completed"
    # Worker resource cleanup finishes before a lifecycle action can acquire its gate.
    await coordinator._workers[sid]
    history = coordinator.store.history_records(sid)
    checkpoint = coordinator.store.get_checkpoint(sid)
    selection = coordinator.session_selection(sid)
    original_events = coordinator.store.events(session_id=sid)
    archived = (await client.post(f"/sessions/{sid}/archive")).json()
    assert archived["archived_at"] is not None and archived["deleted_at"] is None
    assert (await client.post(f"/sessions/{sid}/archive")).json() == archived
    assert (await client.get("/sessions")).json()["sessions"] == []
    assert [row["id"] for row in (await client.get("/sessions?state=archived")).json()["sessions"]] == [sid]
    detail = (await client.get(f"/sessions/{sid}")).json()
    assert detail["messages"] == history and detail["selection"] == selection
    assert detail["project_id"] == project["id"] and detail["cwd"] == session["cwd"]
    assert coordinator.store.get_checkpoint(sid) == checkpoint
    assert coordinator.attachments.resolve(sid, attachment["attachment_id"])[0].read_bytes() == b"original attachment"
    assert coordinator.store.events(session_id=sid)[:len(original_events)] == original_events
    retry = await client.post(f"/sessions/{sid}/runs", json={"text": "hello", "request_id": "same"})
    assert retry.status_code == 202 and retry.json()["id"] == rid
    assert (await client.post(f"/sessions/{sid}/unarchive")).json()["archived_at"] is None
    assert len(coordinator.store.list_runs(sid)) == 1
    deleted = await client.delete(f"/sessions/{sid}?permanent=true")
    assert deleted.status_code == 200 and deleted.json() == {"deleted": True, "id": sid}
    assert (await client.get(f"/sessions/{sid}")).status_code == 404
    assert (await client.get(f"/runs/{rid}")).status_code == 404
    assert (await client.delete(f"/sessions/{sid}?permanent=true")).status_code == 404
    assert (await client.post(f"/sessions/{sid}/restore")).status_code == 410
    assert (await client.get("/sessions?state=deleted")).json()["sessions"] == []
    assert coordinator.store.history_records(sid) == [] and coordinator.store.get_checkpoint(sid) is None
    assert coordinator.store.events(session_id=sid) == []
    assert not coordinator.store.connection.execute("SELECT 1 FROM session_model_selection WHERE session_id=?", (sid,)).fetchone()
    assert not coordinator.store.connection.execute("SELECT 1 FROM usage WHERE run_id=?", (rid,)).fetchone()
    assert not coordinator.store.search_messages("hello", all_projects=True)
    assert not Path(attachment["path"]).exists()
    assert (await client.get("/sessions?state=invalid")).status_code == 422
    assert (await client.delete("/sessions/missing?permanent=true")).status_code == 404


@pytest.mark.parametrize("action", ["archive", "delete"])
async def test_inactive_sessions_reject_mutations_on_all_entrypoints(client, coordinator, tmp_path, action):
    sid = coordinator.create_session(tmp_path)["id"]
    coordinator.store.append_message(sid, None, Message("assistant", "saved"))
    before = coordinator.store.history_records(sid)
    await coordinator.change_session_lifecycle(sid, action)
    requests = [
        ("post", f"/sessions/{sid}/runs", {"text": "new"}),
        ("post", f"/sessions/{sid}/messages", {"text": "new"}),
        ("patch", f"/sessions/{sid}", {"title": "renamed"}),
        ("post", f"/sessions/{sid}/fork", {}),
        ("post", f"/sessions/{sid}/side-chat", {"text": "new"}),
        ("post", f"/sessions/{sid}/compact", {}),
        ("patch", f"/sessions/{sid}/selection", {"model": "deepseek-flash"}),
    ]
    for method, path, body in requests:
        response = await getattr(client, method)(path, json=body)
        assert response.status_code == (404 if action == "delete" else 409), (method, path, response.text)
    uploaded = await client.post(f"/sessions/{sid}/attachments?filename=new.txt", content=b"new",
                                 headers={"Content-Type": "text/plain"})
    assert uploaded.status_code == (404 if action == "delete" else 409)
    with pytest.raises(KeyError if action == "delete" else StateError):
        await coordinator.execute_session_tool(sid, "unknown", {})
    with pytest.raises(KeyError if action == "delete" else StateError):
        coordinator.providers.save_selection(sid, {})
    assert coordinator.store.history_records(sid) == ([] if action == "delete" else before)
    assert coordinator.store.list_runs(sid) == []
    if action == "delete":
        with pytest.raises(KeyError):
            coordinator.attachments.list(sid)
    else:
        assert coordinator.attachments.list(sid) == []


@pytest.mark.parametrize("status", ["running", "queued", "waiting_approval", "waiting_input", "cancelling"])
def test_busy_runs_block_without_cancelling_or_changing_state(store, tmp_path, status):
    sid = store.create_session(tmp_path)["id"]
    run, _ = store.create_run(sid, initial_status="queued" if status == "queued" else "running")
    if status not in {"running", "queued"}:
        store.set_run_status(run["id"], status)
    for action in ("archive", "delete"):
        with pytest.raises(SessionBusy):
            store.change_session_lifecycle(sid, action)
    assert store.get_run(run["id"])["status"] == status
    assert store.get_session(sid)["archived_at"] is None and store.get_session(sid)["deleted_at"] is None


@pytest.mark.parametrize("blocker", ["mailbox", "approval", "prepared", "dispatching", "outcome_unknown"])
def test_pending_material_and_uncertain_effects_block_lifecycle(store, tmp_path, blocker):
    sid = store.create_session(tmp_path)["id"]
    run, _ = store.create_run(sid)
    if blocker == "mailbox":
        store.queue_message(sid, "must not disappear")
    elif blocker == "approval":
        store.create_pending(sid, run["id"], "approval", {}, 60)
    else:
        execution = store.prepare_tool(sid, run["id"], ToolCall("call", "write", {}), risk="write", replay_safe=False)
        with store.transaction() as db:
            db.execute("UPDATE tool_executions SET state=? WHERE id=?", (blocker, execution["id"]))
    store.finish_run(run["id"], "failed")
    with pytest.raises(SessionBusy):
        store.change_session_lifecycle(sid, "delete")


def test_pending_message_retraction_and_retry_keep_identity(store, tmp_path):
    sid = store.create_session(tmp_path)["id"]
    mid = store.queue_message(sid, "pending", request_id="same")
    with pytest.raises(SessionBusy):
        store.change_session_lifecycle(sid, "archive")
    assert store.retract_message(sid, mid)
    store.change_session_lifecycle(sid, "archive")
    assert store.queue_message(sid, "pending", request_id="same") == mid
    with pytest.raises(StateError):
        store.queue_message(sid, "new", request_id="new")


def test_parent_and_child_busy_dependency_and_hidden_search(store, tmp_path):
    parent = store.create_session(tmp_path)["id"]
    store.append_message(parent, None, Message("assistant", "unique evidence"))
    child = store.fork_session(parent, kind="side_chat")["id"]
    run, _ = store.create_run(child, initial_status="queued")
    with pytest.raises(SessionBusy):
        store.change_session_lifecycle(parent, "delete")
    store.finish_run(run["id"], "cancelled")
    store.change_session_lifecycle(child, "archive")
    store.change_session_lifecycle(parent, "archive")
    assert store.list_sessions(state="archived") == [store.get_session(parent)]
    assert {s["id"] for s in store.list_sessions(state="all", include_side_chats=True)} == {parent, child}
    assert store.search_messages("unique evidence", all_projects=True) == []
    assert len(store.search_messages("unique evidence", session_id=parent, all_projects=True)) == 1
    assert store.history_records(child)[0]["message"]["metadata"]["inherited_from_session"] == parent


async def test_gate_blocks_manual_writers_and_idempotent_action_does_not_repeat_events(coordinator, tmp_path):
    sid = coordinator.create_session(tmp_path)["id"]
    gate = coordinator._session_gates.setdefault(sid, asyncio.Lock())
    async with gate:
        with pytest.raises(SessionBusy):
            await coordinator.change_session_lifecycle(sid, "archive")
    archived = await coordinator.change_session_lifecycle(sid, "archive")
    events = coordinator.store.events(session_id=sid)
    async with gate:
        assert await coordinator.change_session_lifecycle(sid, "archive") == archived
    assert coordinator.store.events(session_id=sid) == events


async def test_enabled_automation_and_sticky_destination_require_explicit_disable(coordinator, tmp_path):
    sid = coordinator.create_session(tmp_path)["id"]
    automation = coordinator.automation
    job = automation.create_job("schedule", sid, "offline", {"cron": "0 0 * * *", "timezone": "UTC", "session_mode": "sticky"})
    target = coordinator.store.automation_session(sid, "automation:" + job["id"])
    for identity in (sid, target):
        with pytest.raises(SessionBusy, match="automation"):
            await coordinator.change_session_lifecycle(identity, "archive")
    automation.update_job(job["id"], enabled=False)
    await coordinator.change_session_lifecycle(target, "archive")
    with pytest.raises(StateError):
        automation.update_job(job["id"], enabled=True)
    await coordinator.change_session_lifecycle(sid, "archive")
    with pytest.raises(StateError):
        automation.create_job("schedule", sid, "offline", {"cron": "0 0 * * *", "timezone": "UTC"})
    with pytest.raises(StateError):
        automation.update_job(job["id"], prompt="new")
    assert not automation.update_job(job["id"], enabled=False)["enabled"]
    # A poll snapshot captured before disabling cannot create a late trigger.
    automation._claim(job, "late", time.time(), "offline", time.time())
    assert automation.receipts(job["id"]) == []


async def test_pending_automation_delivery_blocks_even_after_disable(coordinator, tmp_path):
    sid = coordinator.create_session(tmp_path)["id"]
    service = coordinator.automation
    job = service.create_job("schedule", sid, "offline", {"cron": "0 0 * * *", "timezone": "UTC"})
    service._claim(job, "pending", time.time(), "offline", time.time())
    service.update_job(job["id"], enabled=False)
    with pytest.raises(SessionBusy, match="pending automation"):
        await coordinator.change_session_lifecycle(sid, "delete")
    service.delete_job(job["id"])
    assert await coordinator.change_session_lifecycle(sid, "delete") == {"deleted": True, "id": sid}


@pytest.mark.parametrize("iteration", range(4))
def test_independent_connections_serialize_archive_against_run_admission(store, tmp_path, iteration):
    sid = store.create_session(tmp_path)["id"]
    other = Store(store.path)
    barrier = threading.Barrier(2)

    def archive():
        barrier.wait(timeout=5)
        try:
            store.change_session_lifecycle(sid, "archive")
            return "archived"
        except StateError:
            return "blocked"

    def submit():
        barrier.wait(timeout=5)
        try:
            other.create_run(sid, initial_status="queued")
            return "queued"
        except StateError:
            return "blocked"

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = pool.submit(archive), pool.submit(submit)
            results = {a.result(timeout=10), b.result(timeout=10)}
        assert results in ({"archived", "blocked"}, {"queued", "blocked"})
        assert not (store.get_session(sid)["archived_at"] and store.list_runs(sid))
    finally:
        other.close()


async def test_project_permanent_delete_counts_and_history_are_truthful(client, coordinator, tmp_path):
    projects = coordinator.workbench.projects
    project = projects.create("A project", str(tmp_path))
    pid = project["id"]
    ids = [coordinator.create_session(project_id=pid)["id"] for _ in range(105)]
    coordinator.store.append_message(ids[0], None, Message("assistant", "permanent evidence"))
    coordinator.store.change_session_lifecycle(ids[1], "archive")
    coordinator.store.change_session_lifecycle(ids[2], "delete")
    config = coordinator.data_dir / "projects" / pid / "config.yaml"
    original_config = config.read_bytes()
    history = coordinator.store.history_records(ids[0])
    assert projects.update(pid, name="Renamed")["session_count"] == 104
    response = await client.delete(f"/workbench/projects/{pid}?permanent=true")
    assert response.status_code == 200
    assert response.json()["preserved_sessions"] == 104 and response.json()["archived_sessions"] == 0
    assert response.json()["permanent"] is True
    assert (await client.delete(f"/workbench/projects/{pid}?permanent=true")).status_code == 404
    assert projects.list() == []
    assert (await client.get("/workbench/projects?include_removed=true")).json()["projects"] == []
    assert coordinator.store.get_session(ids[0])["project_id"] == pid
    assert coordinator.store.history_records(ids[0]) == history and config.read_bytes() == original_config
    with pytest.raises(ValueError, match="removed"):
        coordinator.create_session(project_id=pid)
    with pytest.raises(ValueError, match="removed"):
        coordinator.store.create_session(tmp_path, project_id=pid)
    # Existing chats keep their scope and can still execute, independently of project visibility.
    run = await coordinator.submit(ids[0], "continue after project removal")
    assert (await coordinator.wait(run["id"], timeout=3)).status == "completed"
    assert (await client.post(f"/workbench/projects/{pid}/restore")).status_code == 410
    assert coordinator.store.get_session(ids[1])["archived_at"] is not None
    with pytest.raises(KeyError):
        coordinator.store.get_session(ids[2])


async def test_project_delete_preserves_running_session_scope(client, coordinator, tmp_path):
    project = coordinator.workbench.projects.create("Busy", str(tmp_path))
    session = coordinator.create_session(project_id=project["id"])
    run, _ = coordinator.store.create_run(session["id"], initial_status="queued")
    response = await client.delete(f"/workbench/projects/{project['id']}?permanent=true")
    assert response.status_code == 200 and response.json()["permanent"]
    with pytest.raises(KeyError):
        coordinator.workbench.projects.get(project["id"])
    assert coordinator.store.get_run(run["id"])["status"] == "queued"
    coordinator.store.finish_run(run["id"], "cancelled")


async def test_session_pagination_keeps_old_active_and_recovers_oldest_inactive(client, coordinator, tmp_path):
    active = coordinator.create_session(tmp_path)["id"]
    for state, action in (("archived", "archive"),):
        ids = []
        for _ in range(105):
            sid = coordinator.create_session(tmp_path)["id"]
            ids.append(sid)
            coordinator.store.change_session_lifecycle(sid, action)
        first = (await client.get(f"/sessions?state={state}&limit=100")).json()["sessions"]
        second = (await client.get(f"/sessions?state={state}&limit=100&offset=100")).json()["sessions"]
        assert len(first) == 100 and len(second) == 5
        assert {row["id"] for row in first}.isdisjoint({row["id"] for row in second})
        assert {row["id"] for row in first + second} == set(ids)
        assert second[-1]["id"] == ids[0]
        endpoint = "unarchive"
        assert (await client.post(f"/sessions/{ids[0]}/{endpoint}")).status_code == 200
    active_ids = {row["id"] for row in (await client.get("/sessions")).json()["sessions"]}
    assert active in active_ids and len(active_ids) == 2
    for query in ("limit=101", "limit=0", "offset=-1"):
        assert (await client.get("/sessions?" + query)).status_code == 422


async def test_removed_project_automation_admission_accepts_only_existing_destinations(coordinator, tmp_path):
    project = coordinator.workbench.projects.create("Automation scope", str(tmp_path))
    sid = coordinator.create_session(project_id=project["id"])["id"]
    service = coordinator.automation
    base_config = {"cron": "0 0 * * *", "timezone": "UTC"}
    sticky = service.create_job("schedule", sid, "offline", {**base_config, "session_mode": "sticky"}, enabled=False)
    target = coordinator.store.automation_session(sid, "automation:" + sticky["id"])
    fresh = service.create_job("schedule", sid, "offline", base_config, enabled=False)
    coordinator.workbench.projects.remove(project["id"])
    for mode in ("fresh", "sticky"):
        with pytest.raises(ValueError, match="removed"):
            service.create_job("schedule", sid, "offline", {**base_config, "session_mode": mode})
    with pytest.raises(ValueError, match="removed"):
        service.update_job(fresh["id"], enabled=True)
    assert not service.get_job(fresh["id"])["enabled"]
    assert service.update_job(sticky["id"], enabled=True)["enabled"]
    source = service.create_job("schedule", sid, "offline", {**base_config, "session_mode": "source"})
    with pytest.raises(ValueError, match="removed"):
        service.update_job(source["id"], config={"session_mode": "fresh"})
    assert service.get_job(source["id"])["config"]["session_mode"] == "source"
    # Both accepted routing modes deliver successfully with an offline model,
    # retaining the removed project association and existing destination IDs.
    now = time.time()
    service._claim(service.get_job(sticky["id"]), "sticky", now, "offline", now)
    service._claim(source, "source", now, "offline", now)
    await service._deliver(now)
    receipts = service.receipts()
    assert len(receipts) == 2 and all(row["state"] == "submitted" for row in receipts)
    runs = [coordinator.store.get_run(row["run_id"]) for row in receipts]
    assert {run["session_id"] for run in runs} == {sid, target}
    for run in runs:
        assert (await coordinator.wait(run["id"], timeout=3)).status == "completed"


async def test_project_accepts_multiple_new_conversations_with_the_managed_directory(client, coordinator, tmp_path):
    directory = tmp_path / "project work"
    directory.mkdir()
    project = coordinator.workbench.projects.create("Repeated conversations", directory)
    ids = []
    for title in ("first", "second", "third"):
        response = await client.post("/sessions", json={"project_id": project["id"], "cwd": str(tmp_path), "title": title})
        assert response.status_code == 201
        session = response.json()
        ids.append(session["id"])
        assert session["project_id"] == project["id"] and session["cwd"] == str(directory.resolve())
    assert len(set(ids)) == 3 and coordinator.workbench.projects.list()[0]["session_count"] == 3


async def test_legacy_config_only_project_prefix_remains_valid_but_deleted_managed_id_does_not(coordinator, tmp_path):
    overlay = coordinator.data_dir / "projects" / "project_foo" / "config.yaml"
    overlay.parent.mkdir(parents=True)
    overlay.write_text(yaml.safe_dump({"cwd": str(tmp_path), "permissions": {"denied_tools": ["process_exec"]}}), encoding="utf-8")
    legacy = coordinator.create_session(project_id="project_foo")
    assert legacy["project_id"] == "project_foo" and legacy["cwd"] == str(tmp_path.resolve())
    managed = coordinator.workbench.projects.create("Managed", tmp_path)
    survivor = coordinator.create_session(project_id=managed["id"])
    config = coordinator.data_dir / "projects" / managed["id"] / "config.yaml"
    config.write_text(yaml.safe_dump({"cwd": str(tmp_path), "permissions": {"denied_tools": ["process_exec"]}}), encoding="utf-8")
    coordinator.workbench.projects.remove(managed["id"])
    with pytest.raises(ValueError, match="permanently removed"):
        coordinator.create_session(project_id=managed["id"])
    settings = load_config(coordinator.data_dir, project_id=survivor["project_id"], cwd=Path(survivor["cwd"]))
    assert "process_exec" in settings.denied_tools
    assert coordinator.store.get_session(survivor["id"])["project_id"] == managed["id"]
    # A previously read update cannot recreate the deleted registration.
    with pytest.raises(KeyError):
        coordinator.workbench.projects._save("late update", tmp_path, managed["id"])


async def test_stale_client_delete_requires_explicit_permanent_contract(client, coordinator, tmp_path):
    project = coordinator.workbench.projects.create("Stale frontend protection", tmp_path)
    session = coordinator.create_session(project_id=project["id"])
    for endpoint in (f"/sessions/{session['id']}", f"/workbench/projects/{project['id']}"):
        assert (await client.delete(endpoint)).status_code == 409
        assert (await client.delete(endpoint + "?permanent=false")).status_code == 409
    assert coordinator.store.get_session(session["id"])["project_id"] == project["id"]
    assert coordinator.workbench.projects.get(project["id"])["id"] == project["id"]
    for endpoint in ("/health", "/ui/bootstrap", "/capabilities"):
        response = await client.get(endpoint)
        assert response.status_code == 200
        assert {"session_permanent_delete_v1", "project_permanent_delete_v1"} <= set(response.json()["capabilities"])


async def test_committed_delete_reports_file_cleanup_failure_without_failing_the_action(client, coordinator, tmp_path, monkeypatch):
    sid = coordinator.create_session(tmp_path)["id"]
    owned = coordinator.attachments.register_bytes(sid, "blocked.txt", b"file to remove")
    original_unlink = Path.unlink

    def denied_unlink(path, *args, **kwargs):
        if str(path) == owned["path"]:
            raise PermissionError("user-private-value-must-never-appear")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", denied_unlink)
    response = await client.delete(f"/sessions/{sid}?permanent=true")
    assert response.status_code == 200 and response.json()["deleted"]
    assert response.json()["cleanup_warnings"] == [{"code": "artifact_cleanup_failed", "error_type": "PermissionError",
                                                     "unremoved_owned_artifacts": 1}]
    assert owned["path"] not in response.text and "user-private" not in response.text
    assert Path(owned["path"]).exists()
    assert (await client.get(f"/sessions/{sid}")).status_code == 404
    assert not coordinator.store.events(session_id=sid)
    assert sid not in coordinator._session_gates


async def test_parent_delete_keeps_branch_history_audit_kind_and_restored_attachments(tmp_path):
    data = tmp_path / "source"
    value = RunCoordinator(data, tools=[], credentials=NoCredentials())
    parent = value.create_session(tmp_path)["id"]
    shared = value.attachments.register_bytes(parent, "shared.txt", b"branch evidence")
    unshared = value.attachments.register_bytes(parent, "unshared.txt", b"forget this attachment")
    run, _ = value.store.create_run(parent)
    value.store.append_message(parent, run["id"], value.attachments.resolve_message(parent, "original", [shared["attachment_id"]]))
    call = ToolCall("material", "write", {"already_applied": True})
    execution = value.store.prepare_tool(parent, run["id"], call, risk="write", replay_safe=False)
    value.store.mark_dispatching(execution["id"])
    value.store.commit_tool(execution["id"], ToolResult("written once"))
    value.store.append_message(parent, run["id"], Message("assistant", "complete"))
    value.store.finish_run(run["id"], "completed")
    child = value.store.fork_session(parent, kind="side_chat")["id"]
    grandchild = value.store.fork_session(child)["id"]
    value.store.close_side_chat(child)
    child_history = value.store.history_records(child)
    grandchild_history = value.store.history_records(grandchild)
    await value.close()
    package = backup_bundle(data, tmp_path / "branch.zip")
    restored = tmp_path / "restored"
    restore_bundle(package, restored)
    value = RunCoordinator(restored, tools=[], credentials=NoCredentials())
    try:
        shared_path = value.attachments.resolve(child, shared["attachment_id"])[0]
        unshared_path = value.attachments.resolve(parent, unshared["attachment_id"])[0]
        await value.change_session_lifecycle(parent, "delete")
        assert value.store.history_records(child) == child_history
        assert value.store.history_records(grandchild) == grandchild_history
        lineage = value.store.lineage(child)
        assert lineage["parent_id"] is None and lineage["kind"] == "side_chat" and lineage["closed_at"]
        assert value.store.session_ancestors(grandchild) == [child]
        assert not value.store.has_unresolved(grandchild)
        assert value.attachments.resolve(child, shared["attachment_id"])[0] == shared_path
        assert value.attachments.resolve(grandchild, shared["attachment_id"])[0].read_bytes() == b"branch evidence"
        assert not unshared_path.exists()
        mappings = json.loads((restored / "restore-artifacts.json").read_text(encoding="utf-8"))["artifact_map"]
        assert shared["artifact_id"] in mappings and unshared["artifact_id"] not in mappings
        assert set(registered_artifacts(value.store.path)) == {shared["artifact_id"]}
        audit = next(event for event in value.store.events(session_id=child) if event["type"] == "session.ancestor_deleted")
        assert not audit["payload"]["replayable"]
        assert audit["payload"]["executions"][0]["id"] == execution["id"]
        assert value.store.connection.execute("SELECT COUNT(*) FROM tool_executions").fetchone()[0] == 0
        with pytest.raises(KeyError):
            value.store.get_run(run["id"])
        # Deleting this child propagates its inherited audit to the surviving branch.
        await value.change_session_lifecycle(child, "delete")
        assert value.store.session_ancestors(grandchild) == []
        audit = next(event for event in value.store.events(session_id=grandchild) if event["type"] == "session.ancestor_deleted")
        assert audit["payload"]["audit_events"][0]["type"] == "session.ancestor_deleted"
        assert value.attachments.resolve(grandchild, shared["attachment_id"])[0].exists()
        assert value.store.connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        await value.close()


def test_legacy_recycle_bin_migration_purges_idle_but_never_unknown_effects(tmp_path):
    path = tmp_path / "runtime.db"
    original = Store(path)
    idle = original.create_session(tmp_path)["id"]
    unknown = original.create_session(tmp_path)["id"]
    original.append_message(idle, None, Message("assistant", "old deleted transcript"))
    child = original.fork_session(idle, kind="side_chat")["id"]
    run, _ = original.create_run(unknown)
    execution = original.prepare_tool(unknown, run["id"], ToolCall("uncertain", "write", {}), risk="write", replay_safe=False)
    original.mark_dispatching(execution["id"])
    original.commit_tool(execution["id"], ToolResult("must reconcile", True, "transport", "unknown"))
    original.finish_run(run["id"], "needs_reconciliation")
    with original.transaction() as db:
        db.executemany("INSERT INTO session_lifecycle VALUES(?,NULL,?)", [(idle, time.time()), (unknown, time.time())])
    original.close()
    reopened = Store(path)
    try:
        with pytest.raises(KeyError):
            reopened.get_session(idle)
        assert reopened.history(child)[0].content == "old deleted transcript"
        assert reopened.lineage(child)["parent_id"] is None
        assert reopened.list_sessions(state="deleted") == []
        assert unknown not in {row["id"] for row in reopened.list_sessions(state="all")}
        assert reopened.tool_execution(execution["id"])["state"] == "outcome_unknown"
        with pytest.raises(SessionBusy):
            reopened.delete_session(unknown)
        reopened.reconcile_tool(execution["id"], ToolResult("verified applied"), evidence="offline effect inspection")
        assert reopened.delete_session(unknown) == {"deleted": True, "id": unknown}
        assert reopened.connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        reopened.close()
