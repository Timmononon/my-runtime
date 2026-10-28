from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from aster.automation.service import AutomationService, _next_cron
from aster.state import Store


def instant(text, zone):
    return datetime.fromisoformat(text).replace(tzinfo=ZoneInfo(zone)).timestamp()


def test_spring_nonexistent_wall_time_is_skipped():
    zone = "America/New_York"
    next_time = _next_cron("30 2 * * *", zone, instant("2026-03-07T03:00", zone))
    assert datetime.fromtimestamp(next_time, ZoneInfo(zone)).isoformat() == "2026-03-09T02:30:00-04:00"


def test_fall_ambiguous_wall_time_runs_once_at_first_occurrence():
    zone = "America/New_York"
    first = _next_cron("30 1 * * *", zone, instant("2026-10-31T03:00", zone))
    second = _next_cron("30 1 * * *", zone, first)
    assert datetime.fromtimestamp(first, ZoneInfo(zone)).isoformat() == "2026-11-01T01:30:00-04:00"
    assert datetime.fromtimestamp(second, ZoneInfo(zone)).isoformat() == "2026-11-02T01:30:00-05:00"
    assert second - first == 25 * 3600


@pytest.mark.asyncio
async def test_timezone_change_sleep_and_restart_use_persisted_instants(tmp_path):
    now = [instant("2026-10-30T08:00", "America/New_York")]
    store = Store(tmp_path / "state.db")
    session = store.create_session(tmp_path)
    calls = []

    class Coordinator:
        async def submit(self, sid, prompt, request_id=None, unattended=False):
            existing = next((run for run in store.list_runs(sid) if run["request_id"] == request_id), None)
            if existing:
                return existing
            run, _ = store.create_run(sid, request_id=request_id, initial_status="queued")
            calls.append((run["id"], unattended))
            store.set_run_status(run["id"], "completed", answer="Done")
            return store.get_run(run["id"])

    coordinator = Coordinator()
    try:
        service = AutomationService(store, coordinator, clock=lambda: now[0])
        job = service.create_job(
            "schedule",
            session["id"],
            "Check",
            {
                "cron": "30 1 * * *",
                "timezone": "America/New_York",
                "misfire": "bounded_catchup",
                "catchup_limit": 3,
                "overlap": "queue",
                "max_pending": 3,
            },
        )
        # Sleep across both the fall-back date and a service restart.
        now[0] = instant("2026-11-02T03:00", "America/New_York")
        restarted = AutomationService(store, coordinator, clock=lambda: now[0])
        await restarted.tick()
        assert len(calls) == 3 and all(unattended for _, unattended in calls)
        times = [
            datetime.fromtimestamp(receipt["scheduled_at"], ZoneInfo("America/New_York")).isoformat()
            for receipt in restarted.receipts(job["id"])
        ]
        assert len(set(times)) == 3
        assert sum(value.startswith("2026-11-01T01:30") for value in times) == 1
        changed = restarted.update_job(job["id"], config={"timezone": "Europe/Berlin", "cron": "0 9 * * *"})
        assert changed["next_fire"] == _next_cron("0 9 * * *", "Europe/Berlin", now[0])
        await restarted.tick()
        assert len(calls) == 3
    finally:
        store.close()
