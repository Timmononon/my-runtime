import json
from contextlib import asynccontextmanager

import httpx
import pytest
import yaml

from aster.api import create_app
from aster.customization.credentials import CredentialStore
from aster.domain import ModelTurn
from aster.engine import FakeModel
from aster.providers.profiles import ProviderProfiles, PublicProviderTransport, endpoint_url
from aster.service import RunCoordinator
from aster.state import StateError, Store

TOKEN = "test-local-token-that-is-at-least-thirty-two-characters"
SECRET = "fixture-provider-secret-unique-no-real-credentials"


class Vault:
    def __init__(self):
        self.values = {}

    def get_password(self, service, name):
        return self.values.get((service, name))

    def set_password(self, service, name, value):
        self.values[service, name] = value

    def delete_password(self, service, name):
        del self.values[service, name]


def profile_values(**changes):
    return {"name": "Local protocol fixture", "base_url": "https://provider.example/v1", "protocol": "openai_chat",
            "models": [{"id": "test-model", "name": "Test Model", "context_window": 40000, "max_output_tokens": 4000,
                        "input_modalities": ["text", "image"], "effort_levels": ["low", "high"]}], **changes}


@asynccontextmanager
async def service(tmp_path, *, fake=True):
    vault = Vault()
    coordinator = RunCoordinator(tmp_path, tools=[], credentials=CredentialStore(backend=vault),
                                 model_factory=(lambda _: FakeModel([ModelTurn("fixture answer")])) if fake else None)
    app = create_app(coordinator, token=TOKEN)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
        base_url="http://localhost:8765", headers={"Authorization": "Bearer " + TOKEN}) as client:
        yield coordinator, client, vault


async def test_profile_api_write_only_key_validation_and_revision(tmp_path):
    async with service(tmp_path) as (coordinator, client, vault):
        created = await client.post("/providers", json={**profile_values(), "api_key": SECRET})
        assert created.status_code == 201, created.text
        item = created.json()
        assert item["configured"] and item["credential_source"] == "vault"
        assert item["models"][0]["name"] == "Test Model"
        assert SECRET not in created.text and "api_key" not in created.text
        assert SECRET in vault.values.values()
        assert SECRET not in coordinator.store.path.read_bytes().decode("utf-8", errors="ignore")
        for body in ({**profile_values(), "api_key": {"wrong": SECRET}}, {**profile_values(), SECRET: SECRET},
                     {**profile_values(), "api_key": SECRET, "models": [{"id": SECRET, "unknown": SECRET}]}):
            rejected = await client.post("/providers", json=body)
            assert rejected.status_code == 422 and SECRET not in rejected.text
        rejected = await client.patch(f"/providers/{item['id']}", json={"expected_revision": 1, "api_key": SECRET + "\n"})
        assert rejected.status_code == 422 and SECRET not in rejected.text
        edited = (await client.patch(f"/providers/{item['id']}", json={"expected_revision": 1, "name": "Renamed"})).json()
        assert edited["revision"] == 2 and edited["identity_revision"] == 1
        conflict = await client.patch(f"/providers/{item['id']}", json={"expected_revision": 1, "name": "Stale"})
        assert conflict.status_code == 409
        listed = await client.get("/providers")
        assert SECRET not in listed.text and "credential_ref" not in listed.text
        pasted = await client.patch(f"/providers/{item['id']}", json={"expected_revision": 2, "name": SECRET})
        assert pasted.status_code == 422 and SECRET not in pasted.text
        assert coordinator.providers.get(item["id"])["name"] == "Renamed"
        disconnected = await client.request("DELETE", f"/providers/{item['id']}/key", json={"expected_revision": 2})
        assert not disconnected.json()["configured"] and disconnected.json()["identity_revision"] == 2
        assert SECRET not in vault.values.values()
        deleted = await client.delete(f"/providers/{item['id']}?expected_revision=3")
        assert deleted.json()["deleted"]


def test_file_key_edit_invalidates_new_pin_but_metadata_and_same_key_reordering_do_not(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    store = Store(tmp_path / "runtime.db")
    profiles = ProviderProfiles(store, CredentialStore(data_dir=tmp_path))
    try:
        profiles.update("deepseek", expected_revision=1, api_key=SECRET)
        pin = profiles.pin("deepseek", "deepseek-flash", "high")
        assert "credential_digest" in pin and SECRET not in json.dumps(pin)
        assert "credential_digest" not in profiles.get("deepseek")
        profiles.update("deepseek", expected_revision=2, name="Metadata edit")
        path = tmp_path / "credentials.yaml"
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        path.write_text("# same key, different formatting\n" + yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        assert profiles.resolve_pin(pin) == SECRET
        reference = profiles._row("deepseek")["credential_ref"]
        document["services"]["aster"][reference] = "manually-replaced-fixture-key"
        path.write_text(yaml.safe_dump(document), encoding="utf-8")
        with pytest.raises(StateError, match="credential or endpoint changed"):
            profiles.resolve_pin(pin)
        # Existing immutable snapshots without the new binding remain readable.
        legacy_pin = {name: value for name, value in pin.items() if name != "credential_digest"}
        assert profiles.resolve_pin(legacy_pin) == "manually-replaced-fixture-key"
    finally:
        store.close()


async def test_queued_run_rejects_manually_changed_file_key_before_model_call(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    model = FakeModel([ModelTurn("must not run")])
    coordinator = RunCoordinator(tmp_path, tools=[], model_factory=lambda _: model, max_parallel_runs=1)
    try:
        coordinator.providers.update("deepseek", expected_revision=1, api_key=SECRET)
        sid = coordinator.create_session(tmp_path)["id"]
        await coordinator._capacity.acquire()
        run = await coordinator.submit(sid, "queued before direct file edit", provider_id="deepseek")
        captured = coordinator.store.run_request(run["id"])
        reference = coordinator.providers._row("deepseek")["credential_ref"]
        # Editing a file does not increment the provider's SQLite revision.
        CredentialStore(data_dir=tmp_path).set(reference, "direct-file-edit-fixture-key")
        assert coordinator.providers.get("deepseek")["identity_revision"] == 2
        coordinator._capacity.release()
        result = await coordinator.wait(run["id"], timeout=3)
        assert result.status == "failed" and "credential or endpoint changed" in result.error
        assert model.calls == [] and coordinator.store.tool_executions(run["id"]) == []
        assert coordinator.store.run_request(run["id"]) == captured
        assert SECRET not in json.dumps(captured) and "direct-file-edit-fixture-key" not in result.error
    finally:
        if coordinator._capacity.locked():
            coordinator._capacity.release()
        await coordinator.close()


async def test_recovery_rejects_manually_changed_file_key_without_rewriting_snapshot(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    first = RunCoordinator(tmp_path, tools=[], model_factory=lambda _: FakeModel([]))
    first.providers.update("deepseek", expected_revision=1, api_key=SECRET)
    sid = first.create_session(tmp_path)["id"]
    run = await first.submit(sid, "captured before restart", provider_id="deepseek")
    first.store.set_run_status(run["id"], "running")
    captured = first.store.run_request(run["id"])
    reference = first.providers._row("deepseek")["credential_ref"]
    await first.close()
    CredentialStore(data_dir=tmp_path).set(reference, "changed-after-restart-fixture-key")
    model = FakeModel([ModelTurn("must not recover")])
    second = RunCoordinator(tmp_path, tools=[], model_factory=lambda _: model)
    try:
        with pytest.raises(StateError, match="credential or endpoint changed"):
            await second.recover(run["id"])
        assert model.calls == [] and second.store.tool_executions(run["id"]) == []
        assert second.store.run_request(run["id"]) == captured
        assert SECRET not in json.dumps(captured)
    finally:
        await second.close()


def test_legacy_env_adoption_replace_and_detach_do_not_mutate_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "old-environment-fixture")
    vault, store = Vault(), Store(tmp_path / "runtime.db")
    try:
        profiles = ProviderProfiles(store, CredentialStore(backend=vault))
        assert profiles.get("deepseek")["credential_source"] == "environment"
        pin = profiles.pin("deepseek", "deepseek-flash", "high")
        assert profiles.resolve_pin(pin) == "old-environment-fixture"
        newer = profiles.update("deepseek", expected_revision=1, api_key=SECRET)
        assert newer["credential_source"] == "vault"
        with pytest.raises(StateError):
            profiles.resolve_pin(pin)
        assert profiles.resolve_pin(profiles.pin("deepseek", "deepseek-flash", "high")) == SECRET
        assert not profiles.delete_key("deepseek", 2)["configured"]
        import os
        assert os.environ["DEEPSEEK_API_KEY"] == "old-environment-fixture"
    finally:
        store.close()


async def test_endpoint_change_detaches_key_and_fetch_requires_explicit_saved_revision(tmp_path):
    async with service(tmp_path) as (coordinator, client, _):
        item = (await client.post("/providers", json={**profile_values(), "api_key": SECRET})).json()
        requests = []

        def handler(request):
            requests.append(request)
            assert str(request.url) == "https://provider.example/v1/models"
            assert request.headers["Authorization"] == "Bearer " + SECRET
            return httpx.Response(200, json={"data": [{"id": "test-model"}]})

        coordinator.providers.client_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
        assert requests == []
        fetched = await client.post(f"/providers/{item['id']}/models/fetch", json={"expected_revision": 1})
        assert fetched.status_code == 200 and len(requests) == 1
        model = fetched.json()["models"][0]
        assert model["effort_levels"] == ["low", "high"] and model["input_modalities"] == ["text", "image"]
        assert model["name"] == "Test Model"
        assert fetched.json()["identity_revision"] == 1
        changed = await client.patch(f"/providers/{item['id']}", json={"expected_revision": 2, "base_url": "https://different.example/v1"})
        assert not changed.json()["configured"] and changed.json()["models"] == []
        failed = await client.post(f"/providers/{item['id']}/models/fetch", json={"expected_revision": 3})
        assert failed.status_code == 502 and len(requests) == 1


async def test_fetch_redirect_and_credential_echo_fail_closed(tmp_path):
    async with service(tmp_path) as (coordinator, client, _):
        item = coordinator.providers.create(**profile_values(), api_key=SECRET)
        seen = []

        def redirect(request):
            seen.append(str(request.url))
            return httpx.Response(307, headers={"Location": "http://127.0.0.1/stolen"})

        coordinator.providers.client_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(redirect), follow_redirects=True)
        response = await client.post(f"/providers/{item['id']}/models/fetch", json={"expected_revision": 1})
        assert response.status_code == 502 and len(seen) == 1 and SECRET not in response.text
        coordinator.providers.client_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"data": [{"id": SECRET}]})))
        response = await client.post(f"/providers/{item['id']}/models/fetch", json={"expected_revision": 1})
        assert response.status_code == 502 and SECRET not in response.text


@pytest.mark.parametrize("url", ["http://provider.example/v1", "https://localhost/v1", "https://127.0.0.1/v1",
                                "https://169.254.169.254/v1", "https://x:secret@provider.example", "https://provider.example/?key=x",
                                "https://provider.example:8443/v1", "https://provider.example/%2e%2e"])
def test_profiles_reject_unsafe_addresses(url):
    with pytest.raises(ValueError):
        endpoint_url(url, "openai_chat")


async def test_public_transport_pins_resolved_address_and_tls_hostname(monkeypatch):
    seen = []

    async def address(_, **kwargs):
        return "93.184.216.34"

    monkeypatch.setattr("aster.providers.profiles.verify_public_endpoint", address)
    transport = PublicProviderTransport("https://provider.example/v1")
    await transport.inner.aclose()
    transport.inner = httpx.MockTransport(lambda req: seen.append(req) or httpx.Response(200, text="ok"))
    async with httpx.AsyncClient(transport=transport) as client:
        await client.get("https://provider.example/v1/models")
    assert seen[0].url.host == "93.184.216.34"
    assert seen[0].headers["host"] == "provider.example" and seen[0].extensions["sni_hostname"] == "provider.example"


async def test_selection_snapshot_and_queued_identity_change_are_durable(tmp_path):
    async with service(tmp_path) as (coordinator, client, _):
        item = coordinator.providers.create(**profile_values(), api_key=SECRET)
        sid = coordinator.create_session(tmp_path)["id"]
        selection = {"provider_id": item["id"], "model": "test-model", "reasoning_effort": "high", "selected_skill": None}
        response = await client.patch(f"/sessions/{sid}/selection", json=selection)
        assert response.status_code == 200 and response.json() == selection
        await coordinator._capacity.acquire()
        await coordinator._capacity.acquire()
        await coordinator._capacity.acquire()
        await coordinator._capacity.acquire()
        run = await coordinator.submit(sid, "queued fixture", request_id="same")
        payload = coordinator.store.run_request(run["id"])
        assert payload["snapshot"]["_provider"]["base_url"] == item["base_url"]
        coordinator.select_session(sid, reasoning_effort="low")
        assert payload["selection"]["reasoning_effort"] == "high"
        coordinator.providers.update(item["id"], expected_revision=1, api_key="replacement-fixture-key")
        for _ in range(4):
            coordinator._capacity.release()
        result = await coordinator.wait(run["id"], timeout=3)
        assert result.status == "failed" and "credential or endpoint changed" in result.error
        assert coordinator.store.usage(run["id"]) == []
        assert SECRET not in json.dumps(coordinator.store.run_request(run["id"]))
        assert (await client.patch(f"/sessions/{sid}/selection", json={"reasoning_effort": "max"})).status_code == 422
        assert (await client.post(f"/sessions/{sid}/runs", json={"text": "unsafe", "permissions": {"auto_approve_tools": ["shell"]}})).status_code == 422
        assert (await client.get(f"/sessions/{sid}" )).json()["selection"]["reasoning_effort"] == "low"


async def test_real_coordinator_dispatches_selected_chat_protocol_without_model_discovery(tmp_path):
    async with service(tmp_path, fake=False) as (coordinator, client, _):
        item = coordinator.providers.create(**profile_values(), api_key=SECRET)
        requests = []

        def handler(request):
            requests.append(request)
            body = json.loads(request.content)
            assert request.url.path == "/v1/chat/completions"
            assert body["model"] == "test-model" and body["reasoning_effort"] == "low"
            chunk = {"choices": [{"index": 0, "delta": {"content": "Actual adapter reply"}, "finish_reason": None}]}
            final = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 10, "completion_tokens": 3}}
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=
                "data: " + json.dumps(chunk) + "\n\ndata: " + json.dumps(final) + "\n\ndata: [DONE]\n\n")

        coordinator.providers.client_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
        sid = coordinator.create_session(tmp_path)["id"]
        response = await client.post(f"/sessions/{sid}/runs", json={"text": "test local transport", "provider_id": item["id"],
            "model": "test-model", "reasoning_effort": "low", "request_id": "actual-protocol"})
        assert response.status_code == 202, response.text
        result = await coordinator.wait(response.json()["id"], timeout=4)
        assert result.status == "completed", result.error
        assert result.text == "Actual adapter reply" and len(requests) == 1
        assert coordinator.store.usage(result.run_id)[0]["input_tokens"] == 10


async def test_fork_inherits_next_run_choice_without_authorization(tmp_path):
    async with service(tmp_path) as (coordinator, _, _):
        item = coordinator.providers.create(**profile_values(), api_key=SECRET)
        sid = coordinator.create_session(tmp_path)["id"]
        selected = coordinator.select_session(sid, provider_id=item["id"], model="test-model", reasoning_effort="low")
        child = coordinator.branch(sid, side_chat=True)
        assert coordinator.session_selection(child["id"]) == selected
        assert coordinator.store.list_runs(child["id"]) == []
        assert coordinator.store.pending_requests() == []
        coordinator.providers.delete(item["id"], item["revision"])
        fallback = coordinator.branch(sid)
        assert "selection_warning" in fallback
        assert coordinator.session_selection(fallback["id"])["provider_id"] == "deepseek"


async def test_legacy_payload_cannot_recover_removed_credential_binding(tmp_path):
    async with service(tmp_path, fake=False) as (coordinator, _, vault):
        vault.set_password("aster", "deepseek", SECRET)
        sid = coordinator.create_session(tmp_path)["id"]
        from aster.config import load_config
        settings = load_config(tmp_path, cwd=tmp_path).data
        payload = {"snapshot": settings, "agent_id": "default", "project_id": None, "unattended": False}
        run, _ = coordinator.store.create_run(sid, request_payload=payload)
        coordinator.providers.delete_key("deepseek", 1)
        from aster.service import ConfigurationError
        with pytest.raises(ConfigurationError, match="predates"):
            await coordinator._build_engine(run, payload)
        assert vault.get_password("aster", "deepseek") == SECRET


async def test_existing_yaml_deepseek_model_is_not_blocked_by_new_ui_catalogue(tmp_path, monkeypatch):
    async with service(tmp_path, fake=False) as (coordinator, _, vault):
        from aster.providers.deepseek import ModelCapabilities
        calls = []

        class LegacyModel:
            def __init__(self, key, **kwargs):
                self.model, self.max_output_tokens = kwargs["model"], kwargs["max_output_tokens"]

            async def refresh_capabilities(self, **kwargs):
                calls.append(self.model)
                return {self.model: ModelCapabilities(self.model, 50000, 8192, ("text",), ("high",), "fixture", {})}

            async def generate(self, *args, **kwargs):
                return ModelTurn("legacy YAML preserved")

            async def aclose(self):
                pass

        monkeypatch.setattr("aster.service.coordinator.DeepSeekResponsesClient", LegacyModel)
        (tmp_path / "config.yaml").write_text("model: legacy-configured-model\n", encoding="utf-8")
        vault.set_password("aster", "deepseek", SECRET)
        sid = coordinator.create_session(tmp_path)["id"]
        run = await coordinator.submit(sid, "retain YAML choice")
        result = await coordinator.wait(run["id"], timeout=3)
        assert result.status == "completed", result.error
        assert result.text == "legacy YAML preserved" and calls == ["legacy-configured-model"]


async def test_selected_skill_pins_source_and_restrictions_before_queue_starts(tmp_path):
    from aster.domain import ToolCall, ToolResult, ToolSpec

    class Write:
        spec = ToolSpec("fixture_write", "fixture write", {"type": "object", "properties": {}, "additionalProperties": False}, risk="write")
        executions = 0

        async def execute(self, arguments, context):
            self.executions += 1
            return ToolResult("should not execute")

    path = tmp_path / "skills" / "limited" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nname: limited\nallowed-tools: [file_read]\n---\nOld selected instructions", encoding="utf-8")
    tool = Write()
    model = FakeModel([ModelTurn(tool_calls=[ToolCall("forbidden", "fixture_write", {})]), ModelTurn("respected restriction")])
    coordinator = RunCoordinator(tmp_path, tools=[tool], model_factory=lambda _: model, credentials=CredentialStore(backend=Vault()), max_parallel_runs=1)
    await coordinator.start()
    try:
        sid = coordinator.create_session(tmp_path)["id"]
        await coordinator._capacity.acquire()
        run = await coordinator.submit(sid, "use selected skill", selected_skill="limited")
        payload = coordinator.store.run_request(run["id"])
        assert payload["selected_skill_snapshot"]["allowed_tools"] == ["file_read"]
        path.write_text("---\nname: limited\nallowed-tools: [fixture_write]\n---\nNew changed instructions", encoding="utf-8")
        coordinator._capacity.release()
        result = await coordinator.wait(run["id"], timeout=3)
        assert result.status == "completed", result.error
        assert tool.executions == 0
        assert "Old selected instructions" in model.calls[0]["system"]
        assert "New changed instructions" not in model.calls[0]["system"]
        results = [m for m in coordinator.store.history(sid) if m.role == "tool"]
        assert results and results[0].metadata["is_error"]
    finally:
        await coordinator.close()


def test_cli_configure_rebinds_detached_profile_without_duplicate_key(tmp_path, monkeypatch):
    from aster.cli import main

    vault = Vault()
    credentials = CredentialStore(backend=vault)
    store = Store(tmp_path / "runtime.db")
    profiles = ProviderProfiles(store, credentials)
    profiles.delete_key("deepseek", 1)
    store.close()
    def local_credentials(*, data_dir):
        assert data_dir == tmp_path
        return credentials

    monkeypatch.setattr("aster.cli.CredentialStore", local_credentials)
    monkeypatch.setattr("aster.cli.getpass.getpass", lambda prompt: SECRET)
    assert main(["--data-dir", str(tmp_path), "configure"]) == 0
    store = Store(tmp_path / "runtime.db")
    try:
        profiles = ProviderProfiles(store, credentials)
        assert profiles.get("deepseek")["credential_source"] == "vault"
        assert profiles.resolve_pin(profiles.pin("deepseek", "deepseek-flash", "high")) == SECRET
        assert len(vault.values) == 1 and ("aster", "deepseek") not in vault.values
    finally:
        store.close()


def test_cli_configure_routes_to_matching_daemon_before_sending_key(tmp_path, monkeypatch):
    from aster.cli import _configure_key

    seen = []
    status = {"running": True, "kind": "daemon", "host": "127.0.0.1", "port": 9876, "instance_id": "expected"}
    monkeypatch.setattr("aster.maintenance.service_status", lambda _: status)
    monkeypatch.setattr("aster.cli.load_api_token", lambda _: TOKEN)
    original_client = httpx.Client

    def handler(request):
        seen.append(request)
        if request.url.path == "/health":
            return httpx.Response(200, json={"instance_id": "expected"})
        assert request.headers["authorization"] == "Bearer " + TOKEN
        if request.url.path == "/providers":
            return httpx.Response(200, json={"providers": [{"id": "deepseek", "revision": 6}]})
        assert request.method == "PATCH" and json.loads(request.content) == {"expected_revision": 6, "api_key": SECRET}
        return httpx.Response(200, json={"configured": True})

    monkeypatch.setattr("aster.cli.httpx.Client", lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs))
    _configure_key(tmp_path, SECRET)
    assert len(seen) == 3 and SECRET not in str(seen[0].url)
    seen.clear()
    status["instance_id"] = "different"
    with pytest.raises(ValueError, match="identity"):
        _configure_key(tmp_path, SECRET)
    assert len(seen) == 1 and not seen[0].content
