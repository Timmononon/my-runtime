"""Scoped GitHub source discovery and real Worker tool dispatch, all offline."""
import json
from contextlib import asynccontextmanager

import pytest
from test_github_core import REPO, issue, setup

from aster.github.normalize import event
from aster.state import StateError


@asynccontextmanager
async def runtime(tmp_path):
    coordinator, source, remote, clock = setup(tmp_path, fields={
        "enabled": True, "mode": "tasks", "allowed_actors": ["alice"],
        "token": "ghp_only_an_offline_source_fixture"})
    try:
        yield coordinator, source, remote, clock
    finally:
        await coordinator.close()


def worker(coordinator, source, clock, *, role="worker", request_id="new-worker"):
    value = event("issue", issue(clock), REPO)
    ticket = coordinator.tasks.create_ticket(source["source_session_id"], "Inspect issue", "Inspect the local checkout",
        workers=[{"title": "Inspect", "goal": "Inspect the local checkout", "role": role}],
        request_id=request_id, admission=coordinator.github.admission(coordinator.github._lease(source["id"]),
                                                                     value, explicit=True))
    return ticket, ticket["workers"][0]["session_id"]


async def test_worker_discovers_exact_source_offline_then_reads_and_prepares_under_real_intent(tmp_path):
    async with runtime(tmp_path) as (coordinator, source, remote, clock):
        ticket, sid = worker(coordinator, source, clock)
        context = coordinator.github.worker_context(sid)
        expected = {"source_id": source["id"], "repository": REPO, "numbers": [7]}
        assert context == {"ticket_id": ticket["id"], "sources": [expected]}
        result = await coordinator.execute_session_tool(sid, "github_read", {"resource": "sources"})
        assert not result.is_error and result.data == {"sources": [expected]} and remote.requests == []
        assert "ghp_only_an_offline_source_fixture" not in json.dumps([context, result.data])
        assert "credential" not in json.dumps(context)
        read = await coordinator.execute_session_tool(sid, "github_read", {"resource": "issue", "number": 7})
        assert not read.is_error and read.data["repository"] == REPO
        assert len(remote.requests) == 1 and remote.requests[0].method == "GET"
        prepared = await coordinator.execute_session_tool(sid, "github_prepare", {
            "kind": "comment", "number": 7, "body": "Local reviewable result"}, approved=True)
        assert not prepared.is_error, prepared
        assert prepared.data["source_id"] == source["id"] and prepared.data["ticket_id"] == ticket["id"]
        assert prepared.data["state"] == "draft" and len(remote.requests) == 1 and remote.posted == []
        execution = coordinator.store.tool_executions(coordinator.store.list_runs(sid)[0]["id"])[0]
        assert execution["tool_name"] == "github_prepare" and execution["state"] in {"committed", "checkpointed"}


@pytest.mark.parametrize("guessed", [REPO, "github", "107", "issue:107:external-event-id"])
async def test_wrong_source_id_explains_safe_discovery_and_does_not_call_network(tmp_path, guessed):
    async with runtime(tmp_path) as (coordinator, source, remote, clock):
        _, sid = worker(coordinator, source, clock)
        failed = await coordinator.execute_session_tool(sid, "github_read", {
            "source_id": guessed, "resource": "repository"})
        assert failed.is_error and failed.error_category == "validation"
        assert failed.data == {"reason": "source_not_found"} and "resource='sources'" in failed.content
        assert "repository names and Issue IDs are not source IDs" in failed.content
        assert remote.requests == []
        recovered = await coordinator.execute_session_tool(sid, "github_read", {
            "source_id": source["id"], "resource": "repository"})
        assert not recovered.is_error and len(remote.requests) == 1


async def test_multiple_owner_sources_require_explicit_selection_and_never_widen_worker_scope(tmp_path):
    async with runtime(tmp_path) as (coordinator, source, remote, clock):
        _, sid = worker(coordinator, source, clock)
        alternate = coordinator.github.create_source(source["source_session_id"], "another/repository")
        ambiguous = await coordinator.execute_session_tool(source["source_session_id"], "github_read", {"resource": "repository"})
        assert ambiguous.is_error and ambiguous.data == {"reason": "source_ambiguous"}
        listing = await coordinator.execute_session_tool(source["source_session_id"], "github_read", {"resource": "sources"})
        assert {s["source_id"] for s in listing.data["sources"]} == {source["id"], alternate["id"]}
        worker_listing = await coordinator.execute_session_tool(sid, "github_read", {"resource": "sources"})
        assert [s["source_id"] for s in worker_listing.data["sources"]] == [source["id"]]
        denied = await coordinator.execute_session_tool(sid, "github_read", {
            "source_id": alternate["id"], "resource": "repository"})
        assert denied.is_error and denied.error_category == "permission" and remote.requests == []


async def test_unrelated_conversation_discovers_nothing_and_cannot_select_another_source(tmp_path):
    async with runtime(tmp_path) as (coordinator, source, remote, _):
        sid = coordinator.create_session(tmp_path / "unrelated")["id"]
        listing = await coordinator.execute_session_tool(sid, "github_read", {"resource": "sources"})
        assert not listing.is_error and listing.data == {"sources": []}
        missing = await coordinator.execute_session_tool(sid, "github_read", {"resource": "repository"})
        assert missing.is_error and missing.data == {"reason": "source_unavailable"}
        denied = await coordinator.execute_session_tool(sid, "github_read", {
            "source_id": source["id"], "resource": "repository"})
        assert denied.is_error and denied.error_category == "permission" and remote.requests == []
        assert coordinator.github.worker_context(sid) is None


async def test_reader_discovers_and_reads_but_cannot_prepare_even_with_explicit_approval(tmp_path):
    async with runtime(tmp_path) as (coordinator, source, remote, clock):
        _, sid = worker(coordinator, source, clock, role="reader")
        listing = await coordinator.execute_session_tool(sid, "github_read", {"resource": "sources"})
        assert not listing.is_error and listing.data["sources"][0]["source_id"] == source["id"]
        denied = await coordinator.execute_session_tool(sid, "github_prepare", {
            "kind": "comment", "body": "No write authority", "number": 7}, approved=True)
        assert denied.is_error and denied.outcome == "not_executed"
        assert coordinator.github.drafts(source["id"]) == [] and remote.requests == []


@pytest.mark.parametrize("change", ["source", "cancel", "retire"])
async def test_worker_context_and_discovery_recheck_live_task_authority(tmp_path, change):
    async with runtime(tmp_path) as (coordinator, source, remote, clock):
        ticket, sid = worker(coordinator, source, clock)
        if change == "source":
            coordinator.github.update_source(source["id"], source["revision"], allowed_actors=["someone-else"])
        elif change == "cancel":
            await coordinator.tasks.cancel_ticket(ticket["id"])
        else:
            with coordinator.store.transaction() as db:
                db.execute("UPDATE local_task_bindings SET retired=1 WHERE session_id=?", (sid,))
        with pytest.raises((StateError, PermissionError)):
            coordinator.github.tool_sources(sid)
        with pytest.raises((StateError, PermissionError)):
            coordinator.github.worker_context(sid)
        assert remote.requests == []


async def test_archived_owner_cannot_use_discovery_or_source_inference(tmp_path):
    async with runtime(tmp_path) as (coordinator, source, remote, _):
        coordinator.github.update_source(source["id"], source["revision"], enabled=False)
        await coordinator.change_session_lifecycle(source["source_session_id"], "archive")
        with pytest.raises(StateError):
            coordinator.github.tool_sources(source["source_session_id"])
        with pytest.raises(StateError):
            coordinator.github.resolve_tool_source(source["source_session_id"])
        assert remote.requests == []


async def test_incomplete_draft_returns_argument_guidance_without_creating_a_draft(tmp_path):
    async with runtime(tmp_path) as (coordinator, source, remote, clock):
        _, sid = worker(coordinator, source, clock)
        failed = await coordinator.execute_session_tool(sid, "github_prepare", {
            "kind": "comment", "body": "Missing thread number"}, approved=True)
        assert failed.is_error and failed.error_category == "validation"
        assert "comment draft needs a thread number" in failed.content
        assert coordinator.github.drafts(source["id"]) == [] and remote.requests == []
