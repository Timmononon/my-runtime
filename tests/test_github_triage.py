"""GitHub authorization and semantic triage through real durable Runtime paths."""
import asyncio
import json

import pytest
from test_github_core import comment, issue, setup
from test_github_integration import api, source

from aster.domain import ModelTurn, Usage
from aster.engine import FakeModel
from aster.state import StateError


def triage_model(coordinator, *, decision="process", callback=None):
    calls = []

    def factory(config, pin):
        async def generate(messages, tools):
            calls.append({"config": config, "pin": pin, "messages": messages, "tools": tools})
            if callback:
                result = callback(messages, tools)
                if asyncio.iscoroutine(result):
                    result = await result
                if result is not None:
                    return result
            return ModelTurn(json.dumps({"decision": decision, "reason": "Offline triage reasoning"}),
                usage=Usage(input_tokens=47, output_tokens=13))
        return FakeModel([generate])

    coordinator.task_triage_model_factory = factory
    return calls


async def new_event(coordinator, source_row, remote, clock, **fields):
    await coordinator.github.poll_source(source_row["id"])
    clock.now += 10
    remote.issues = [issue(clock, **fields)]
    return await coordinator.github.poll_source(source_row["id"])


@pytest.mark.parametrize("decision,disposition,tickets", [
    ("process", "accepted", 1), ("ignore", "triage_ignored", 0)])
async def test_explicit_issue_is_semantically_judged_once_before_any_worker(tmp_path, decision, disposition, tickets):
    coordinator, item, remote, clock = setup(tmp_path, fields={"enabled": True, "mode": "tasks", "allowed_actors": ["alice"]})
    calls = triage_model(coordinator, decision=decision)
    try:
        result = await new_event(coordinator, item, remote, clock)
        assert result["accepted"] == tickets
        assert len(coordinator.tasks.list_tickets()) == tickets
        assert len(calls) == 1 and calls[0]["tools"] == []
        assert calls[0]["pin"]["id"] == "deepseek" and calls[0]["config"]["model"] == "deepseek-flash"
        event = coordinator.github.events(item["id"])[0]
        assert event["disposition"] == disposition
        assert event["triage"]["reason"] == "Offline triage reasoning"
        assert event["triage"]["decision"] == decision
        assert event["triage"]["usage"]["input_tokens"] == 47
        assert coordinator.store.list_runs() == []
        await coordinator.github.poll_source(item["id"])
        assert len(calls) == 1 and len(coordinator.tasks.list_tickets()) == tickets
        assert remote.posted == []
    finally:
        await coordinator.close()


@pytest.mark.parametrize("mode,body,expected_calls", [("off", "inspect implementation", 0), ("observe", "inspect implementation", 0),
    ("auto", "unrelated note", 0), ("auto", "inspect the implementation", 1)])
async def test_proactive_rules_remain_the_model_free_first_gate(tmp_path, mode, body, expected_calls):
    coordinator, item, remote, clock = setup(tmp_path, fields={"enabled": True, "mode": "tasks",
        "allowed_actors": ["alice"], "observe_issues": True})
    policy = coordinator.tasks.settings()
    coordinator.tasks.update_settings(policy["revision"], mode, [{"id": "inspect", "enabled": True, "contains": ["implementation"]}])
    calls = triage_model(coordinator)
    try:
        await new_event(coordinator, item, remote, clock, body=body)
        assert len(calls) == expected_calls
        assert len(coordinator.tasks.list_tickets()) == expected_calls
    finally:
        await coordinator.close()


async def test_broad_work_request_creates_ticket_after_process_without_question_gate(tmp_path):
    coordinator, item, remote, clock = setup(tmp_path, fields={"enabled": True, "mode": "tasks",
        "allowed_actors": ["alice"], "bot_login": "aster-bot"})
    calls = triage_model(coordinator)
    try:
        result = await new_event(coordinator, item, remote, clock, body="@aster-bot check whether there are any bugs")
        assert result["accepted"] == 1 and len(calls) == 1
        ticket = coordinator.tasks.list_tickets()[0]
        assert "check whether there are any bugs" in ticket["goal"]
        receipt = coordinator.github.events(item["id"])[0]["triage"]
        assert receipt["decision"] == "process" and receipt["questions"] == []
    finally:
        await coordinator.close()


@pytest.mark.parametrize("case", ["read_only", "disabled", "override", "unlisted", "bot", "baseline"])
async def test_readonly_untrusted_and_historical_events_never_call_triage(tmp_path, case):
    coordinator, item, remote, clock = setup(tmp_path, fields={"enabled": case != "disabled",
        "mode": "read_only" if case == "read_only" else "tasks", "allowed_actors": ["alice"], "observe_issues": True})
    calls = triage_model(coordinator)
    try:
        if case == "baseline":
            remote.issues = [issue(clock, delta=-10)]
        await coordinator.github.poll_source(item["id"], read_only=case == "override")
        clock.now += 10
        if case != "baseline":
            remote.issues = [issue(clock, actor="mallory" if case == "unlisted" else "alice",
                                  actor_type="Bot" if case == "bot" else "User")]
        await coordinator.github.poll_source(item["id"], read_only=case == "override")
        assert not calls and not coordinator.tasks.list_tickets()
        assert coordinator.task_triage.list_decisions() == []
    finally:
        await coordinator.close()


@pytest.mark.parametrize("changed", ["source", "policy", "triage", "credential"])
async def test_awaited_triage_cannot_create_work_under_changed_authority(tmp_path, changed):
    coordinator, item, remote, clock = setup(tmp_path, fields={"enabled": True, "mode": "tasks", "allowed_actors": ["alice"]})

    async def mutate(messages, tools):
        await asyncio.sleep(0)
        if changed == "source":
            current = coordinator.github.get_source(item["id"])
            coordinator.github.update_source(item["id"], current["revision"], allowed_actors=["bob"])
        elif changed == "policy":
            current = coordinator.tasks.settings()
            coordinator.tasks.update_settings(current["revision"], "off", current["rules"])
        elif changed == "triage":
            current = coordinator.task_triage.settings()
            coordinator.task_triage.update_settings(current["revision"], processing_scope="Different repository scope")
        else:
            profile = coordinator.providers.get("deepseek")
            coordinator.providers.update("deepseek", expected_revision=profile["revision"], api_key="rotated-offline-triage-key")

    calls = triage_model(coordinator, callback=mutate)
    try:
        try:
            await new_event(coordinator, item, remote, clock)
        except StateError:
            pass
        assert len(calls) == 1
        assert not coordinator.tasks.list_tickets() and coordinator.store.list_runs() == []
        decisions = coordinator.task_triage.list_decisions()
        assert len(decisions) == 1 and decisions[0]["decision"] != "process"
    finally:
        await coordinator.close()


async def test_provider_failure_is_visible_and_never_falls_back_to_explicit_admission(tmp_path):
    coordinator, item, remote, clock = setup(tmp_path, fields={"enabled": True, "mode": "tasks", "allowed_actors": ["alice"]})

    def failure(*args):
        raise RuntimeError("upstream diagnostic with offline-triage-only-key")

    calls = triage_model(coordinator, callback=failure)
    try:
        await new_event(coordinator, item, remote, clock)
        await coordinator.github.poll_source(item["id"])
        event = coordinator.github.events(item["id"])[0]
        assert event["disposition"] == "triage_failed" and event["triage"]["decision"] is None
        assert event["triage"]["status"] == "failed"
        assert "offline-triage-only-key" not in json.dumps(event)
        assert len(calls) == 1 and not coordinator.tasks.list_tickets()
    finally:
        await coordinator.close()


async def test_pending_new_events_are_bounded_per_poll_but_stop_bypasses_triage(tmp_path):
    coordinator, item, remote, clock = setup(tmp_path, fields={"enabled": True, "mode": "tasks", "allowed_actors": ["alice"]})
    current = coordinator.task_triage.settings()
    coordinator.task_triage.update_settings(current["revision"], max_calls_per_poll=1)
    calls = triage_model(coordinator)
    try:
        await new_event(coordinator, item, remote, clock)
        ticket = coordinator.tasks.list_tickets()[0]
        clock.now += 10
        remote.issues.extend([issue(clock, identity=8), issue(clock, identity=9)])
        remote.comments = [comment(clock, body="/aster stop")]
        await coordinator.github.poll_source(item["id"])
        assert len(calls) == 2
        assert coordinator.tasks.get_ticket(ticket["id"])["cancel_requested"]
        assert any(event["disposition"] == "pending" for event in coordinator.github.events(item["id"]))
        assert any(event["disposition"] == "stopped" for event in coordinator.github.events(item["id"]))
        await coordinator.github.poll_source(item["id"])
        assert len(calls) == 3
        assert len(coordinator.tasks.list_tickets()) == 3
    finally:
        await coordinator.close()


@pytest.mark.parametrize("after_insert", [False, True])
async def test_crash_around_task_commit_reuses_receipt_without_replaying_or_extra_followup(tmp_path, after_insert):
    coordinator, item, remote, clock = setup(tmp_path, fields={"enabled": True, "mode": "tasks", "allowed_actors": ["alice"]})
    calls = triage_model(coordinator)
    original = coordinator.tasks.create_ticket

    def crash(*args, **kwargs):
        if after_insert:
            original(*args, **kwargs)
        raise RuntimeError("simulated process crash")

    try:
        await coordinator.github.poll_source(item["id"])
        clock.now += 10
        remote.issues = [issue(clock)]
        coordinator.tasks.create_ticket = crash
        with pytest.raises(RuntimeError, match="simulated process crash"):
            await coordinator.github.poll_source(item["id"])
        coordinator.tasks.create_ticket = original
        await coordinator.github.poll_source(item["id"])
        assert len(calls) == 1 and len(coordinator.tasks.list_tickets()) == 1
        assert coordinator.store.connection.execute("SELECT COUNT(*) FROM local_task_inputs").fetchone()[0] == 0
        event = coordinator.github.events(item["id"])[0]
        assert event["disposition"] == "accepted" and event["ticket_id"]
    finally:
        await coordinator.close()


async def test_triage_management_is_offline_revision_checked_and_independent_of_worker_selection(tmp_path):
    async with api(tmp_path) as (client, coordinator, remote, models):
        session, _ = await source(client, coordinator, tmp_path)
        calls = triage_model(coordinator)
        current = (await client.get("/tasks/triage/settings")).json()
        assert current["provider_id"] == "deepseek" and current["model"] == "deepseek-flash"
        before = coordinator.session_selection(session["id"])
        updated = await client.patch("/tasks/triage/settings", json={"expected_revision": current["revision"],
            "processing_scope": "Only actionable changes to this runtime", "max_calls_per_poll": 2})
        assert updated.status_code == 200, updated.text
        assert updated.json()["revision"] == current["revision"] + 1
        stale = await client.patch("/tasks/triage/settings", json={"expected_revision": current["revision"], "enabled": False})
        assert stale.status_code == 409
        assert (await client.get("/tasks/triage/decisions")).json() == {"decisions": []}
        assert (await client.patch("/tasks/triage/settings", json={"expected_revision": 1, "api_key": "not-a-config-field"})).status_code == 422
        assert (await client.get("/tasks/triage/settings", headers={"Authorization": ""})).status_code == 401
        assert coordinator.session_selection(session["id"]) == before
        assert calls == [] and remote.requests == [] and models == []


async def test_deleting_source_conversation_removes_no_task_triage_receipts(tmp_path):
    coordinator, item, remote, clock = setup(tmp_path, fields={"enabled": True, "mode": "tasks", "allowed_actors": ["alice"]})
    triage_model(coordinator, decision="ignore")
    try:
        await new_event(coordinator, item, remote, clock)
        assert len(coordinator.task_triage.list_decisions()) == 1
        current = coordinator.github.get_source(item["id"])
        coordinator.github.update_source(item["id"], current["revision"], enabled=False)
        coordinator.store.delete_session(item["source_session_id"])
        assert coordinator.task_triage.list_decisions() == []
        assert coordinator.github.list_sources() == []
    finally:
        await coordinator.close()


async def test_rule_change_during_record_only_evaluation_cannot_skip_semantic_triage(tmp_path):
    coordinator, item, remote, clock = setup(tmp_path, fields={"enabled": True, "mode": "tasks",
        "allowed_actors": ["alice"], "observe_issues": True})
    current = coordinator.tasks.settings()
    rules = [{"id": "change", "enabled": True, "contains": ["implementation"]}]
    coordinator.tasks.update_settings(current["revision"], "observe", rules)
    calls = triage_model(coordinator)
    original = coordinator.tasks.evaluate_candidate

    def race(*args, **kwargs):
        current = coordinator.tasks.settings()
        coordinator.tasks.update_settings(current["revision"], "auto", rules)
        coordinator.tasks.evaluate_candidate = original
        return original(*args, **kwargs)

    try:
        coordinator.tasks.evaluate_candidate = race
        with pytest.raises(StateError, match="before semantic triage"):
            await new_event(coordinator, item, remote, clock, body="inspect implementation")
        assert not calls and coordinator.tasks.list_tickets() == []
        assert coordinator.tasks.list_candidates() == []
        await coordinator.github.poll_source(item["id"])
        assert len(calls) == 1 and len(coordinator.tasks.list_tickets()) == 1
    finally:
        await coordinator.close()


async def test_source_deleted_while_triage_waits_cannot_leave_receipt_or_start_worker(tmp_path):
    coordinator, item, remote, clock = setup(tmp_path, fields={"enabled": True, "mode": "tasks", "allowed_actors": ["alice"]})

    async def remove(messages, tools):
        await asyncio.sleep(0)
        current = coordinator.github.get_source(item["id"])
        coordinator.github.update_source(item["id"], current["revision"], enabled=False)
        coordinator.store.delete_session(item["source_session_id"])

    calls = triage_model(coordinator, callback=remove)
    try:
        with pytest.raises((StateError, KeyError)):
            await new_event(coordinator, item, remote, clock)
        assert len(calls) == 1
        assert coordinator.tasks.list_tickets() == [] and coordinator.task_triage.list_decisions() == []
        assert coordinator.github.list_sources() == []
    finally:
        await coordinator.close()


async def test_lifted_triage_budget_preserves_oversized_event_without_uncreatable_task_or_repeated_call(tmp_path):
    coordinator, item, remote, clock = setup(tmp_path, fields={"enabled": True, "mode": "tasks", "allowed_actors": ["alice"]})
    current = coordinator.task_triage.settings()
    coordinator.task_triage.update_settings(current["revision"], max_input_chars=200000)
    calls = triage_model(coordinator)
    body = "/aster inspect " + "\\" * 60000
    try:
        await new_event(coordinator, item, remote, clock, body=body)
        event = coordinator.github.events(item["id"])[0]
        assert event["body"] == body
        assert event["disposition"] == "triage_failed" and event["triage"]["error_category"] == "input_budget"
        await coordinator.github.poll_source(item["id"])
        assert not calls and not coordinator.tasks.list_tickets()
        assert len(coordinator.task_triage.list_decisions()) == 1
    finally:
        await coordinator.close()
