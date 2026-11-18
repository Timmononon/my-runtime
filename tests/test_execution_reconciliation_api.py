"""Live reconciliation gates use the ledger while historical runs stay intact."""
from __future__ import annotations

import copy
import subprocess
from contextlib import asynccontextmanager

import httpx

from aster.api import create_app
from aster.domain import Message, ToolCall, ToolResult
from aster.service import RunCoordinator

TOKEN = "execution-reconciliation-offline-api-token-with-over-thirty-two-characters"
HEADERS = {"Authorization": "Bearer " + TOKEN}


class EmptyCredentials:
    def get(self, *args, **kwargs):
        return None

    def resolve(self, reference):
        raise ValueError("No credentials in the offline reconciliation test")


@asynccontextmanager
async def api(tmp_path):
    (tmp_path / "workspace").mkdir()

    def forbid_model(*args):
        raise AssertionError("Reconciliation inspection must not invoke a model")

    coordinator = RunCoordinator(tmp_path / "state", tools=[], credentials=EmptyCredentials(),
                                 model_factory=forbid_model, browser_enabled=False)
    app = create_app(coordinator, token=TOKEN, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost",
                                headers=HEADERS) as client:
        try:
            yield client, coordinator
        finally:
            await coordinator.close()


def unknown_run(store, session_id):
    run, _ = store.create_run(session_id, request_payload={"kind": "task"})
    call = ToolCall("original-remote-write", "offline_remote_write", {"target": "fixture"})
    store.append_message(session_id, run["id"], Message("user", "Original task request"))
    store.append_message(session_id, run["id"], Message("assistant", "", tool_calls=[call]))
    intent = store.prepare_tool(session_id, run["id"], call, risk="external", replay_safe=False)
    store.mark_dispatching(intent["id"])
    result = ToolResult("Original remote outcome was unknown", True, "network", "unknown")
    store.commit_tool(intent["id"], result)
    store.append_message(session_id, run["id"], Message("tool", result.content, tool_call_id=call.id,
                                                      metadata={"outcome": "unknown", "is_error": True}))
    store.finish_run(run["id"], "needs_reconciliation", error="Original remote outcome was unknown")
    return run["id"], intent["id"]


async def reconcile(client, execution_id):
    response = await client.post(f"/executions/{execution_id}/reconcile", json={
        "content": "Verified the operation did not occur", "evidence": "Offline external receipt checked",
        "is_error": True,
    })
    assert response.status_code == 200, response.text


async def test_reconcile_clears_live_gate_without_changing_historical_status_or_archive(tmp_path, monkeypatch):
    async with api(tmp_path) as (client, coordinator):
        store = coordinator.store
        sid = store.create_session(tmp_path / "workspace")["id"]
        rid, eid = unknown_run(store, sid)
        history_before = copy.deepcopy(store.history_records(sid))
        run_before = store.get_run(rid)

        def forbidden(*args, **kwargs):
            raise AssertionError("Management inspection must not spawn or contact an external service")

        # The HTTP client already exists; a new external client or subprocess is
        # forbidden throughout the actual read and existing reconciliation APIs.
        monkeypatch.setattr(httpx, "AsyncClient", forbidden)
        monkeypatch.setattr(subprocess, "Popen", forbidden)
        response = await client.get(f"/runs/{rid}")
        assert response.status_code == 200
        assert response.json()["requires_reconciliation"] is True
        assert response.json()["status"] == "needs_reconciliation"
        assert store.get_run(rid) == run_before and store.history_records(sid) == history_before
        assert "execution_reconciliation_status_v1" in (await client.get("/health")).json()["capabilities"]

        await reconcile(client, eid)
        response = await client.get(f"/runs/{rid}")
        assert response.json()["requires_reconciliation"] is False
        assert response.json()["status"] == "needs_reconciliation"
        assert store.get_run(rid) == run_before
        assert store.history_records(sid) == history_before
        assert store.tool_execution(eid)["state"] == "committed"
        assert len(store.list_runs(sid)) == 1


async def test_reconciled_child_stays_blocked_until_ancestor_outcome_is_resolved(tmp_path):
    async with api(tmp_path) as (client, coordinator):
        store = coordinator.store
        parent = store.create_session(tmp_path / "workspace")["id"]
        store.append_message(parent, None, Message("assistant", "Completed source turn before branching"))
        child = store.fork_session(parent, kind="side_chat")["id"]
        # Both sessions can have past effects. A later parent uncertainty also
        # constrains the existing child even after its own receipt is reconciled.
        child_run, child_execution = unknown_run(store, child)
        parent_run, parent_execution = unknown_run(store, parent)
        originals = {sid: copy.deepcopy(store.history_records(sid)) for sid in (parent, child)}
        runs = {rid: store.get_run(rid) for rid in (parent_run, child_run)}
        await reconcile(client, child_execution)
        child_response = await client.get(f"/runs/{child_run}")
        assert child_response.json()["requires_reconciliation"] is True
        assert child_response.json()["status"] == "needs_reconciliation"
        assert store.tool_execution(child_execution)["state"] == "committed"
        assert store.tool_execution(parent_execution)["state"] == "outcome_unknown"
        await reconcile(client, parent_execution)
        assert (await client.get(f"/runs/{child_run}")).json()["requires_reconciliation"] is False
        for rid, original in runs.items():
            assert store.get_run(rid) == original
        for sid, original in originals.items():
            assert store.history_records(sid) == original


async def test_completed_historical_run_reports_current_session_uncertainty_and_requires_auth(tmp_path):
    async with api(tmp_path) as (client, coordinator):
        store = coordinator.store
        sid = store.create_session(tmp_path / "workspace")["id"]
        completed, _ = store.create_run(sid, request_payload={"kind": "task"})
        store.finish_run(completed["id"], "completed", answer="Original completion")
        assert (await client.get(f"/runs/{completed['id']}")).json()["requires_reconciliation"] is False
        _, execution_id = unknown_run(store, sid)
        before = list(store.connection.iterdump())
        response = await client.get(f"/runs/{completed['id']}")
        assert response.json()["status"] == "completed"
        assert response.json()["requires_reconciliation"] is True
        assert list(store.connection.iterdump()) == before
        assert (await client.get(f"/runs/{completed['id']}", headers={"Authorization": ""})).status_code == 401
        await reconcile(client, execution_id)
        assert (await client.get(f"/runs/{completed['id']}")).json()["requires_reconciliation"] is False


async def test_empty_side_chat_session_exposes_inherited_gate_without_creating_a_run(tmp_path):
    async with api(tmp_path) as (client, coordinator):
        store = coordinator.store
        parent = store.create_session(tmp_path / "workspace")["id"]
        child = store.fork_session(parent, kind="side_chat")["id"]
        parent_run, execution_id = unknown_run(store, parent)
        history = {sid: copy.deepcopy(store.history_records(sid)) for sid in (parent, child)}
        original_run = store.get_run(parent_run)
        before = list(store.connection.iterdump())
        response = await client.get(f"/sessions/{child}")
        assert response.status_code == 200, response.text
        assert response.json()["requires_reconciliation"] is True
        assert response.json()["messages"] == [] and response.json()["runs"] == []
        assert list(store.connection.iterdump()) == before
        await reconcile(client, execution_id)
        response = await client.get(f"/sessions/{child}")
        assert response.json()["requires_reconciliation"] is False
        assert response.json()["messages"] == [] and response.json()["runs"] == []
        assert store.get_run(parent_run) == original_run
        for sid, original in history.items():
            assert store.history_records(sid) == original
