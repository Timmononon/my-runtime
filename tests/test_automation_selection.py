import json

import httpx
import pytest
import yaml

from aster.api import create_app
from aster.domain import ModelTurn
from aster.engine import FakeModel
from aster.service import RunCoordinator


def provider(coordinator):
    return coordinator.providers.create(
        name="Offline automation models", base_url="https://fixture.example/v1", protocol="openai_chat",
        models=[{"id": name, "effort_levels": ["low", "high"]} for name in ("alpha", "beta")],
        api_key="offline-automation-fixture-key",
    )["id"]


def coordinator(home, calls):
    def factory(settings):
        calls.append(settings)
        return FakeModel([ModelTurn("offline automation complete")])
    return RunCoordinator(home, tools=[], model_factory=factory)


def choice(pid, model="alpha", skill=None):
    return {"provider_id": pid, "model": model, "reasoning_effort": "low", "selected_skill": skill}


async def deliver(runtime):
    await runtime.automation._deliver(1000)
    receipts = runtime.automation.receipts()
    for receipt in receipts:
        if receipt["run_id"]:
            assert (await runtime.wait(receipt["run_id"], timeout=3)).status == "completed"
    runtime.automation._results(1000)
    return runtime.automation.receipts()


@pytest.mark.parametrize("mode", ["fresh", "sticky", "source"])
async def test_fixed_job_source_edits_and_restart_do_not_change_trigger_choices(tmp_path, mode):
    home, cwd, calls = tmp_path / "home", tmp_path / "project", []
    cwd.mkdir()
    first = coordinator(home, calls)
    pid = provider(first)
    sid = first.create_session(cwd)["id"]
    first.select_session(sid, **choice(pid))
    job = first.automation.create_job("schedule", sid, "use fixed model", {"cron": "* * * * *", "session_mode": mode})
    assert job["config"]["selection"] == choice(pid)
    first.select_session(sid, **choice(pid, "beta"))
    first.automation._claim(job, "one", 1000, job["prompt"], 1000)
    receipt = first.automation.receipts()[0]
    assert receipt["selection"] == choice(pid) and receipt["selection_captured"]
    # Saving a new job choice affects the next trigger, never an existing claim.
    first.automation.update_job(job["id"], config={"selection": choice(pid, "beta")})
    await first.close()
    second = coordinator(home, calls)
    try:
        assert second.automation.receipts()[0]["selection"] == choice(pid)
        first_receipts = await deliver(second)
        first_run = second.store.get_run(first_receipts[0]["run_id"])
        assert second.store.run_request(first_run["id"])["selection"] == choice(pid)
        current = second.automation.get_job(job["id"])
        second.automation._claim(current, "two", 1100, current["prompt"], 1100)
        receipts = await deliver(second)
        last_run = second.store.get_run(receipts[-1]["run_id"])
        assert second.store.run_request(last_run["id"])["selection"] == choice(pid, "beta")
        assert (first_run["session_id"] == last_run["session_id"]) == (mode != "fresh")
        assert second.session_selection(last_run["session_id"]) == choice(pid, "beta")
        assert [item["model"] for item in calls] == ["alpha", "beta"]
        assert "offline-automation-fixture-key" not in json.dumps(second.automation.receipts())
    finally:
        await second.close()


async def test_agent_default_ignores_source_ui_and_captures_defaults_at_each_claim(tmp_path):
    calls = []
    runtime = coordinator(tmp_path / "home", calls)
    pid = provider(runtime)
    config = runtime.data_dir / "agents" / "research" / "config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(yaml.safe_dump(choice(pid, "beta")), encoding="utf-8")
    sid = runtime.create_session(tmp_path, agent_id="research")["id"]
    runtime.select_session(sid, **choice(pid))
    try:
        job = runtime.automation.create_job("schedule", sid, "use Agent defaults", {
            "cron": "* * * * *", "selection_mode": "agent_default"})
        assert "selection" not in job["config"]
        runtime.automation._claim(job, "one", 1000, job["prompt"], 1000)
        config.write_text(yaml.safe_dump(choice(pid)), encoding="utf-8")
        await deliver(runtime)
        runtime.automation._claim(job, "two", 1100, job["prompt"], 1100)
        await deliver(runtime)
        assert [item["model"] for item in calls] == ["beta", "alpha"]
        assert [item["selection_mode"] for item in runtime.automation.receipts()] == ["agent_default"] * 2
        saved = runtime.automation.update_job(job["id"], config={"selection_mode": "fixed"})
        assert saved["config"]["selection"] == runtime.session_selection(sid)
    finally:
        await runtime.close()


async def test_claim_keeps_skill_source_project_instructions_and_permissions_until_restart(tmp_path):
    home, cwd, calls = tmp_path / "home", tmp_path / "project", []
    cwd.mkdir()
    rules = cwd / "AGENTS.md"
    rules.write_text("Original project rule.", encoding="utf-8")
    skill = home / "skills" / "limited" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: limited\nallowed-tools: [file_read]\n---\nOriginal Skill rule.", encoding="utf-8")
    first = coordinator(home, calls)
    pid = provider(first)
    sid = first.create_session(cwd)["id"]
    first.select_session(sid, **choice(pid, skill="limited"))
    job = first.automation.create_job("schedule", sid, "use captured rules", {"cron": "* * * * *"})
    first.automation._claim(job, "one", 1000, job["prompt"], 1000)
    receipt = first.automation.receipts()[0]
    rules.write_text("Changed project rule.", encoding="utf-8")
    skill.write_text("---\nname: limited\nallowed-tools: [shell_exec]\n---\nChanged Skill rule.", encoding="utf-8")
    await first.close()
    second = coordinator(home, calls)
    try:
        await deliver(second)
        run = second.store.run_request(second.automation.receipts()[0]["run_id"])
        assert "Original project rule." in run["instructions"]
        assert "Changed project rule." not in run["instructions"]
        assert run["selected_skill_snapshot"]["content"] == "Original Skill rule."
        assert run["selected_skill_snapshot"]["allowed_tools"] == ["file_read"]
        assert run["selection"]["selected_skill"] == "limited"
        assert second.automation.receipts()[0]["id"] == receipt["id"]
        assert "system" not in second.automation.receipts()[0]
    finally:
        await second.close()


async def test_changed_key_between_claim_and_delivery_blocks_without_recapturing(tmp_path):
    calls = []
    runtime = coordinator(tmp_path / "home", calls)
    pid = provider(runtime)
    sid = runtime.create_session(tmp_path)["id"]
    runtime.select_session(sid, **choice(pid))
    try:
        job = runtime.automation.create_job("schedule", sid, "bound identity", {"cron": "* * * * *"})
        runtime.automation._claim(job, "one", 1000, job["prompt"], 1000)
        captured = runtime.store.connection.execute("SELECT execution_json FROM automation_triggers").fetchone()[0]
        runtime.providers.update(pid, expected_revision=1, api_key="new-offline-fixture-key")
        for _ in range(3):
            await runtime.automation._deliver(1000)
        receipt = runtime.automation.receipts()[0]
        assert receipt["state"] == "error" and receipt["error"] == "StateError"
        assert runtime.store.connection.execute("SELECT execution_json FROM automation_triggers").fetchone()[0] == captured
        assert calls == [] and runtime.store.list_runs() == []
        assert "new-offline-fixture-key" not in json.dumps(runtime.automation.receipts())
        # A metadata edit never makes the unavailable job impossible to disable.
        assert not runtime.automation.update_job(job["id"], enabled=False)["enabled"]
    finally:
        await runtime.close()


async def test_old_job_and_committed_receipt_upgrade_keep_the_accepted_run(tmp_path):
    calls = []
    first = coordinator(tmp_path / "home", calls)
    pid = provider(first)
    sid = first.create_session(tmp_path)["id"]
    first.select_session(sid, **choice(pid))
    job = first.automation.create_job("schedule", sid, "legacy", {"cron": "* * * * *"})
    first.automation._claim(job, "one", 1000, job["prompt"], 1000)
    receipt = first.automation.receipts()[0]
    route = first.store.automation_execution_session(sid, job["id"], receipt["id"], "fresh")
    accepted = await first.submit(route["session_id"], "already committed", request_id=receipt["id"])
    assert (await first.wait(accepted["id"], timeout=3)).status == "completed"
    with first.store.transaction() as db:
        legacy = {key: value for key, value in job["config"].items() if key not in {"selection", "selection_mode"}}
        db.execute("UPDATE automation_jobs SET config_json=?", (json.dumps(legacy),))
        db.execute("UPDATE automation_triggers SET execution_json=NULL")
        db.execute("UPDATE automation_meta SET version=2")
    first.providers.update(pid, expected_revision=1, api_key="new-key-after-accepted-run")
    await first.close()
    second = coordinator(tmp_path / "home", calls)
    try:
        migrated = second.automation.get_job(job["id"])
        assert migrated["config"]["selection_mode"] == "fixed"
        assert migrated["config"]["selection"] == choice(pid)
        receipts = await deliver(second)
        assert receipts[0]["run_id"] == accepted["id"]
        assert len(second.store.list_runs()) == 1 and len(calls) == 1
        assert not receipts[0]["selection_captured"]
    finally:
        await second.close()


async def test_old_prepared_receipt_captures_migrated_job_once_before_delivery(tmp_path):
    calls = []
    first = coordinator(tmp_path / "home", calls)
    pid = provider(first)
    sid = first.create_session(tmp_path)["id"]
    first.select_session(sid, **choice(pid))
    job = first.automation.create_job("schedule", sid, "legacy prepared", {"cron": "* * * * *"})
    first.automation._claim(job, "one", 1000, job["prompt"], 1000)
    with first.store.transaction() as db:
        legacy = {key: value for key, value in job["config"].items() if key not in {"selection", "selection_mode"}}
        db.execute("UPDATE automation_jobs SET config_json=?", (json.dumps(legacy),))
        db.execute("UPDATE automation_triggers SET execution_json=NULL")
        db.execute("UPDATE automation_meta SET version=2")
    await first.close()
    second = coordinator(tmp_path / "home", calls)
    try:
        second.select_session(sid, **choice(pid, "beta"))
        receipts = await deliver(second)
        assert receipts[0]["selection_captured"] and receipts[0]["selection"] == choice(pid)
        await second.automation._deliver(1100)
        assert len(calls) == 1 and calls[0]["model"] == "alpha"
    finally:
        await second.close()


async def test_http_job_fields_preserve_fixed_choices_and_reject_injected_execution_snapshot(tmp_path):
    runtime = coordinator(tmp_path / "home", [])
    pid = provider(runtime)
    sid = runtime.create_session(tmp_path)["id"]
    runtime.select_session(sid, **choice(pid))
    token = "offline-local-token-with-at-least-thirty-two-characters"
    app = create_app(runtime, token=token, manage_lifecycle=False)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765",
                                     headers={"Authorization": "Bearer " + token}) as client:
            response = await client.post("/automations", json={"kind": "schedule", "session_id": sid,
                "prompt": "fixed fixture", "config": {"cron": "* * * * *"}, "enabled": False})
            assert response.status_code == 201, response.text
            job = response.json()
            assert job["config"]["selection_mode"] == "fixed" and job["config"]["selection"] == choice(pid)
            runtime.select_session(sid, **choice(pid, "beta"))
            listed = (await client.get("/automations")).json()["jobs"][0]
            assert listed["config"]["selection"] == choice(pid)
            agent_default = await client.patch("/automations/" + job["id"], json={"config": {"selection_mode": "agent_default"}})
            assert agent_default.status_code == 200 and "selection" not in agent_default.json()["config"]
            fixed = await client.patch("/automations/" + job["id"], json={"config": {"selection_mode": "fixed"}})
            assert fixed.json()["config"]["selection"] == choice(pid, "beta")
            rejected = await client.post(f"/sessions/{sid}/runs", json={"text": "inject", "execution_snapshot": {"snapshot": {"permissions": {}}}})
            assert rejected.status_code == 422 and runtime.store.list_runs() == []
            assert "offline-automation-fixture-key" not in response.text + agent_default.text + fixed.text + rejected.text
    finally:
        await runtime.close()


@pytest.mark.parametrize("invalid", [{"selection_mode": "source"}, {"selection": {"model": "alpha"}},
                                     {"selection": {**choice("deepseek"), "permissions": {}}},
                                     {"selection": {**choice("deepseek"), "reasoning_effort": []}}])
async def test_job_selection_cannot_accept_policy_or_malformed_choice(tmp_path, invalid):
    runtime = coordinator(tmp_path / "home", [])
    try:
        sid = runtime.create_session(tmp_path)["id"]
        with pytest.raises(ValueError):
            runtime.automation.create_job("schedule", sid, "invalid", {"cron": "* * * * *", **invalid})
        assert runtime.automation.list_jobs() == []
    finally:
        await runtime.close()
