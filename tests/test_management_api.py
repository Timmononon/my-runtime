"""Real authenticated ASGI forms, immutable runs, and safe configuration writes."""
import asyncio
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
import yaml

from aster.api import create_app
from aster.config import load_config
from aster.customization.management import ManagementService
from aster.customization.transactions import ManagementRecoveryError
from aster.domain import ModelTurn
from aster.engine import FakeModel
from aster.service import RunCoordinator

TOKEN = "management-offline-token-at-least-thirty-two-characters"
SECRET = "fixture-management-secret-never-real"


@asynccontextmanager
async def service(root, *, model_factory=None):
    coordinator = RunCoordinator(root, tools=[], model_factory=model_factory or (lambda _: FakeModel([ModelTurn("offline")])) )
    app = create_app(coordinator, token=TOKEN)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765",
        headers={"Authorization": "Bearer " + TOKEN}
    ) as client:
        yield coordinator, app, client


async def management(client):
    response = await client.get("/management")
    assert response.status_code == 200, response.text
    return response.json()


async def test_global_form_cas_preserves_unknown_fields_and_never_runs_hooks(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("max_iterations: 123\nfuture_option: {preserve: true}\n", encoding="utf-8")
    async with service(tmp_path) as (coordinator, _, client):
        state = await management(client)
        assert state["extensions"]["embedding"]["enabled"] is False
        assert state["extensions"]["browser"]["mode"] == "isolated"
        marker = tmp_path / "hook-was-executed.txt"
        body = {"expected_revision": state["revision"], "permissions": {"allowed_tools": ["file_read"]},
                "hooks": [{"event": "PreToolUse", "executable": "offline-never-execute",
                           "args": [str(marker)], "timeout_seconds": 15, "output_limit": 16384,
                           "tool_names": None}]}
        saved = await client.patch("/management", json=body)
        assert saved.status_code == 200, saved.text
        assert saved.json()["revision"] != state["revision"]
        document = yaml.safe_load(config.read_text())
        assert document["future_option"] == {"preserve": True} and document["max_iterations"] == 123
        assert not marker.exists() and coordinator.store.list_runs() == []
        assert (await client.patch("/management", json=body)).status_code == 409
        assert (await client.patch("/management", json={"expected_revision": saved.json()["revision"],
                                                       "unknown_secret": SECRET})).status_code == 422


async def test_agent_selection_instructions_future_runs_and_active_snapshot_are_immutable(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    captured = []

    async def first(messages, tools):
        entered.set()
        await release.wait()
        return ModelTurn("first run preserved")

    model = FakeModel([first, ModelTurn("second run")])

    def model_factory(settings):
        captured.append(settings)
        return model

    async with service(tmp_path, model_factory=model_factory) as (coordinator, _, client):
        profile = coordinator.providers.create(name="Offline fixture", base_url="https://provider.example/v1",
            protocol="openai_chat", models=[{"id": mid, "effort_levels": ["high"]} for mid in ("model-one", "model-two")])
        state = await management(client)
        selection = {"provider_id": profile["id"], "model": "model-one", "reasoning_effort": "high", "selected_skill": None}
        created = await client.post("/management/agents", json={"expected_revision": state["revision"], "id": "writer",
            "name": "Writer", "instructions": "old agent instruction", "selection": selection, "permissions": {}})
        assert created.status_code == 201, created.text
        agent = next(item for item in created.json()["agents"] if item["id"] == "writer")
        sid = coordinator.create_session(tmp_path, agent_id="writer")["id"]
        run = await coordinator.submit(sid, "original request")
        await entered.wait()
        immutable = coordinator.store.run_request(run["id"])
        updated = await client.put("/management/agents/writer", json={"expected_revision": agent["revision"],
            "instructions": "new agent instruction", "selection": {**selection, "model": "model-two"}})
        assert updated.status_code == 200, updated.text
        assert coordinator.store.run_request(run["id"]) == immutable
        assert captured[0]["model"] == "model-one" and "old agent instruction" in immutable["instructions"]
        release.set()
        assert (await coordinator.wait(run["id"], timeout=2)).status == "completed"
        assert coordinator.session_selection(sid)["model"] == "model-one"
        new_sid = coordinator.create_session(tmp_path, agent_id="writer")["id"]
        second = await coordinator.submit(new_sid, "later request")
        assert (await coordinator.wait(second["id"], timeout=2)).status == "completed"
        assert captured[1]["model"] == "model-two"
        assert "new agent instruction" in coordinator.store.run_request(second["id"])["instructions"]
        assert (await client.put("/management/agents/writer", json={"expected_revision": agent["revision"],
                                                                   "name": "stale"})).status_code == 409
        assert (await client.delete("/management/agents/writer")).status_code == 405


async def test_agent_permissions_cannot_expand_global_scope_and_invalid_skill_is_rejected(tmp_path):
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"permissions": {
        "allowed_tools": ["file_read", "text_search"], "auto_approve_tools": ["file_read"],
        "denied_tools": ["shell"]}}), encoding="utf-8")
    async with service(tmp_path) as (_, _, client):
        state = await management(client)
        body = {"expected_revision": state["revision"], "id": "reader", "instructions": "read",
                "permissions": {"allowed_tools": ["file_read", "memory_record"],
                                "auto_approve_tools": ["file_read", "memory_record"]}}
        saved = await client.post("/management/agents", json=body)
        assert saved.status_code == 201, saved.text
        agent = next(item for item in saved.json()["agents"] if item["id"] == "reader")
        assert agent["effective_permissions"]["allowed_tools"] == ["file_read"]
        assert agent["effective_permissions"]["auto_approve_tools"] == ["file_read"]
        assert agent["effective_permissions"]["denied_tools"] == ["shell"]
        invalid = await client.put("/management/agents/reader", json={"expected_revision": agent["revision"],
            "selection": {"provider_id": "deepseek", "model": "deepseek-flash", "reasoning_effort": "high",
                          "selected_skill": "missing-skill"}})
        assert invalid.status_code == 422 and invalid.json()["detail"] == "Management fields are invalid"
        assert (await client.get("/management/agents/reader")).json()["revision"] == agent["revision"]


async def test_mcp_forms_credentials_diagnostics_reconnect_and_remove_are_real(tmp_path, monkeypatch):
    ready = False
    calls = 0

    class Session:
        async def list_tools(self, cursor=None):
            return SimpleNamespace(tools=[{"name": "read", "inputSchema": {"type": "object"}}], nextCursor=None)

    @asynccontextmanager
    async def connector(config, secret_resolver=None):
        nonlocal calls
        calls += 1
        if not ready:
            raise ConnectionError(SECRET)
        assert secret_resolver(config.env["ACCESS_TOKEN"]) == SECRET
        yield Session()

    monkeypatch.setattr("aster.mcp.client.sdk_connection", connector)
    async with service(tmp_path) as (coordinator, _, client):
        state = await management(client)
        key = await client.post("/management/credentials", json={"expected_revision": state["revision"],
                                                                 "account": "mcp-test", "value": SECRET})
        assert key.status_code == 201 and SECRET not in key.text
        saved = await client.patch("/management", json={"expected_revision": state["revision"], "mcp_servers": [{
            "name": "fixture", "command": "offline-fixture", "env": {"ACCESS_TOKEN": key.json()["credential_ref"]},
            "connect_attempts": 1, "reconnect_attempts": 0, "timeout_seconds": 0.1}]})
        assert saved.status_code == 200 and calls == 0
        assert saved.json()["mcp_servers"][0]["health"]["status"] == "not_connected"
        bad = await client.post("/management/mcp/fixture/diagnose")
        assert bad.status_code == 200 and bad.json()["health"][0]["status"] == "exhausted"
        assert SECRET not in bad.text and calls == 1
        ready = True
        good = await client.post("/management/mcp/fixture/reconnect")
        assert good.status_code == 200 and good.json()["health"][0]["status"] == "healthy"
        assert good.json()["health"][0]["generation"] == 2 and calls == 2
        after = await management(client)
        removed = await client.patch("/management", json={"expected_revision": after["revision"], "mcp_servers": []})
        assert removed.status_code == 200 and removed.json()["mcp_servers"] == []
        assert SECRET not in (tmp_path / "config.yaml").read_text()
        assert (await client.post("/management/mcp/fixture/reconnect")).status_code == 409
        assert not coordinator._mcp_retiring


async def test_secret_redaction_and_validation_never_echo_rejected_values(tmp_path):
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"mcp_servers": [{"name": "legacy", "command": "unused",
        "args": ["--token", SECRET], "env": {"PRIVATE_VALUE": SECRET}, "headers": {"X-Private": SECRET}}]}), encoding="utf-8")
    async with service(tmp_path) as (_, _, client):
        response = await client.get("/management")
        assert response.status_code == 200 and SECRET not in response.text
        assert response.json()["mcp_servers"][0]["env"]["PRIVATE_VALUE"] == "<redacted>"
        revision = response.json()["revision"]
        bodies = [{"expected_revision": revision, "mcp_servers": [{"name": "bad", "command": "unused",
                   "headers": {"Authorization": SECRET}}]},
                  {"expected_revision": revision, SECRET: SECRET},
                  {"expected_revision": revision, "permissions": {"allowed_tools": SECRET}}]
        for body in bodies:
            rejected = await client.patch("/management", json=body)
            assert rejected.status_code == 422 and SECRET not in rejected.text
        invalid = await client.post("/management/credentials", json={"expected_revision": revision,
            "account": "unsafe/" + SECRET, "value": SECRET + "\n"})
        assert invalid.status_code == 422 and SECRET not in invalid.text
        assert not (tmp_path / "credentials.yaml").exists()


async def test_extensions_save_validates_without_network_and_clears_changed_search_binding(tmp_path, monkeypatch):
    async def forbid_dns(*args, **kwargs):
        pytest.fail("save must not contact the configured service")

    monkeypatch.setattr("aster.providers.profiles.verify_public_endpoint", forbid_dns)
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"http_credential_origins": {
        "env:UNRELATED_TOKEN": ["https://other.example:443"]}}), encoding="utf-8")
    async with service(tmp_path) as (_, _, client):
        state = await management(client)
        saved = await client.patch("/management", json={"expected_revision": state["revision"],
            "web_search": {"backend": "searxng", "base_url": "https://search.example", "credential_ref": "env:SEARCH_TOKEN"},
            "browser": {"mode": "bridge", "headless": True, "cdp_endpoint": "http://127.0.0.1:9222",
                        "allowed_pages": ["https://example.com/page"]}, "embedding": {"enabled": False}})
        assert saved.status_code == 200, saved.text
        origins = yaml.safe_load((tmp_path / "config.yaml").read_text())["http_credential_origins"]
        assert origins["env:SEARCH_TOKEN"] == ["https://search.example:443"]
        assert origins["env:UNRELATED_TOKEN"] == ["https://other.example:443"]
        changed = await client.patch("/management", json={"expected_revision": saved.json()["revision"],
            "web_search": {"backend": "searxng", "base_url": "https://different.example", "credential_ref": "env:SEARCH_TOKEN"}})
        assert changed.status_code == 200 and changed.json()["extensions"]["web_search"]["credential_ref"] is None
        document = yaml.safe_load((tmp_path / "config.yaml").read_text())
        assert document["http_credential_origins"]["env:SEARCH_TOKEN"] == []
        assert document["http_credential_origins"]["env:UNRELATED_TOKEN"] == ["https://other.example:443"]
        for key, invalid in (("web_search", {"backend": "searxng", "base_url": "http://127.0.0.1:9999"}),
                             ("browser", {"mode": "bridge", "cdp_endpoint": "http://remote.example:9222", "allowed_pages": []}),
                             ("embedding", {"enabled": True, "provider_id": "deepseek", "model": "embedding-fixture"})):
            rejected = await client.patch("/management", json={"expected_revision": changed.json()["revision"], key: invalid})
            assert rejected.status_code == 422


async def test_search_disable_roundtrip_stays_empty_and_can_be_reconfigured(tmp_path, monkeypatch):
    async def forbid_dns(*args, **kwargs):
        pytest.fail("management reads/saves must never contact a search service")

    monkeypatch.setattr("aster.providers.profiles.verify_public_endpoint", forbid_dns)
    config = tmp_path / "config.yaml"
    unrelated = {"env:UNRELATED_TOKEN": ["https://other.example:443"]}
    config.write_text(yaml.safe_dump({"http_credential_origins": unrelated}), encoding="utf-8")
    async with service(tmp_path) as (coordinator, _, client):
        state = await management(client)
        enabled = await client.patch("/management", json={"expected_revision": state["revision"],
            "web_search": {"backend": "searxng", "base_url": "https://search.example"}})
        assert enabled.status_code == 200, enabled.text
        assert enabled.json()["extensions"]["web_search"]["credential_ref"] is None
        disabled = await client.patch("/management", json={"expected_revision": enabled.json()["revision"],
                                                          "web_search": {}})
        assert disabled.status_code == 200, disabled.text
        assert disabled.json()["extensions"]["web_search"] == {}
        persisted = yaml.safe_load(config.read_text())
        assert persisted["web_search"] == {} and persisted["http_credential_origins"] == unrelated
        reloaded = await management(client)
        assert reloaded["extensions"]["web_search"] == {} and reloaded["revision"] == disabled.json()["revision"]
        configured = await client.patch("/management", json={"expected_revision": reloaded["revision"],
            "web_search": {"backend": "searxng", "base_url": "https://new-search.example",
                           "credential_ref": "env:SEARCH_TOKEN"}})
        assert configured.status_code == 200, configured.text
        persisted = yaml.safe_load(config.read_text())
        assert persisted["http_credential_origins"] == {**unrelated, "env:SEARCH_TOKEN": ["https://new-search.example:443"]}
        disabled_again = await client.patch("/management", json={"expected_revision": configured.json()["revision"],
                                                                "web_search": {}})
        assert disabled_again.status_code == 200 and disabled_again.json()["extensions"]["web_search"] == {}
        persisted = yaml.safe_load(config.read_text())
        assert persisted["web_search"] == {} and "web_search_binding" not in persisted
        assert persisted["http_credential_origins"] == {**unrelated, "env:SEARCH_TOKEN": []}
        assert coordinator.store.list_runs() == [] and not (tmp_path / "credentials.yaml").exists()


async def test_legacy_null_search_reference_loads_disabled_without_rewriting_config(tmp_path, monkeypatch):
    async def forbid_dns(*args, **kwargs):
        pytest.fail("loading legacy disabled settings must not perform a network operation")

    monkeypatch.setattr("aster.providers.profiles.verify_public_endpoint", forbid_dns)
    config = tmp_path / "config.yaml"
    original = yaml.safe_dump({"web_search": {"credential_ref": None}, "future_option": "preserved",
                              "http_credential_origins": {"env:UNRELATED_TOKEN": ["https://other.example:443"]}}).encode()
    config.write_bytes(original)
    async with service(tmp_path) as (coordinator, _, client):
        state = await management(client)
        assert state["extensions"]["web_search"] == {}
        assert config.read_bytes() == original
        assert (await management(client))["revision"] == state["revision"]
        saved = await client.patch("/management", json={"expected_revision": state["revision"], "web_search": {}})
        assert saved.status_code == 200 and saved.json()["extensions"]["web_search"] == {}
        persisted = yaml.safe_load(config.read_text())
        assert persisted["web_search"] == {} and persisted["future_option"] == "preserved"
        assert persisted["http_credential_origins"] == {"env:UNRELATED_TOKEN": ["https://other.example:443"]}
        assert coordinator.store.list_runs() == []


@pytest.mark.parametrize("invalid", [
    {"credential_ref": "env:SEARCH_TOKEN"},
    {"credential_ref": None, "backend": "searxng"},
    {"credential_ref": None, "timeout_seconds": 15},
    {"backend": "searxng", "base_url": ""},
    {"backend": "searxng", "base_url": "http://127.0.0.1:8765", "credential_ref": "env:SEARCH_TOKEN"},
    {"backend": "brave", "base_url": "https://changed.example", "credential_ref": "env:SEARCH_TOKEN"},
])
async def test_incomplete_or_unsafe_search_stays_rejected_without_origin_changes(tmp_path, invalid):
    config = tmp_path / "config.yaml"
    original = yaml.safe_dump({"web_search": invalid,
                              "http_credential_origins": {"env:UNRELATED_TOKEN": ["https://other.example:443"]}}).encode()
    config.write_bytes(original)
    async with service(tmp_path) as (coordinator, _, client):
        rejected_read = await client.get("/management")
        assert rejected_read.status_code == 422 and rejected_read.json()["error"] == "invalid_request"
        assert all(value not in rejected_read.text for key, value in invalid.items()
                   if key in {"credential_ref", "base_url"} and isinstance(value, str) and value)
        revision = ManagementService(coordinator).revision()
        rejected_save = await client.patch("/management", json={"expected_revision": revision, "web_search": invalid})
        assert rejected_save.status_code == 422 and rejected_save.json()["detail"] == "Management fields are invalid"
        assert config.read_bytes() == original


async def test_redacted_arguments_roundtrip_preserves_values_but_changed_identity_is_rejected(tmp_path):
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"mcp_servers": [{"name": "legacy", "command": "unchanged",
        "args": ["--token", SECRET]}], "hooks": [{"event": "Stop", "executable": "unchanged-hook",
        "args": ["--password=" + SECRET]}]}), encoding="utf-8")
    async with service(tmp_path) as (_, _, client):
        state = await management(client)
        server = {key: value for key, value in state["mcp_servers"][0].items() if key != "health"}
        server["timeout_seconds"] = 75
        hook = {**state["hooks"][0], "timeout_seconds": 30}
        saved = await client.patch("/management", json={"expected_revision": state["revision"],
                                                        "mcp_servers": [server], "hooks": [hook]})
        assert saved.status_code == 200 and SECRET not in saved.text
        document = yaml.safe_load((tmp_path / "config.yaml").read_text())
        assert document["mcp_servers"][0]["args"] == ["--token", SECRET]
        assert document["hooks"][0]["args"] == ["--password=" + SECRET]
        rejected = await client.patch("/management", json={"expected_revision": saved.json()["revision"],
            "mcp_servers": [{**server, "command": "different-command"}]})
        assert rejected.status_code == 422 and SECRET not in rejected.text


async def test_management_authentication_cookie_csrf_and_capability_contract(tmp_path):
    async with service(tmp_path) as (coordinator, app, client):
        assert (await client.get("/management", headers={"Authorization": ""})).status_code == 401
        ticket = (await client.post("/ui/tickets", json={})).json()["ticket"]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765") as browser:
            headers = {"X-Aster-UI": "1", "Origin": "http://localhost:8765"}
            exchange = await browser.post("/ui/auth", json={"ticket": ticket}, headers=headers)
            assert exchange.status_code == 200, exchange.text
            state = (await browser.get("/management", headers=headers)).json()
            payload = {"expected_revision": state["revision"], "permissions": {"allowed_tools": []}}
            assert (await browser.patch("/management", json=payload, headers={"X-Aster-UI": "1"})).status_code == 403
            assert (await browser.patch("/management", json=payload, headers=headers)).status_code == 200
        caps = (await client.get("/capabilities")).json()["capabilities"]
        assert {"management_v1", "mcp_health_v1", "automation_selection_v1", "performance_timeline_v1",
                "browser_handoff_v1", "web_search_v1", "memory_graph_v1", "memory_semantic_recall_v1"} <= set(caps)
        sid = coordinator.create_session(tmp_path)["id"]
        status = await client.get(f"/browser-control/sessions/{sid}")
        assert status.status_code == 200 and status.json()["attached"] is False
        assert coordinator.store.list_runs() == []


async def test_agent_file_pair_blocks_admission_without_store_lock_inversion(tmp_path, monkeypatch):
    async with service(tmp_path) as (coordinator, _, client):
        initial = await management(client)
        created = await client.post("/management/agents", json={"expected_revision": initial["revision"],
            "id": "pair", "instructions": "old coherent instruction"})
        agent = next(item for item in created.json()["agents"] if item["id"] == "pair")
        manager = ManagementService(coordinator)
        config_path = tmp_path / "agents" / "pair" / "config.yaml"
        midway, release, reader_entered = threading.Event(), threading.Event(), threading.Event()
        from aster.customization import management as module
        real_write = module.atomic_write

        def paused_write(path, content):
            real_write(path, content)
            if path == config_path:
                midway.set()
                assert release.wait(3), "test did not release the file-pair writer"

        monkeypatch.setattr(module, "atomic_write", paused_write)
        writer = asyncio.create_task(asyncio.to_thread(manager.save_agent, "pair", agent["revision"],
            {"name": "new coherent name", "instructions": "new coherent instruction"}))
        assert await asyncio.to_thread(midway.wait, 2)

        def admission_inside_transaction():
            with coordinator.store.transaction():
                reader_entered.set()
                return load_config(tmp_path, agent_id="pair")

        reader = asyncio.create_task(asyncio.to_thread(admission_inside_transaction))
        assert await asyncio.to_thread(reader_entered.wait, 2)
        await asyncio.sleep(0.03)
        assert not reader.done()
        release.set()
        await asyncio.wait_for(writer, 2)
        snapshot = await asyncio.wait_for(reader, 2)
        assert snapshot.data["name"] == "new coherent name" and "new coherent instruction" in snapshot.instructions
        assert "old coherent instruction" not in snapshot.instructions


async def test_agent_mid_commit_failure_rolls_back_both_files_and_null_is_rejected_before_write(tmp_path, monkeypatch):
    async with service(tmp_path) as (_, _, client):
        initial = await management(client)
        created = await client.post("/management/agents", json={"expected_revision": initial["revision"],
            "id": "rollback", "name": "Original", "instructions": "original instruction"})
        agent = next(item for item in created.json()["agents"] if item["id"] == "rollback")
        folder = tmp_path / "agents" / "rollback"
        before = {name: (folder / name).read_bytes() for name in ("config.yaml", "AGENT.md")}
        from aster.customization import management as module
        real_write, failed = module.atomic_write, False

        def fail_instruction_once(path, content):
            nonlocal failed
            if path == folder / "AGENT.md" and not failed:
                failed = True
                raise OSError(SECRET)
            real_write(path, content)

        monkeypatch.setattr(module, "atomic_write", fail_instruction_once)
        response = await client.put("/management/agents/rollback", json={"expected_revision": agent["revision"],
            "name": "Changed", "instructions": "changed instruction"})
        assert response.status_code == 503 and SECRET not in response.text
        assert all((folder / name).read_bytes() == content for name, content in before.items())
        assert (await client.get("/management/agents/rollback")).json()["revision"] == agent["revision"]
        null = await client.put("/management/agents/rollback", json={"expected_revision": agent["revision"],
                                                                    "name": None, "instructions": None})
        assert null.status_code == 422
        assert all((folder / name).read_bytes() == content for name, content in before.items())


async def test_automation_trigger_endpoint_returns_captured_selection_without_internal_json(tmp_path):
    coordinator = RunCoordinator(tmp_path, tools=[], model_factory=lambda _: FakeModel([]))
    app = create_app(coordinator, token=TOKEN, manage_lifecycle=False)
    try:
        sid = coordinator.create_session(tmp_path)["id"]
        job = coordinator.automation.create_job("schedule", sid, "offline receipt", {"cron": "* * * * *"})
        coordinator.automation._claim(job, "explicit-offline-fixture", 1000, job["prompt"], 1000)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost",
                                    headers={"Authorization": "Bearer " + TOKEN}) as client:
            response = await client.get(f"/automations/{job['id']}/triggers")
            assert response.status_code == 200 and "execution_json" not in response.text
            receipt = response.json()["triggers"][0]
            assert receipt["selection_captured"] and receipt["selection_mode"] == "fixed"
            assert receipt["selection"]["model"] == "deepseek-flash"
            assert coordinator.store.list_runs() == []
            assert (await client.get("/automations/missing/triggers")).status_code == 404
    finally:
        await coordinator.close()


@pytest.mark.parametrize("conflicting_edit", [False, True])
async def test_crashed_agent_pair_recovers_coherently_or_preserves_conflicting_user_edit(tmp_path, monkeypatch, conflicting_edit):
    async with service(tmp_path) as (coordinator, _, client):
        initial = await management(client)
        created = await client.post("/management/agents", json={"expected_revision": initial["revision"],
            "id": "crash", "instructions": "before crash instruction", "name": "Before"})
        agent = next(item for item in created.json()["agents"] if item["id"] == "crash")
        folder = tmp_path / "agents" / "crash"
        from aster.customization import management as module
        real_write = module.atomic_write

        def interrupted_after_first_file(path, content):
            real_write(path, content)
            if path == folder / "config.yaml":
                raise SystemExit("simulated process loss after first atomic replacement")

        monkeypatch.setattr(module, "atomic_write", interrupted_after_first_file)
        with pytest.raises(SystemExit):
            ManagementService(coordinator).save_agent("crash", agent["revision"],
                {"name": "After", "instructions": "after crash instruction"})
        journal = folder / ".management-transaction.json"
        assert journal.exists()
        if conflicting_edit:
            (folder / "AGENT.md").write_text("manual post-crash user edit", encoding="utf-8")
            before = {name: (folder / name).read_bytes() for name in ("config.yaml", "AGENT.md")}
            with pytest.raises(ManagementRecoveryError):
                load_config(tmp_path, agent_id="crash")
            assert journal.exists() and all((folder / name).read_bytes() == content for name, content in before.items())
            assert load_config(tmp_path, agent_id="default").agent_id == "default"
        else:
            snapshot = load_config(tmp_path, agent_id="crash")
            assert snapshot.data["name"] == "After" and "after crash instruction" in snapshot.instructions
            assert "before crash instruction" not in snapshot.instructions and not journal.exists()
