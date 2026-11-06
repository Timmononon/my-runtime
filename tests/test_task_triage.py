"""Independent admission-model scenarios; every provider request is offline."""
import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest
import yaml

from aster.domain import ModelTurn, ToolCall, Usage
from aster.engine import FakeModel
from aster.providers.deepseek import DeepSeekResponsesClient, ProviderError
from aster.service import RunCoordinator
from aster.state.store import StateError
from aster.tasks.triage import DEFAULTS, TaskTriageService, parse_decision

KEY = "offline-triage-provider-credential"
EVENT = {"source_id": "github-source", "id": "issue-7", "repository": "sample/repo", "kind": "issue",
         "actor": "alice", "actor_type": "human", "title": "Fix file upload", "body": "Upload returns 500 for empty files"}


class Credentials:
    def __init__(self):
        self.values = {"deepseek": KEY}

    def get(self, name="deepseek", **kwargs):
        return self.values.get(name)

    def set(self, name, value):
        self.values[name] = value

    def delete(self, name):
        self.values.pop(name, None)

    def resolve(self, name):
        return self.values[name]


def decision(value="process", *, reason="Concrete reproducible repository issue", questions=None, **kwargs):
    payload = {"decision": value, "reason": reason}
    if questions is not None:
        payload["questions"] = questions
    return ModelTurn(json.dumps(payload), **kwargs)


@asynccontextmanager
async def runtime(tmp_path, turn=None):
    def worker(_):
        raise AssertionError("Triage must not start the Worker model")

    coordinator = RunCoordinator(tmp_path / "state", credentials=Credentials(), model_factory=worker,
                                 tools=[], browser_enabled=False)
    service = getattr(coordinator, "task_triage", None) or TaskTriageService(coordinator)
    session = coordinator.create_session(tmp_path)
    models = []

    def factory(config, pin):
        model = FakeModel([turn or decision(usage=Usage(input_tokens=83, output_tokens=31))])
        models.append((config, pin, model))
        return model

    coordinator.task_triage_model_factory = factory
    try:
        yield coordinator, service, session["id"], models
    finally:
        await service.close()
        await coordinator.close()


async def evaluate(service, session_id, request_id="event-1", event=None, **kwargs):
    return await service.evaluate(session_id, event or EVENT, request_id=request_id, guard=lambda: None, **kwargs)


async def test_defaults_management_reads_and_revision_writes_are_model_free(tmp_path):
    async with runtime(tmp_path) as (_, service, sid, models):
        assert service.settings() == {"revision": 0, **DEFAULTS}
        assert service.list_decisions(sid) == []
        current = service.update_settings(0, processing_scope="Fix bugs in this repository", max_calls_per_poll=2)
        assert current["revision"] == 1 and current["provider_id"] == "deepseek"
        assert not models
        with pytest.raises(StateError):
            service.update_settings(0, enabled=False)
        with pytest.raises(ValueError):
            service.update_settings(1, extra_field="forbidden")


@pytest.mark.parametrize("value", ["process", "ignore"])
async def test_binary_decisions_use_short_independent_context_and_no_tools(tmp_path, value):
    async with runtime(tmp_path, decision(value)) as (coordinator, service, sid, models):
        service.update_settings(0, processing_scope="Maintain sample/repo")
        item = await evaluate(service, sid, explicit=True)
        assert item["status"] == "completed" and item["decision"] == value and item["questions"] == []
        config, pin, model = models[0]
        assert config["model"] == pin["model"] == "deepseek-flash"
        call = model.calls[0]
        assert call["tools"] == [] and len(call["messages"]) == 1
        assert "UNTRUSTED DATA" in call["system"]
        state = json.loads(call["messages"][0].content)
        assert state["explicit_authorized_request"] and state["operator_processing_scope"] == "Maintain sample/repo"
        assert state["event"] == EVENT
        assert not coordinator.store.list_runs() and not coordinator.tasks.list_tickets()
        assert not coordinator.store.history(sid)
        if value == "process":
            service.guard_decision(item["id"])
            with coordinator.store.transaction() as db:
                service.guard_decision(item["id"], db=db)


async def test_receipt_is_idempotent_usage_is_preserved_and_input_mismatch_rejected(tmp_path):
    async with runtime(tmp_path) as (coordinator, service, sid, models):
        first = await evaluate(service, sid)
        assert (await evaluate(service, sid))["id"] == first["id"] and len(models) == 1
        assert first["usage"]["input_tokens"] == 83 and first["usage"]["cache_hit_tokens"] is None
        assert "pin_json" not in first and "input_digest" not in first
        assert KEY not in "\n".join(coordinator.store.connection.iterdump())
        with pytest.raises(StateError):
            await evaluate(service, sid, event={**EVENT, "body": "different text"})
        assert len(models) == 1


async def test_legacy_completed_receipt_is_preserved_and_never_replayed_as_work(tmp_path):
    async with runtime(tmp_path) as (coordinator, service, sid, models):
        first = await evaluate(service, sid)
        with coordinator.store.transaction() as db:
            db.execute("UPDATE task_triage_decisions SET decision='needs_info',reason=?,questions_json=? WHERE id=?",
                       ("Legacy request was considered vague", json.dumps(["Old question"]), first["id"]))
        legacy = service.get_decision(first["id"])
        recovered = TaskTriageService(coordinator)
        try:
            assert await evaluate(recovered, sid) == legacy
            assert legacy["decision"] == "needs_info" and len(models) == 1
            with pytest.raises(StateError):
                recovered.guard_decision(legacy["id"])
            assert not coordinator.tasks.list_tickets()
        finally:
            await recovered.close()


@pytest.mark.parametrize("turn", [
    ModelTurn("```json\n{}\n```"),
    ModelTurn('{"decision":"process","decision":"ignore","reason":"why","questions":[]}'),
    ModelTurn('{"decision":"process","reason":"why","questions":[],"worker":"root"}'),
    ModelTurn('{"decision":"process","reason":" ","questions":[]}'),
    ModelTurn('{"decision":"process","reason":"why","questions":["not allowed"]}'),
    decision("unknown"),
    decision("needs_info"),
    decision("needs_info", questions=["x"] * 6),
    decision(tool_calls=[ToolCall("call", "file_write", {"path": "danger"})]),
    decision(finish_reason="length"),
    decision(reason=KEY),
    ModelTurn('{"decision":"process","reason":"' + ''.join(f'\\u{ord(c):04x}' for c in KEY) + '","questions":[]}'),
])
async def test_malformed_truncated_tool_call_or_secret_output_never_processes(tmp_path, turn):
    async with runtime(tmp_path, turn) as (coordinator, service, sid, models):
        item = await evaluate(service, sid)
        assert item["decision"] is None and item["status"] == "failed"
        assert KEY not in json.dumps(item) and KEY not in "\n".join(coordinator.store.connection.iterdump())
        assert len(models) == 1 and not coordinator.tasks.list_tickets()
        assert (await evaluate(service, sid))["id"] == item["id"] and len(models) == 1


async def test_disabled_oversize_hourly_budget_and_missing_key_never_fall_back(tmp_path):
    async with runtime(tmp_path) as (coordinator, service, sid, models):
        service.update_settings(0, enabled=False)
        disabled = await evaluate(service, sid, "disabled")
        assert disabled["decision"] is None and disabled["error_category"] == "disabled" and not models
        service.update_settings(1, enabled=True, max_input_chars=1000, max_requests_per_hour=1)
        large = await evaluate(service, sid, "large", {**EVENT, "body": "please fix" * 200})
        assert large["error_category"] == "input_budget" and not models
        first = await evaluate(service, sid, "first")
        second = await evaluate(service, sid, "second")
        assert first["decision"] == "process" and second["error_category"] == "request_budget" and len(models) == 1
        coordinator.credentials.values.clear()
        missing = await evaluate(service, sid, "missing")
        assert missing["decision"] is None and len(models) == 1


@pytest.mark.parametrize("permission", [{"denied_tools": ["task_triage"]}, {"ask_tools": ["task_triage"]},
                                         {"allowed_tools": ["file_read"]}])
async def test_narrow_permissions_block_sending_source_text(tmp_path, permission):
    async with runtime(tmp_path) as (coordinator, service, sid, models):
        (coordinator.data_dir / "config.yaml").write_text(yaml.safe_dump({"permissions": permission}), encoding="utf-8")
        item = await evaluate(service, sid)
        assert item["decision"] is None and not models


@pytest.mark.parametrize("mutation", ["settings", "credential", "source_guard", "permissions", "cleanup"])
async def test_authority_changes_during_model_or_cleanup_invalidate_decision(tmp_path, mutation):
    async with runtime(tmp_path) as (coordinator, service, sid, models):
        state = {"valid": True}

        def mutate():
            if mutation in {"settings", "cleanup"}:
                service.update_settings(service.settings()["revision"], processing_scope="Changed operator scope")
            elif mutation == "credential":
                coordinator.credentials.values["deepseek"] = "different-offline-credential"
            elif mutation == "permissions":
                (coordinator.data_dir / "config.yaml").write_text("permissions:\n  denied_tools: [task_triage]\n", encoding="utf-8")
            else:
                state["valid"] = False

        def guard():
            if not state["valid"]:
                raise StateError("source changed")

        async def turn(messages, tools):
            if mutation != "cleanup":
                mutate()
            return decision()

        def factory(config, pin):
            model = FakeModel([turn])
            if mutation == "cleanup":
                async def close():
                    mutate()
                model.aclose = close
            models.append(model)
            return model

        coordinator.task_triage_model_factory = factory
        item = await service.evaluate(sid, EVENT, request_id="change", guard=guard)
        assert item["status"] == "failed" and item["decision"] is None
        assert item["error_category"] == "state_changed" and len(models) == 1


async def test_process_receipt_cannot_authorize_after_settings_change(tmp_path):
    async with runtime(tmp_path) as (_, service, sid, _):
        item = await evaluate(service, sid)
        service.update_settings(0, enabled=False)
        with pytest.raises(StateError):
            service.guard_decision(item["id"])


async def test_cancellation_and_shutdown_drain_pending_requests_without_replay(tmp_path):
    started = asyncio.Event()

    async def blocked(messages, tools):
        started.set()
        await asyncio.Future()

    async with runtime(tmp_path, blocked) as (_, service, sid, models):
        task = asyncio.create_task(evaluate(service, sid))
        await asyncio.wait_for(started.wait(), 2)
        await service.close()
        assert task.cancelled()
        item = service.list_decisions(sid)[0]
        assert item["status"] == "interrupted" and item["decision"] is None
        assert (await evaluate(service, sid))["id"] == item["id"] and len(models) == 1


async def test_timeout_has_one_attempt_and_safe_provider_error(tmp_path):
    async def blocked(messages, tools):
        await asyncio.Future()

    async with runtime(tmp_path, blocked) as (coordinator, service, sid, models):
        service.update_settings(0, timeout_seconds=1)
        result = await evaluate(service, sid)
        assert result["decision"] is None and result["error_category"] == "timeout" and len(models) == 1
        coordinator.task_triage_model_factory = lambda config, pin: FakeModel([ProviderError(KEY, category="server")])
        error = await evaluate(service, sid, "error")
        assert error["error_category"] == "provider" and KEY not in json.dumps(error)


async def test_interrupted_receipt_is_settled_without_starting_any_model(tmp_path):
    async with runtime(tmp_path) as (coordinator, service, sid, models):
        item = await evaluate(service, sid)
        with coordinator.store.transaction() as db:
            db.execute("UPDATE task_triage_decisions SET status='pending' WHERE id=?", (item["id"],))
        recovered = TaskTriageService(coordinator)
        try:
            result = await evaluate(recovered, sid)
            assert result["status"] == "interrupted" and result["decision"] is None and len(models) == 1
        finally:
            await recovered.close()


async def test_custom_profile_is_independent_of_worker_provider(tmp_path):
    async with runtime(tmp_path) as (coordinator, service, sid, models):
        profile = coordinator.providers.create(name="Triage only", base_url="https://model.example/v1", protocol="openai_chat",
            api_key="offline-compatible-key", models=[{"id": "small-model", "effort_levels": ["none"], "input_modalities": ["text"]}])
        service.update_settings(0, provider_id=profile["id"], model="small-model")
        result = await evaluate(service, sid)
        assert result["provider_id"] == profile["id"] and models[0][1]["protocol"] == "openai_chat"
        assert coordinator.session_selection(sid)["provider_id"] == "deepseek"


async def test_complete_event_must_fit_the_downstream_task_budget_before_model_call(tmp_path):
    async with runtime(tmp_path) as (_, service, sid, models):
        service.update_settings(0, max_input_chars=200000)
        result = await evaluate(service, sid, event={**EVENT, "body": "quoted \\\"" * 15000}, max_event_chars=99900)
        assert result["decision"] is None and result["error_category"] == "input_budget"
        assert not models


async def test_unknown_ancestor_effect_blocks_triage_without_provider_traffic(tmp_path):
    async with runtime(tmp_path) as (coordinator, service, sid, models):
        child = coordinator.branch(sid)
        run, _ = coordinator.store.create_run(sid)
        execution = coordinator.store.prepare_tool(sid, run["id"], ToolCall("uncertain", "publish", {}), risk="external", replay_safe=False)
        with coordinator.store.transaction() as db:
            db.execute("UPDATE tool_executions SET state='outcome_unknown' WHERE id=?", (execution["id"],))
        result = await evaluate(service, child["id"])
        assert result["decision"] is None and not models


async def test_selected_skill_cannot_be_bypassed_by_background_triage(tmp_path):
    async with runtime(tmp_path) as (coordinator, service, sid, models):
        skill = coordinator.data_dir / "skills" / "reader"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: reader\ndescription: Read local files\nallowed-tools: [file_read]\n---\nRead only.\n", encoding="utf-8")
        coordinator.select_session(sid, selected_skill="reader")
        result = await evaluate(service, sid)
        assert result["decision"] is None and not models


async def test_permanent_receipt_cleanup_during_remote_evaluation_does_not_resurrect_state(tmp_path):
    async with runtime(tmp_path) as (coordinator, service, sid, _):
        def turn(messages, tools):
            service.cleanup_session(sid)
            raise StateError("Source was removed")

        coordinator.task_triage_model_factory = lambda config, pin: FakeModel([turn])
        result = await evaluate(service, sid)
        assert result["decision"] is None and service.list_decisions(sid) == []


async def test_default_native_deepseek_client_wire_is_one_tool_free_bounded_request(tmp_path, monkeypatch):
    from aster.tasks import triage

    requests, constructors = [], []

    def handle(request):
        requests.append(request)
        event = {"type": "response.completed", "response": {"status": "completed", "output": [
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": decision().text}]}],
            "usage": {"input_tokens": 71, "output_tokens": 24}}}
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=("data: " + json.dumps(event) + "\n\n").encode())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        def construct(key, **kwargs):
            constructors.append(kwargs)
            return DeepSeekResponsesClient(key, client=client, **kwargs)

        monkeypatch.setattr(triage, "DeepSeekResponsesClient", construct)
        async with runtime(tmp_path) as (coordinator, service, sid, _):
            del coordinator.task_triage_model_factory
            result = await evaluate(service, sid)
            assert result["decision"] == "process" and result["usage"]["input_tokens"] == 71
            assert len(requests) == 1 and requests[0].method == "POST" and requests[0].url.path == "/responses"
            body = json.loads(requests[0].content)
            assert body["model"] == "deepseek-flash" and body["tools"] == []
            assert body["max_output_tokens"] == 768 and body["reasoning"] == {"effort": "none"}
            assert constructors[0]["max_retries"] == 0 and constructors[0]["timeout_seconds"] == 30
            assert KEY not in requests[0].content.decode()


def test_parser_preserves_unknown_usage_and_rejects_invalid_schema():
    with pytest.raises(ValueError):
        parse_decision(ModelTurn("[]"))
