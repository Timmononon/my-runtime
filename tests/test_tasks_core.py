import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

from aster.domain import ModelTurn, ToolCall, ToolResult, ToolSpec
from aster.engine import FakeModel
from aster.service import RunCoordinator
from aster.state.store import SessionBusy, StateError
from aster.tasks import TaskService


class Credentials:
    def get(self, *args, **kwargs):
        return None

    def resolve(self, reference):
        raise ValueError("No test credential")


def coordinator(tmp_path, factory=None, tools=None):
    value = RunCoordinator(tmp_path / "data", model_factory=factory or (lambda settings: FakeModel([ModelTurn("done")])),
                           tools=tools or [], credentials=Credentials(), browser_enabled=False)
    if not hasattr(value, "tasks"):
        value.tasks = TaskService(value)
    return value


async def settle(value, identity):
    for _ in range(200):
        await value.tasks.tick()
        item = value.tasks.get_ticket(identity)
        if item["state"] in {"completed", "failed", "cancelled", "needs_reconciliation"}:
            return item
        await asyncio.sleep(0.01)
    raise AssertionError("Task did not finish")


async def test_task_workers_are_empty_independent_parallel_and_summarized(tmp_path, monkeypatch):
    def unavailable_system_probe():
        raise AssertionError("Worker admission must not query Windows WMI")

    monkeypatch.setattr("platform.system", unavailable_system_probe)
    entered, release = [], asyncio.Event()

    async def turn(messages, tools):
        entered.append(messages)
        if len(entered) == 2:
            release.set()
        await asyncio.wait_for(release.wait(), 2)
        return ModelTurn("verified " + messages[-1].content)

    value = coordinator(tmp_path, lambda settings: FakeModel([turn]))
    try:
        sid = value.create_session(tmp_path)["id"]
        item = value.tasks.create_ticket(sid, "Parallel work", "two disjoint assignments", workers=[
            {"title": "one", "goal": "first"}, {"title": "two", "goal": "second", "role": "reader"}], request_id="once")
        repeated = value.tasks.create_ticket(sid, "different", "ignored", request_id="once")
        assert repeated["id"] == item["id"]
        assert len({worker["session_id"] for worker in item["workers"]}) == 2
        assert len({worker["cwd"] for worker in item["workers"]}) == 2
        assert all(not value.store.history(worker["session_id"]) for worker in item["workers"])
        finished = await settle(value, item["id"])
        assert finished["state"] == "completed"
        assert "verified first" in finished["summary"] and "verified second" in finished["summary"]
        assert len(entered) == 2
        assert value.tasks.overview()["counts"]["completed"] == 1
    finally:
        await value.close()


async def test_task_worker_holds_session_gate_against_operator_actions(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()

    async def turn(messages, tools):
        entered.set()
        await release.wait()
        return ModelTurn("done")

    value = coordinator(tmp_path, lambda settings: FakeModel([turn]))
    try:
        source_id = value.create_session(tmp_path)["id"]
        item = value.tasks.create_ticket(source_id, "Task", "Hold the worker runner")
        await value.tasks.tick()
        await asyncio.wait_for(entered.wait(), 2)
        worker_id = item["workers"][0]["session_id"]
        assert value._session_gates[worker_id].locked()
        with pytest.raises(SessionBusy, match="already executing"):
            await value.execute_session_tool(worker_id, "task_list", {})
        assert len(value.store.list_runs(worker_id)) == 1
        release.set()
        assert (await settle(value, item["id"]))["state"] == "completed"
        assert not value._session_gates[worker_id].locked()
    finally:
        release.set()
        await value.close()


async def test_observe_off_auto_revision_and_candidate_idempotence(tmp_path):
    value = coordinator(tmp_path)
    try:
        sid = value.create_session(tmp_path)["id"]
        settings = value.tasks.update_settings(0, "observe", [{"id": "reports", "enabled": True, "contains": ["report"]}])
        observed = value.tasks.evaluate_candidate(sid, "create report", "event-1")
        assert observed["decision"] == "observed" and not value.tasks.list_tickets()
        assert value.tasks.evaluate_candidate(sid, "different report", "event-1")["id"] == observed["id"]
        with pytest.raises(StateError):
            value.tasks.update_settings(0, "auto", [])
        value.tasks.update_settings(settings["revision"], "auto", settings["rules"])
        accepted = value.tasks.evaluate_candidate(sid, "create report", "event-2")
        assert accepted["decision"] == "accepted"
        assert (await settle(value, accepted["ticket_id"]))["state"] == "completed"
        current = value.tasks.settings()
        value.tasks.update_settings(current["revision"], "off", current["rules"])
        assert value.tasks.evaluate_candidate(sid, "create report", "event-3")["decision"] == "ignored"
    finally:
        await value.close()


async def test_policy_changed_before_proactive_launch_prevents_worker(tmp_path):
    calls = []
    value = coordinator(tmp_path, lambda settings: calls.append(settings) or FakeModel([ModelTurn("bad")]))
    try:
        sid = value.create_session(tmp_path)["id"]
        policy = value.tasks.update_settings(0, "auto", [{"id": "x", "enabled": True, "contains": ["report"]}])
        item = value.tasks.evaluate_candidate(sid, "report", "candidate")
        value.tasks.update_settings(policy["revision"], "observe", policy["rules"])
        result = await settle(value, item["ticket_id"])
        assert result["state"] == "failed" and not calls
    finally:
        await value.close()


async def test_followup_survives_restart_and_does_not_duplicate_submission(tmp_path):
    value = coordinator(tmp_path)
    sid = value.create_session(tmp_path)["id"]
    item = value.tasks.create_ticket(sid, "Task", "first", request_id="task")
    await settle(value, item["id"])
    await value.tasks.add_input(item["id"], "new requirement", request_id="followup")
    await value.close()
    value = coordinator(tmp_path)
    try:
        await value.tasks.add_input(item["id"], "ignored repeat", request_id="followup")
        result = await settle(value, item["id"])
        runs = value.store.list_runs(item["workers"][0]["session_id"])
        assert result["state"] == "completed" and len(runs) == 2
        assert value.store.run_request(runs[0]["id"])["text"].endswith("new requirement")
    finally:
        await value.close()


async def test_unknown_parent_effect_blocks_launch_and_worker_retry(tmp_path):
    value = coordinator(tmp_path)
    try:
        sid = value.create_session(tmp_path)["id"]
        historical = value.tasks.create_ticket(sid, "Finished", "known result")
        await settle(value, historical["id"])
        item = value.tasks.create_ticket(sid, "Task", "goal")
        run, _ = value.store.create_run(sid)
        execution = value.store.prepare_tool(sid, run["id"], ToolCall("write", "test_write", {}), risk="write", replay_safe=False)
        value.store.mark_dispatching(execution["id"])
        value.store.commit_tool(execution["id"], ToolResult("unknown", True, outcome="unknown"))
        value.store.set_run_status(run["id"], "needs_reconciliation")
        await value.tasks.tick()
        assert value.tasks.get_ticket(item["id"])["workers"][0]["run_id"] is None
        assert value.tasks.get_ticket(item["id"])["state"] == "needs_reconciliation"
        assert not value.tasks.get_ticket(item["id"])["can_retry"]
        history = value.tasks.get_ticket(historical["id"])
        assert history["state"] == "completed" and not history["attention"]
        assert not history["can_add_input"]
        with pytest.raises(StateError):
            value.tasks.worker_guard(item["workers"][0]["session_id"])
        with pytest.raises(StateError):
            value.tasks.create_ticket(sid, "no", "new work")
    finally:
        await value.close()


async def test_parent_snapshot_and_other_agent_cannot_expand_permissions(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "config.yaml").write_text("permissions:\n  auto_approve_tools: [file_write, file_read]\n", encoding="utf-8")
    agent = data / "agents" / "reviewer"
    agent.mkdir(parents=True)
    (agent / "config.yaml").write_text("permissions:\n  denied_tools: [file_write]\n", encoding="utf-8")
    value = coordinator(tmp_path)
    try:
        sid = value.create_session(tmp_path)["id"]
        ticket = value.tasks.create_ticket(sid, "Review", "inspect", workers=[{"title": "review", "goal": "inspect", "agent_id": "reviewer"}])
        snapshot = value.tasks.worker_execution(ticket["workers"][0]["session_id"])
        assert "file_write" in snapshot["snapshot"]["permissions"]["denied_tools"]
        assert "file_write" not in snapshot["snapshot"]["permissions"]["auto_approve_tools"]
        assert snapshot["snapshot"]["allowed_roots"] == [str(tmp_path)]
        with pytest.raises(StateError):
            value.tasks.create_ticket(ticket["workers"][0]["session_id"], "nested", "forbidden")
        with pytest.raises(SessionBusy):
            value.tasks.check_lifecycle({sid}, "delete")
        await value.tasks.cancel_ticket(ticket["id"])
        value.tasks.check_lifecycle({sid}, "delete")
    finally:
        await value.close()


async def test_worker_budgets_partitioned_and_no_management_model_calls(tmp_path):
    calls = []
    value = coordinator(tmp_path, lambda settings: calls.append(settings) or FakeModel([ModelTurn("done")]))
    try:
        sid = value.create_session(tmp_path)["id"]
        ticket = value.tasks.create_ticket(sid, "Bounded", "goal", workers=[{"title": "a", "goal": "a"}, {"title": "b", "goal": "b"}])
        pins = [value.tasks.worker_execution(worker["session_id"]) for worker in ticket["workers"]]
        assert sum(pin["snapshot"]["runtime"]["max_model_requests"] for pin in pins) == 200
        value.tasks.settings()
        value.tasks.list_tickets()
        value.tasks.overview()
        value.tasks.list_candidates()
        assert not calls
        with value.store.transaction() as db:
            value.tasks.cleanup_session(sid, db)
        assert not value.tasks.list_tickets()
    finally:
        await value.close()


async def test_deleting_worker_preserves_sibling_reader_and_deleting_source_retires_it(tmp_path):
    value = coordinator(tmp_path)
    try:
        sid = value.create_session(tmp_path)["id"]
        ticket = value.tasks.create_ticket(sid, "Two readers", "inspect", workers=[
            {"title": "one", "goal": "one", "role": "reader"}, {"title": "two", "goal": "two", "role": "reader"}])
        await settle(value, ticket["id"])
        first, second = [worker["session_id"] for worker in ticket["workers"]]
        value.store.delete_session(first)
        assert value.tasks.worker_binding(second)["reader"]
        assert not value.tasks.worker_binding(second)["retired"]
        value.tasks.worker_guard(second)
        assert len(value.tasks.get_ticket(ticket["id"])["workers"]) == 2
        value.store.delete_session(sid)
        assert value.tasks.worker_binding(second)["reader"]
        assert value.tasks.worker_binding(second)["retired"]
        with pytest.raises(StateError):
            value.tasks.worker_guard(second)
        with pytest.raises(StateError):
            value.tasks.worker_execution(second)
    finally:
        await value.close()


async def test_narrow_active_tool_scope_is_permanent_in_worker_snapshot(tmp_path):
    value = coordinator(tmp_path)
    try:
        sid = value.create_session(tmp_path)["id"]
        ticket = value.tasks.create_ticket(sid, "Limited", "read", tool_allowance=["file_read", "directory_list"])
        snapshot = value.tasks.worker_execution(ticket["workers"][0]["session_id"])
        assert snapshot["snapshot"]["permissions"]["allowed_tools"] == ["directory_list", "file_read"]
    finally:
        await value.close()


async def test_completed_unacknowledged_run_reattaches_even_when_budget_exhausted(tmp_path):
    value = coordinator(tmp_path)
    try:
        sid = value.create_session(tmp_path)["id"]
        ticket = value.tasks.create_ticket(sid, "Crash boundary", "one request")
        worker = value.tasks._workers(ticket["id"])[0]
        snapshot = json.loads(worker["snapshot_json"])
        snapshot["snapshot"]["runtime"]["max_model_requests"] = 1
        with value.store.transaction() as db:
            db.execute("UPDATE local_task_workers SET snapshot_json=? WHERE id=?", (json.dumps(snapshot), worker["id"]))
        Path(worker["cwd"]).mkdir(parents=True)
        run = await value.submit(worker["session_id"], "one request", request_id=f"local-task:{worker['id']}:0", execution_snapshot=snapshot)
        await value.wait(run["id"], timeout=3)
        assert len(value.store.usage(run["id"])) == 1
        # Simulate submit committed and completed before its Task acknowledgement.
        assert value.tasks._workers(ticket["id"])[0]["run_id"] is None
        await value.tasks.tick()
        result = value.tasks.get_ticket(ticket["id"])
        assert result["state"] == "completed"
        assert result["workers"][0]["run_id"] == run["id"]
        assert len(value.store.list_runs(worker["session_id"])) == 1
    finally:
        await value.close()


async def test_cancellation_waits_for_runtime_and_never_reports_success(tmp_path):
    entered = asyncio.Event()

    async def never(messages, tools):
        entered.set()
        await asyncio.Event().wait()

    value = coordinator(tmp_path, lambda settings: FakeModel([never]))
    try:
        sid = value.create_session(tmp_path)["id"]
        ticket = value.tasks.create_ticket(sid, "Cancel", "wait")
        await value.tasks.tick()
        await asyncio.wait_for(entered.wait(), 2)
        result = await value.tasks.cancel_ticket(ticket["id"])
        assert result["state"] in {"working", "cancelled"}
        final = await settle(value, ticket["id"])
        assert final["state"] == "cancelled"
        assert all(worker["state"] == "cancelled" for worker in final["workers"])
        assert not final["can_add_input"]
    finally:
        await value.close()


async def test_known_failed_worker_retry_uses_pinned_identity_and_retains_evidence(tmp_path):
    attempts = []

    def factory(settings):
        attempts.append(settings)
        return FakeModel([RuntimeError("simulated failure")]) if len(attempts) == 1 else FakeModel([ModelTurn("recovered")])

    value = coordinator(tmp_path, factory)
    try:
        sid = value.create_session(tmp_path)["id"]
        ticket = value.tasks.create_ticket(sid, "Retry", "continue safely")
        failed = await settle(value, ticket["id"])
        assert failed["state"] == "failed" and failed["can_retry"]
        old = failed["workers"][0]["run_id"]
        await value.tasks.retry_ticket(ticket["id"], expected_revision=failed["revision"])
        recovered = await settle(value, ticket["id"])
        assert recovered["state"] == "completed"
        assert value.store.get_run(old)["status"] == "failed"
        assert len(value.store.list_runs(ticket["workers"][0]["session_id"])) == 2
        assert attempts[0]["model"] == attempts[1]["model"]
        assert attempts[1]["runtime"]["max_model_requests"] < attempts[0]["runtime"]["max_model_requests"]
    finally:
        await value.close()


async def test_originating_run_ceiling_and_selection_survive_later_source_edits(tmp_path):
    value = coordinator(tmp_path)
    try:
        sid = value.create_session(tmp_path)["id"]
        source = await value.submit(sid, "source", overrides={"permissions": {"allowed_tools": ["file_read"]}, "model": "frozen-model"})
        ticket = value.tasks.create_ticket(sid, "Pinned", "read", parent_run_id=source["id"])
        snapshot = value.tasks.worker_execution(ticket["workers"][0]["session_id"])
        assert snapshot["snapshot"]["permissions"]["allowed_tools"] == ["file_read"]
        assert snapshot["selection"]["model"] == "frozen-model"
        assert snapshot["snapshot"]["model"] == "frozen-model"
    finally:
        await value.close()


async def test_unattended_denied_write_is_failed_task_even_if_model_turn_completed(tmp_path):
    class Write:
        spec = ToolSpec("controlled_write", "Controlled write", {"type": "object", "properties": {}, "additionalProperties": False}, risk="write")

        async def execute(self, arguments, context):
            raise AssertionError("An unapproved write must never execute")

    value = coordinator(tmp_path, lambda settings: FakeModel([
        ModelTurn(tool_calls=[ToolCall("write", "controlled_write", {})]), ModelTurn("I could not write it.")]), [Write()])
    try:
        (value.data_dir / "config.yaml").write_text("permissions:\n  denied_tools: [controlled_write]\n", encoding="utf-8")
        sid = value.create_session(tmp_path)["id"]
        ticket = value.tasks.create_ticket(sid, "Write", "produce a file", unattended=True)
        result = await settle(value, ticket["id"])
        assert result["state"] == "failed" and result["attention"]
        assert result["workers"][0]["run_status"] == "completed"
        assert result["workers"][0]["state"] == "failed"
    finally:
        await value.close()


async def test_recovered_permission_result_clears_same_tool_but_validation_error_does_not_fail_task(tmp_path):
    class ControlledRead:
        spec = ToolSpec("controlled_read", "Controlled read", {"type": "object", "properties": {}, "additionalProperties": False})

        def __init__(self):
            self.calls = 0

        async def execute(self, arguments, context):
            self.calls += 1
            if self.calls == 1:
                return ToolResult("No access yet", True, "permission", "not_executed")
            return ToolResult("Read the actual result")

    value = coordinator(tmp_path, lambda settings: FakeModel([
        ModelTurn(tool_calls=[ToolCall("first", "controlled_read", {})]),
        ModelTurn(tool_calls=[ToolCall("second", "controlled_read", {})]), ModelTurn("verified")]), [ControlledRead()])
    try:
        sid = value.create_session(tmp_path)["id"]
        ticket = value.tasks.create_ticket(sid, "Read", "verify")
        result = await settle(value, ticket["id"])
        assert result["state"] == "completed" and not result["attention"]
    finally:
        await value.close()


async def test_successful_same_tool_with_different_arguments_does_not_clear_denied_intent(tmp_path):
    class Read:
        spec = ToolSpec("read_named", "Read named item", {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]})

        async def execute(self, arguments, context):
            if arguments["name"] == "A":
                return ToolResult("denied", True, "permission", "not_executed")
            return ToolResult("B exists")

    value = coordinator(tmp_path, lambda settings: FakeModel([
        ModelTurn(tool_calls=[ToolCall("a", "read_named", {"name": "A"})]),
        ModelTurn(tool_calls=[ToolCall("b", "read_named", {"name": "B"})]), ModelTurn("B worked")]), [Read()])
    try:
        sid = value.create_session(tmp_path)["id"]
        ticket = value.tasks.create_ticket(sid, "Read A", "read A")
        result = await settle(value, ticket["id"])
        assert result["state"] == "failed" and result["attention"]
    finally:
        await value.close()


@pytest.mark.windows
@pytest.mark.skipif(os.name != "nt", reason="Windows directory junction ownership")
async def test_owned_worker_junction_retarget_is_blocked_before_execution(tmp_path):
    value = coordinator(tmp_path)
    link = None
    try:
        source = tmp_path / "source"
        outside = tmp_path / "outside"
        outside.mkdir()
        sid = value.create_session(source)["id"]
        ticket = value.tasks.create_ticket(sid, "Owned", "inspect")
        worker = ticket["workers"][0]
        link = Path(worker["cwd"])
        link.parent.mkdir(parents=True)
        result = await asyncio.to_thread(subprocess.run, ["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True,
                                         creationflags=subprocess.CREATE_NO_WINDOW, check=False)
        assert result.returncode == 0, result.stderr.decode(errors="replace")
        with pytest.raises(StateError, match="redirected"):
            value.tasks.worker_guard(worker["session_id"])
        await value.tasks.tick()
        assert value.tasks.get_ticket(ticket["id"])["state"] == "failed"
        assert not value.store.list_runs(worker["session_id"])
    finally:
        if link is not None and link.is_junction():
            link.rmdir()
        await value.close()
