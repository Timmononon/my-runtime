"""Official native search and catalogue paths, using temporary state and mock HTTP."""
import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest
import yaml

from aster.api import create_app
from aster.customization.credentials import CredentialStore
from aster.customization.management import ManagementService
from aster.domain import ModelTurn, ToolCall, Usage
from aster.engine import FakeModel
from aster.engine.telemetry import performance
from aster.providers.deepseek import ProviderError
from aster.service import RunCoordinator
from aster.state import ReconciliationRequired

SECRET = "offline-deepseek-search-private-key"
TOKEN = "deepseek-search-offline-api-token-thirty-two-characters"


class Vault:
    def __init__(self):
        self.values = {}

    def get_password(self, service, name):
        return self.values.get((service, name))

    def set_password(self, service, name, value):
        self.values[service, name] = value

    def delete_password(self, service, name):
        self.values.pop((service, name), None)


@asynccontextmanager
async def runtime(tmp_path, monkeypatch, *, model=None, config=None):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    if config:
        (tmp_path / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    coordinator = RunCoordinator(tmp_path, credentials=CredentialStore(backend=Vault()), browser_enabled=False,
                                 model_factory=lambda _: model or FakeModel([]))
    coordinator.providers.update("deepseek", expected_revision=1, api_key=SECRET)
    manager = ManagementService(coordinator)
    manager.update(manager.revision(), {"web_search": {"backend": "deepseek"}})
    app = create_app(coordinator, token=TOKEN)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app),
            base_url="http://localhost:8765", headers={"Authorization": "Bearer " + TOKEN}, trust_env=False) as client:
        try:
            yield coordinator, manager, client
        finally:
            await coordinator.close()


def payload(*, content=None, usage=None):
    return {"content": content if content is not None else [
        {"type": "web_search_tool_result", "content": [
            {"type": "web_search_result", "url": "https://example.org/first", "title": "First", "page_age": "2026-10-11"},
            {"type": "web_search_result", "url": "https://example.org/first", "title": "Duplicate"},
            {"type": "web_search_result", "url": "https://example.org/second", "title": "Second"}]},
        {"type": "text", "text": "Unstructured https://not-a-result.example must not become a source",
         "citations": [{"url": "https://example.org/first", "cited_text": "Observed cited excerpt"}]}],
        "usage": usage if usage is not None else {"input_tokens": 12, "output_tokens": 5,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
            "server_tool_use": {"web_search_requests": 1}}}


def inject(coordinator, handler, *, redirects=False):
    tool = next(item for item in coordinator.base_tools if item.spec.name == "web_search")
    tool.client_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                                   follow_redirects=redirects, trust_env=False)
    return tool


async def test_native_search_actual_wire_sources_usage_budget_and_cost(tmp_path, monkeypatch):
    calls = []
    async with runtime(tmp_path, monkeypatch) as (coordinator, _, _):
        (tmp_path / "prices.yaml").write_text(yaml.safe_dump({"version": "offline-rates", "currency": "USD",
            "models": {"deepseek-flash": {"per_million": {"input_uncached": 1, "input_cached": 1, "output": 2}}}}))
        def handler(request):
            calls.append(request)
            assert str(request.url) == "https://api.deepseek.com/anthropic/v1/messages"
            assert request.method == "POST" and request.headers["x-api-key"] == SECRET
            assert request.headers["Authorization"] == "Bearer " + SECRET
            assert request.headers["anthropic-version"] == "2023-06-01"
            body = json.loads(request.content)
            assert body["model"] == "deepseek-flash" and body["max_tokens"] == 4096
            assert body["tools"] == [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}]
            assert "offline query" in body["messages"][0]["content"][0]["text"]
            return httpx.Response(200, json=payload())
        inject(coordinator, handler)
        sid = coordinator.create_session(tmp_path)["id"]
        result = await coordinator.execute_session_tool(sid, "web_search", {"query": "offline query", "limit": 1}, approved=True)
        assert not result.is_error and len(calls) == 1
        assert result.data["retrieval"] == "native_web_search" and result.data["truncated"]
        assert result.data["results"] == [{"url": "https://example.org/first", "title": "First",
            "snippet": "Observed cited excerpt", "source": "deepseek", "published_at": "2026-10-11"}]
        assert "not-a-result" not in result.content
        run = coordinator.store.list_runs(sid)[0]
        usage = coordinator.store.usage(run["id"])
        assert len(usage) == 1 and usage[0]["raw"]["purpose"] == "web_search"
        assert usage[0]["raw"]["provider_id"] == "deepseek" and usage[0]["raw"]["model"] == "deepseek-flash"
        assert usage[0]["input_tokens"] == 12 and usage[0]["output_tokens"] == 5
        assert usage[0]["raw"]["native_search_requests"] == 1
        assert coordinator.store.run_budget(run["id"])["model_requests"] == 1
        assert coordinator.store.tool_executions(run["id"])[0]["replay_safe"] == 0
        assert result.data["cost_estimate"]["amount"] == "0.000022"
        assert performance(coordinator.store, run["id"], data_dir=tmp_path)["cost_estimate"]["amount"] == "0.000022"
        assert SECRET not in json.dumps(coordinator.store.events(run_id=run["id"]))
        assert SECRET not in json.dumps(coordinator.store.usage(run["id"])) and SECRET not in result.content


@pytest.mark.parametrize("content", [
    [{"type": "text", "text": "Plain answer https://ordinary.example " + SECRET}],
    [{"type": "web_search_tool_result", "content": {"type": "web_search_tool_result_error", "error_code": SECRET}}],
    [{"type": "web_search_tool_result", "content": [{"type": "unrelated", "text": SECRET}]}],
])
async def test_native_structured_failure_keeps_usage_and_never_scrapes_prose(tmp_path, monkeypatch, content):
    calls = []
    async with runtime(tmp_path, monkeypatch) as (coordinator, _, _):
        inject(coordinator, lambda request: calls.append(request) or httpx.Response(200, json=payload(content=content)))
        sid = coordinator.create_session(tmp_path)["id"]
        result = await coordinator.execute_session_tool(sid, "web_search", {"query": "fixture"}, approved=True)
        assert result.is_error and result.outcome == "known" and len(calls) == 1
        assert SECRET not in result.content and "ordinary.example" not in result.content
        run = coordinator.store.list_runs(sid)[0]
        assert coordinator.store.usage(run["id"])[0]["input_tokens"] == 12
        assert result.data["cost_estimate"]["status"] == "unknown"


async def test_native_empty_structured_results_are_real_no_data_and_missing_usage_stays_unknown(tmp_path, monkeypatch):
    async with runtime(tmp_path, monkeypatch) as (coordinator, _, _):
        inject(coordinator, lambda request: httpx.Response(200, json=payload(
            content=[{"type": "web_search_tool_result", "content": []}], usage={})))
        sid = coordinator.create_session(tmp_path)["id"]
        result = await coordinator.execute_session_tool(sid, "web_search", {"query": "fixture"}, approved=True)
        assert not result.is_error and result.data["no_data"] and result.data["results"] == []
        assert result.data["usage"]["input_tokens"] is None and result.data["usage"]["cache_hit_tokens"] is None
        assert result.data["cost_estimate"]["status"] == "unknown" and result.data["cost_estimate"]["amount"] is None


async def test_native_redaction_drops_literal_and_encoded_key_urls_and_citations(tmp_path, monkeypatch):
    encoded = "".join(f"%{ord(char):02X}" for char in SECRET)
    content = [{"type": "web_search_tool_result", "content": [
        {"type": "web_search_result", "url": "https://example.org/" + encoded, "title": "leak"},
        {"type": "web_search_result", "url": "https://example.org/?ordinary=" + SECRET, "title": "leak"},
        {"type": "web_search_result", "url": "https://example.org/clean", "title": SECRET + encoded}]},
        {"type": "text", "text": SECRET, "citations": [{"url": "https://example.org/clean", "cited_text": SECRET + encoded}]}]
    async with runtime(tmp_path, monkeypatch) as (coordinator, _, _):
        inject(coordinator, lambda request: httpx.Response(200, json=payload(content=content)))
        sid = coordinator.create_session(tmp_path)["id"]
        result = await coordinator.execute_session_tool(sid, "web_search", {"query": "fixture"}, approved=True)
        assert not result.is_error and len(result.data["results"]) == 1
        run = coordinator.store.list_runs(sid)[0]
        for value in (result.content, json.dumps(coordinator.store.events(run_id=run["id"])),
                      json.dumps(coordinator.store.tool_executions(run["id"]))):
            assert SECRET not in value and encoded not in value


@pytest.mark.parametrize("change", ["key", "config"])
async def test_native_await_rechecks_credentials_and_global_settings_before_releasing_results(tmp_path, monkeypatch, change):
    calls = []
    async with runtime(tmp_path, monkeypatch) as (coordinator, manager, _):
        async def handler(request):
            calls.append(request)
            await asyncio.sleep(0)
            if change == "key":
                coordinator.providers.update("deepseek", expected_revision=2, api_key="new-offline-key")
            else:
                manager.update(manager.revision(), {"web_search": {}})
            return httpx.Response(200, json=payload())
        inject(coordinator, handler)
        sid = coordinator.create_session(tmp_path)["id"]
        result = await coordinator.execute_session_tool(sid, "web_search", {"query": "fixture"}, approved=True)
        assert result.is_error and result.outcome == "known" and len(calls) == 1
        assert result.error_category == "configuration" and "results" not in result.data
        run = coordinator.store.list_runs(sid)[0]
        assert coordinator.store.usage(run["id"])[0]["input_tokens"] == 12
        assert SECRET not in result.content


@pytest.mark.parametrize("cancelled", [True, False])
async def test_native_partial_response_cancel_or_timeout_is_unknown_never_replayed(tmp_path, monkeypatch, cancelled):
    entered, closed = asyncio.Event(), asyncio.Event()
    calls = []
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            entered.set()
            try:
                await asyncio.Event().wait()
                yield b"never complete"
            finally:
                closed.set()
    async with runtime(tmp_path, monkeypatch) as (coordinator, manager, _):
        manager.update(manager.revision(), {"web_search": {"backend": "deepseek", "timeout_seconds": 1}})
        inject(coordinator, lambda request: calls.append(request) or httpx.Response(200, stream=Stream()))
        sid = coordinator.create_session(tmp_path)["id"]
        pending = asyncio.create_task(coordinator.execute_session_tool(sid, "web_search", {"query": "fixture"}, approved=True))
        await asyncio.wait_for(entered.wait(), 1)
        run = coordinator.store.list_runs(sid)[0]
        if cancelled:
            coordinator.cancel(run["id"])
        result = await asyncio.wait_for(pending, 3)
        assert result.is_error and result.outcome == "unknown" and closed.is_set() and len(calls) == 1
        receipt = coordinator.store.usage(run["id"])[0]
        assert receipt["input_tokens"] is None and receipt["raw"]["status"] == "outcome_unknown"
        assert coordinator.store.get_run(run["id"])["status"] == "needs_reconciliation"
        with pytest.raises(ReconciliationRequired):
            await coordinator.execute_session_tool(sid, "web_search", {"query": "fixture"}, approved=True)
        assert len(calls) == 1


@pytest.mark.parametrize("budget", ["requests", "tokens"])
async def test_native_search_includes_auxiliary_requests_and_tokens_in_run_budget(tmp_path, monkeypatch, budget):
    config = {"permissions": {"auto_approve_tools": ["web_search"]},
              "runtime": {"max_model_requests": 1 if budget == "requests" else 20,
                      "max_total_tokens": 10 if budget == "tokens" else 1000}}
    model = FakeModel([ModelTurn(tool_calls=[ToolCall("search", "web_search", {"query": "fixture"})], usage=Usage(2, 2)),
                       ModelTurn("must not send a second primary request")])
    calls = []
    async with runtime(tmp_path, monkeypatch, model=model, config=config) as (coordinator, _, _):
        inject(coordinator, lambda request: calls.append(request) or httpx.Response(200, json=payload()))
        sid = coordinator.create_session(tmp_path)["id"]
        run = await coordinator.submit(sid, "offline budget fixture")
        result = await coordinator.wait(run["id"], timeout=3)
        assert result.status == "budget_exhausted" and len(model.calls) == 1
        assert len(calls) == (0 if budget == "requests" else 1)
        assert coordinator.store.run_budget(run["id"])["model_requests"] == (1 if budget == "requests" else 2)


async def test_official_catalogue_uses_native_path_under_tun_but_compatible_still_pins_dns(tmp_path, monkeypatch):
    native_calls, public_calls = [], []
    async def tun_address(value):
        public_calls.append(value)
        raise ProviderError("Fixture TUN DNS returned non-public 198.18.1.1", category="endpoint")
    def native(timeout):
        def handler(request):
            native_calls.append(request)
            assert str(request.url) == "https://api.deepseek.com/models"
            assert request.headers["Authorization"] == "Bearer " + SECRET
            return httpx.Response(200, json={"data": [{"id": "deepseek-flash"}]})
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    monkeypatch.setattr("aster.providers.profiles.verify_public_endpoint", tun_address)
    monkeypatch.setattr("aster.providers.profiles.native_deepseek_client", native)
    async with runtime(tmp_path, monkeypatch) as (coordinator, _, client):
        response = await client.post("/providers/deepseek/models/fetch", json={"expected_revision": 2})
        assert response.status_code == 200 and len(native_calls) == 1 and public_calls == []
        assert response.json()["models"][0]["effort_levels"] == ["none", "low", "high", "max"]
        compatible = coordinator.providers.create(name="Compatible official host fixture", base_url="https://api.deepseek.com",
                                                   protocol="openai_chat", api_key=SECRET)
        original = coordinator.providers._row(compatible["id"])
        response = await client.post(f"/providers/{compatible['id']}/models/fetch", json={"expected_revision": 1})
        assert response.status_code == 502 and response.json()["category"] == "endpoint"
        assert len(native_calls) == 1 and public_calls == ["https://api.deepseek.com"]
        assert coordinator.providers._row(compatible["id"]) == original


@pytest.mark.parametrize("status", [307, 401, 404, 429, 503])
async def test_official_catalogue_and_native_search_failures_do_not_redirect_echo_or_replace_metadata(tmp_path, monkeypatch, status):
    calls = []
    async with runtime(tmp_path, monkeypatch) as (coordinator, _, client):
        def handler(request):
            calls.append(request)
            return httpx.Response(status, headers={"location": "https://attacker.example/stolen"}, json={"error": SECRET})
        coordinator.providers.client_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                                                         follow_redirects=True, trust_env=False)
        original = coordinator.providers._row("deepseek")
        response = await client.post("/providers/deepseek/models/fetch", json={"expected_revision": 2})
        assert response.status_code == 502 and response.json()["status_code"] == status
        assert SECRET not in response.text and len(calls) == 1
        assert coordinator.providers._row("deepseek") == original
        inject(coordinator, handler, redirects=True)
        sid = coordinator.create_session(tmp_path)["id"]
        result = await coordinator.execute_session_tool(sid, "web_search", {"query": "fixture"}, approved=True)
        assert result.is_error and result.outcome == "known" and SECRET not in result.content
        assert len(calls) == 2 and all(request.url.host == "api.deepseek.com" for request in calls)


@pytest.mark.parametrize("invalid", [
    {"backend": "deepseek", "base_url": "https://changed.example"},
    {"backend": "deepseek", "credential_ref": "keyring:aster/search"},
    {"backend": "deepseek", "provider_id": "custom"},
    {"backend": "deepseek", "model": "absent-model"},
    {"backend": "deepseek", "max_tokens": True},
])
async def test_native_management_rejects_address_key_or_unlisted_model_and_get_stays_offline(tmp_path, monkeypatch, invalid):
    async with runtime(tmp_path, monkeypatch) as (_, _, client):
        state = (await client.get("/management")).json()
        before = (tmp_path / "config.yaml").read_bytes()
        response = await client.patch("/management", json={"expected_revision": state["revision"], "web_search": invalid})
        assert response.status_code == 422 and (tmp_path / "config.yaml").read_bytes() == before


async def test_legacy_search_partial_timeout_keeps_dispatched_outcome_unknown(tmp_path, monkeypatch):
    calls, closed = [], asyncio.Event()
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            try:
                await asyncio.Event().wait()
                yield b"never complete"
            finally:
                closed.set()
    async with runtime(tmp_path, monkeypatch) as (coordinator, manager, _):
        manager.update(manager.revision(), {"web_search": {"backend": "searxng", "base_url": "https://search.example",
                                                           "timeout_seconds": 1}})
        inject(coordinator, lambda request: calls.append(request) or httpx.Response(200, stream=Stream()))
        sid = coordinator.create_session(tmp_path)["id"]
        result = await coordinator.execute_session_tool(sid, "web_search", {"query": "fixture"}, approved=True)
        assert result.is_error and result.error_category == "timeout" and result.outcome == "unknown"
        assert len(calls) == 1 and closed.is_set()
        run = coordinator.store.list_runs(sid)[0]
        assert coordinator.store.tool_executions(run["id"])[0]["state"] == "outcome_unknown"
        assert coordinator.store.usage(run["id"]) == []


async def test_native_response_budget_preserves_unknown_billing_and_catalogue_budget_keeps_old_metadata(tmp_path, monkeypatch):
    calls = []
    async with runtime(tmp_path, monkeypatch) as (coordinator, _, client):
        coordinator.providers.client_factory = lambda: httpx.AsyncClient(trust_env=False,
            transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1))))
        original = coordinator.providers._row("deepseek")
        response = await client.post("/providers/deepseek/models/fetch", json={"expected_revision": 2})
        assert response.status_code == 502 and response.json()["category"] == "budget"
        assert coordinator.providers._row("deepseek") == original
        inject(coordinator, lambda request: calls.append(request) or httpx.Response(200, content=b"x" * (4 * 1024 * 1024 + 1)))
        sid = coordinator.create_session(tmp_path)["id"]
        result = await coordinator.execute_session_tool(sid, "web_search", {"query": "fixture"}, approved=True)
        assert result.is_error and result.error_category == "budget" and result.outcome == "unknown"
        run = coordinator.store.list_runs(sid)[0]
        assert coordinator.store.usage(run["id"])[0]["input_tokens"] is None and len(calls) == 2


async def test_cost_uses_search_model_rate_and_keeps_primary_request_rate(tmp_path, monkeypatch):
    model = FakeModel([ModelTurn(tool_calls=[ToolCall("search", "web_search", {"query": "fixture"})], usage=Usage(2, 2)),
                       ModelTurn("complete", usage=Usage(3, 1))])
    model.model = "primary-pricing-fixture"
    async with runtime(tmp_path, monkeypatch, model=model,
                       config={"permissions": {"auto_approve_tools": ["web_search"]}}) as (coordinator, _, _):
        (tmp_path / "prices.yaml").write_text(yaml.safe_dump({"version": "two-models", "currency": "USD", "models": {
            "primary-pricing-fixture": {"per_million": {"input_uncached": 1, "input_cached": 1, "output": 1}},
            "deepseek-flash": {"per_million": {"input_uncached": 2, "input_cached": 2, "output": 4}}}}))
        inject(coordinator, lambda request: httpx.Response(200, json=payload()))
        run = await coordinator.submit(coordinator.create_session(tmp_path)["id"], "offline cost fixture")
        assert (await coordinator.wait(run["id"], timeout=3)).status == "completed"
        result = performance(coordinator.store, run["id"], data_dir=tmp_path)
        assert result["cost_estimate"]["amount"] == "0.000052" and result["cost_estimate"]["requests"] == 3
        assert sum(item["type"] == "model.request" for item in result["timeline"]) == 3
        assert any(item.get("purpose") == "web_search" for item in result["timeline"])


async def test_official_directory_keeps_native_none_switch_without_granting_it_to_compatible(tmp_path, monkeypatch):
    from aster.providers.deepseek import DeepSeekResponsesClient
    from aster.providers.profiles import models_metadata
    catalogue = {"data": [{"id": "deepseek-flash", "effort": {"supported_levels": ["low", "high", "max"]}}]}
    async with runtime(tmp_path, monkeypatch) as (coordinator, _, client):
        sid = coordinator.create_session(tmp_path)["id"]
        coordinator.select_session(sid, reasoning_effort="none")
        coordinator.providers.client_factory = lambda: httpx.AsyncClient(trust_env=False,
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=catalogue)))
        response = await client.post("/providers/deepseek/models/fetch", json={"expected_revision": 2})
        assert response.status_code == 200 and response.json()["models"][0]["effort_levels"] == ["none", "low", "high", "max"]
        assert coordinator.session_selection(sid)["reasoning_effort"] == "none"
        assert coordinator.providers.pin("deepseek", "deepseek-flash", "none")["reasoning_effort"] == "none"
        async with httpx.AsyncClient(trust_env=False, transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=catalogue))) as http:
            adapter = DeepSeekResponsesClient(SECRET, client=http, effort="none")
            capabilities = await adapter.refresh_capabilities()
            assert capabilities["deepseek-flash"].effort_levels == ("none", "low", "high", "max")
        requests = []
        def handler(request):
            requests.append(request)
            if request.method == "GET":
                return httpx.Response(200, json=catalogue)
            assert json.loads(request.content)["reasoning"] == {"effort": "none"}
            terminal = {"type": "response.completed", "response": {"status": "completed", "output": [
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "none preserved"}]}]}}
            return httpx.Response(200, headers={"content-type": "text/event-stream"},
                                  content="event: response.completed\ndata: " + json.dumps(terminal) + "\n\n")
        def adapter_factory(key, **kwargs):
            adapter = DeepSeekResponsesClient(key, client=httpx.AsyncClient(trust_env=False,
                transport=httpx.MockTransport(handler)), **kwargs)
            adapter._owns_client = True
            return adapter
        coordinator.model_factory = None
        monkeypatch.setattr("aster.service.coordinator.DeepSeekResponsesClient", adapter_factory)
        run = await coordinator.submit(sid, "native none preparation and actual protocol fixture")
        outcome = await coordinator.wait(run["id"], timeout=3)
        assert outcome.status == "completed" and outcome.text == "none preserved"
        assert [request.method for request in requests] == ["GET", "POST"]
    assert models_metadata([{"id": "compatible", "effort_levels": ["low", "high"]}],
                           protocol="openai_chat")[0]["effort_levels"] == ["low", "high"]


async def test_native_success_keeps_results_and_usage_when_optional_prices_yaml_is_invalid(tmp_path, monkeypatch):
    async with runtime(tmp_path, monkeypatch) as (coordinator, _, _):
        (tmp_path / "prices.yaml").write_text("version: [invalid optional price source\n", encoding="utf-8")
        inject(coordinator, lambda request: httpx.Response(200, json=payload()))
        sid = coordinator.create_session(tmp_path)["id"]
        result = await coordinator.execute_session_tool(sid, "web_search", {"query": "fixture"}, approved=True)
        assert not result.is_error and len(result.data["results"]) == 2
        assert result.data["cost_estimate"]["status"] == "unknown" and result.data["cost_estimate"]["amount"] is None
        run = coordinator.store.list_runs(sid)[0]
        assert coordinator.store.usage(run["id"])[0]["input_tokens"] == 12
        assert "invalid optional price source" not in result.content


@pytest.mark.parametrize("body", [{"unexpected": "unrelated fixture body"}, {"error": SECRET}])
async def test_searxng_http200_error_or_unrelated_body_is_not_reported_as_empty_success(tmp_path, monkeypatch, body):
    calls = []
    async with runtime(tmp_path, monkeypatch) as (coordinator, manager, _):
        manager.update(manager.revision(), {"web_search": {"backend": "searxng", "base_url": "https://search.example"}})
        inject(coordinator, lambda request: calls.append(request) or httpx.Response(200, json=body))
        result = await coordinator.execute_session_tool(coordinator.create_session(tmp_path)["id"], "web_search",
                                                       {"query": "fixture"}, approved=True)
        assert result.is_error and result.error_category == "protocol" and result.outcome == "known"
        assert "no_data" not in result.data and SECRET not in result.content and len(calls) == 1

