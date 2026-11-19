"""Live reconciliation gates use the ledger while historical runs stay intact."""
from __future__ import annotations

import asyncio
import copy
import subprocess
from contextlib import asynccontextmanager

import httpx
import pytest

from aster.api import create_app
from aster.customization.hooks import HookCommand, supervised_hook_executor
from aster.domain import Message, ToolCall, ToolContext, ToolResult, ToolSpec
from aster.engine.dispatcher import Dispatcher
from aster.policy import PermissionPolicy
from aster.service import RunCoordinator
from aster.state import ReconciliationRequired
from aster.tools.registry import Registry

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


async def reconciliation_flags(client, paths):
    flags = []
    for path in paths:
        response = await client.get(path)
        assert response.status_code == 200, response.text
        flags.append(response.json()["requires_reconciliation"])
    return flags


@pytest.mark.parametrize("outcome", ["known", "unknown"])
async def test_live_dispatch_after_turn_completion_needs_no_operator_action(tmp_path, outcome):
    async with api(tmp_path) as (client, coordinator):
        store = coordinator.store
        sid = store.create_session(tmp_path / "workspace")["id"]
        child = store.fork_session(sid, kind="side_chat")["id"]
        run, _ = store.create_run(sid, request_payload={"kind": "task"})
        rid = run["id"]
        # Automatic memory evaluations use the dispatcher after the user turn finishes.
        store.finish_run(rid, "completed", answer="Main turn completed")
        paths = [f"/sessions/{sid}", f"/runs/{rid}", f"/sessions/{child}"]
        started, finish = asyncio.Event(), asyncio.Event()

        class ExternalStep:
            spec = ToolSpec("post_turn_step", "Offline post-turn operation", {"type": "object"}, risk="external")

            async def execute(self, arguments, context):
                started.set()
                await finish.wait()
                return ToolResult("Offline receipt", is_error=outcome == "unknown", outcome=outcome)

        dispatcher = Dispatcher(Registry([ExternalStep()]), store, PermissionPolicy(allowed_tools={"post_turn_step"}))
        context = ToolContext(sid, rid, tmp_path, tmp_path / "artifacts", asyncio.Event())
        task = asyncio.create_task(dispatcher.execute([ToolCall("call", "post_turn_step", {})], context))
        try:
            await asyncio.wait_for(started.wait(), timeout=10)
            assert store.tool_executions(rid)[0]["state"] == "dispatching"
            assert await reconciliation_flags(client, paths) == [False, False, False]
            assert store.has_unresolved(sid) and store.has_unresolved(child)
            with pytest.raises(ReconciliationRequired):
                store.create_run(child, initial_status="queued")
            queued, _ = store.create_run(sid, initial_status="queued", request_payload={"kind": "task"})
            assert queued["status"] == "queued"
        finally:
            finish.set()
            await task
        assert await reconciliation_flags(client, paths) == [outcome == "unknown"] * len(paths)
        assert store.get_run(rid)["status"] == "completed"
        if outcome == "unknown":
            with pytest.raises(ReconciliationRequired):
                store.create_run(sid, initial_status="queued")


async def test_abandoned_hook_dispatch_needs_reconciliation_even_with_a_running_status(tmp_path):
    async with api(tmp_path) as (client, coordinator):
        store = coordinator.store
        sid = store.create_session(tmp_path / "workspace")["id"]
        run, _ = store.create_run(sid, request_payload={"kind": "task"})
        rid = run["id"]
        paths = [f"/sessions/{sid}", f"/runs/{rid}"]

        async def interrupt(event, payload):
            assert event == "hook.started"
            assert await reconciliation_flags(client, paths) == [False, False]
            raise asyncio.CancelledError()

        context = ToolContext(sid, rid, tmp_path, tmp_path / "artifacts", asyncio.Event(),
                              emit=interrupt, services={"store": store})
        with pytest.raises(asyncio.CancelledError):
            await supervised_hook_executor(None)(HookCommand("Stop", "unused-offline"), {}, context)
        assert store.get_run(rid)["status"] == "running"
        assert store.tool_executions(rid)[0]["state"] == "dispatching"
        assert await reconciliation_flags(client, paths) == [True, True]


async def test_ownership_cannot_hide_an_abandoned_sibling_or_a_committed_unknown(tmp_path):
    async with api(tmp_path) as (client, coordinator):
        store = coordinator.store
        sid = store.create_session(tmp_path / "workspace")["id"]
        run, _ = store.create_run(sid, request_payload={"kind": "task"})
        rid = run["id"]
        paths = [f"/sessions/{sid}", f"/runs/{rid}"]
        owned, abandoned = [store.prepare_tool(sid, rid, ToolCall(key, "offline_step", {}),
                                                risk="external", replay_safe=False)["id"]
                            for key in ("live", "orphan")]
        with store.tool_dispatch(owned):
            store.mark_dispatching(abandoned)
            assert await reconciliation_flags(client, paths) == [True, True]
            store.commit_tool(abandoned, ToolResult("Verified receipt"))
            assert await reconciliation_flags(client, paths) == [False, False]
            store.commit_tool(owned, ToolResult("Missing remote receipt", True, "network", "unknown"))
            assert await reconciliation_flags(client, paths) == [True, True]


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
