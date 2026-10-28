import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from aster.automation import AutomationService
from aster.domain import ToolCall, ToolContext, ToolResult
from aster.state.store import Store
from aster.tools.interaction import ScheduleTool, SetWorkPlanTool


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 10, 8, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Coordinator:
    def __init__(self, store, hold=False):
        self.store = store
        self.calls = []
        self.hold = hold

    async def submit(self, session_id, text, request_id=None, unattended=False):
        existing = next(
            (run for run in self.store.list_runs(session_id) if run["request_id"] == request_id), None
        )
        if existing:
            return existing
        run, _ = self.store.create_run(session_id, request_id=request_id, initial_status="queued")
        self.calls.append(
            {"run_id": run["id"], "text": text, "unattended": unattended, "request_id": request_id}
        )
        if not self.hold:
            self.store.set_run_status(run["id"], "completed", answer="Checked: unchanged result")
        return self.store.get_run(run["id"])


@pytest.fixture
def environment(tmp_path):
    store = Store(tmp_path / "runtime.db")
    session = store.create_session(tmp_path)
    clock = Clock()
    coordinator = Coordinator(store)
    yield store, session, clock, coordinator
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("policy,expected", [("skip", 0), ("coalesce", 1), ("bounded_catchup", 3)])
async def test_missed_run_policies_and_unattended(environment, policy, expected):
    store, session, clock, coordinator = environment
    service = AutomationService(store, coordinator, clock=clock)
    job = service.create_job(
        "schedule",
        session["id"],
        "Check",
        {
            "cron": "* * * * *",
            "misfire": policy,
            "grace_seconds": 10,
            "overlap": "queue",
            "max_pending": 5,
            "catchup_limit": 3,
        },
    )
    clock.advance(600)
    await service.tick()
    assert len(coordinator.calls) == expected
    assert all(call["unattended"] for call in coordinator.calls)
    await service.tick()
    assert len(coordinator.calls) == expected
    assert service.get_job(job["id"])["next_fire"] > clock()


@pytest.mark.asyncio
async def test_claim_recovery_and_submit_idempotence(environment):
    store, session, clock, coordinator = environment
    service = AutomationService(store, coordinator, clock=clock)
    job = service.create_job("schedule", session["id"], "Check", {"cron": "* * * * *"})
    clock.advance(60)
    service._schedule(job, clock())
    claim = service.receipts()[0]
    # Crash after coordinator commits a run but before automation stores run_id.
    execution_session = store.automation_session(session["id"], f"automation:{job['id']}:trigger:{claim['id']}")
    await coordinator.submit(execution_session, claim["prompt"], request_id=claim["id"], unattended=True)
    restarted = AutomationService(store, coordinator, clock=clock)
    await restarted.tick()
    assert len(coordinator.calls) == 1
    assert restarted.receipts()[0]["state"] == "completed"


@pytest.mark.asyncio
async def test_overlap_daily_quota_delete_and_scope(environment, tmp_path):
    store, session, clock, _ = environment
    coordinator = Coordinator(store, hold=True)
    service = AutomationService(store, coordinator, clock=clock)
    job = service.create_job("schedule", session["id"], "Check", {"cron": "* * * * *", "max_runs_per_day": 1})
    clock.advance(60)
    await service.tick()
    clock.advance(60)
    await service.tick()
    assert len(coordinator.calls) == 1
    assert service.receipts()[-1]["state"] == "skipped"
    other = store.create_session(tmp_path)
    ctx = ToolContext(
        other["id"], "unused", tmp_path, tmp_path, asyncio.Event(), services={"automation": service}
    )
    result = await ScheduleTool("delete").execute({"job_id": job["id"]}, ctx)
    assert result.is_error
    service.delete_job(job["id"])
    assert service.list_jobs() == []
    assert service.receipts(job["id"])


@pytest.mark.asyncio
async def test_real_file_watcher_stability_restart_and_internal_ignore(environment, tmp_path):
    store, session, clock, coordinator = environment
    watch = tmp_path / "inbox"
    watch.mkdir()
    artifacts = watch / "artifacts"
    artifacts.mkdir()
    service = AutomationService(store, coordinator, clock=clock, ignored_roots=[artifacts])
    job = service.create_job(
        "watcher",
        session["id"],
        "Read stable file",
        {"path": str(watch), "recursive": True, "debounce_seconds": 1, "stable_seconds": 2},
    )
    await service.tick()
    document = watch / "中文.txt"
    document.write_text("first", encoding="utf-8")
    (artifacts / "own.txt").write_text("ignore", encoding="utf-8")
    (watch / ".temporary").write_text("ignore", encoding="utf-8")
    clock.advance(1)
    await service.tick()
    assert not coordinator.calls
    document.write_text("not yet stable", encoding="utf-8")
    clock.advance(1)
    await service.tick()
    clock.advance(1)
    await service.tick()
    assert not coordinator.calls
    restarted = AutomationService(store, coordinator, clock=clock, ignored_roots=[artifacts])
    clock.advance(2)
    await restarted.tick()
    assert len(coordinator.calls) == 1
    assert "中文.txt" in coordinator.calls[0]["text"]
    clock.advance(5)
    await restarted.tick()
    assert len(coordinator.calls) == 1
    assert len(restarted.receipts(job["id"])) == 1


@pytest.mark.asyncio
async def test_watcher_does_not_retrigger_own_outputs(environment, tmp_path):
    store, session, clock, coordinator = environment
    service = AutomationService(store, coordinator, clock=clock)
    job = service.create_job("watcher", session["id"], "Process", {"path": str(tmp_path), "pattern": "*.txt"})
    await service.tick()
    first = tmp_path / "first.txt"
    first.write_text("incoming", encoding="utf-8")
    clock.advance(1)
    await service.tick()
    clock.advance(3)
    await service.tick()
    run_id = coordinator.calls[0]["run_id"]
    output = tmp_path / "generated.txt"
    output.write_text("own output", encoding="utf-8")
    intent = store.prepare_tool(
        session["id"], run_id, ToolCall("write", "file_write", {}), risk="write", replay_safe=False
    )
    store.mark_dispatching(intent["id"])
    store.commit_tool(intent["id"], ToolResult("written", data={"path": str(output)}))
    service._mark_self_outputs(job, run_id, clock())
    clock.advance(4)
    await service.tick()
    clock.advance(4)
    await service.tick()
    assert len(coordinator.calls) == 1


@pytest.mark.asyncio
async def test_heartbeat_active_hours_baseline_and_quiet_unchanged(environment, tmp_path):
    store, session, clock, coordinator = environment
    target = tmp_path / "target.txt"
    target.write_text("one", encoding="utf-8")
    delivered = []
    service = AutomationService(
        store, coordinator, clock=clock, event_consumer=lambda event: delivered.append(event)
    )
    service.create_job(
        "heartbeat",
        session["id"],
        "Summarize changes",
        {
            "target": {"kind": "file", "path": str(target)},
            "interval_seconds": 30,
            "active_hours": {"start": "08:00", "end": "20:00"},
        },
    )
    await service.tick()
    assert not coordinator.calls
    event_count = len(store.events())
    clock.advance(30)
    await service.tick()
    assert len(store.events()) == event_count
    target.write_text("changed content", encoding="utf-8")
    clock.advance(30)
    await service.tick()
    assert len(coordinator.calls) == 1
    results = [event for event in delivered if event["type"] == "automation.result"]
    assert len(results) == 1
    clock.advance(30)
    await service.tick()
    assert len([event for event in delivered if event["type"] == "automation.result"]) == 1
    clock.advance(13 * 3600)
    target.write_text("outside hours", encoding="utf-8")
    await service.tick()
    assert len(coordinator.calls) == 1


@pytest.mark.asyncio
async def test_plan_cannot_mark_runtime_complete(environment, tmp_path):
    store, session, _, _ = environment
    run, _ = store.create_run(session["id"])
    ctx = ToolContext(
        session["id"], run["id"], tmp_path, tmp_path, asyncio.Event(), services={"store": store}
    )
    tool = SetWorkPlanTool()
    result = await tool.execute(
        {
            "steps": [
                {"id": "a", "text": "Read", "status": "completed"},
                {"id": "b", "text": "Write", "status": "completed"},
            ]
        },
        ctx,
    )
    assert not result.is_error
    assert store.get_plan(session["id"])["snapshot"]["lifecycle"] == "active"
    assert store.get_run(run["id"])["status"] == "running"


@pytest.mark.asyncio
async def test_start_close_are_idempotent(environment):
    store, _, clock, coordinator = environment
    service = AutomationService(store, coordinator, clock=clock, poll_seconds=0.01)
    await service.start()
    task = service._task
    await service.start()
    assert service._task is task
    await service.close()
    await service.close()
    assert task.done()
