"""Automatic memory settings use real operator routes and never call a model."""
import json

import httpx
import pytest
import yaml
from test_management_api import management, service

KEY = "synthetic-typesafe-key-not-real"


async def test_auto_memory_default_read_is_local_and_legacy_config_unchanged(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    path = tmp_path / "config.yaml"
    original = b"max_iterations: 123\nfuture_option: keep\n"
    path.write_bytes(original)
    async with service(tmp_path) as (coordinator, _, client):
        def forbidden(*args, **kwargs):
            raise AssertionError("Management must never connect to a model")
        coordinator.providers.client_factory = forbidden
        async def forbidden_http(*args, **kwargs):
            raise AssertionError("Management must never send an external request")
        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden_http)
        state = await management(client)
        config = state["extensions"]["auto_memory"]
        status = state["extensions"]["auto_memory_status"]
        assert config["enabled"] is False and config["model"] == "jev-1.14.0"
        assert status == {"enabled": False, "configured": False, "gate_configured": False,
                          "extractor_configured": False, "status": "disabled",
                          "missing": ["jev_credential", "deepseek_extractor"]}
        assert path.read_bytes() == original
        assert coordinator.store.list_runs() == []
        response = await client.get("/capabilities")
        assert response.status_code == 200
        assert "memory_auto_capture_v1" in response.json()["capabilities"]


async def test_auto_memory_write_only_key_enable_cas_reopen_and_disable(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    async with service(tmp_path) as (coordinator, _, client):
        before = await management(client)
        key = await client.post("/management/credentials", json={"expected_revision": before["revision"],
                                                                 "account": "jev", "value": KEY})
        assert key.status_code == 201 and KEY not in key.text
        assert key.json()["credential_ref"] == "keyring:aster/jev"
        still_disabled = await management(client)
        assert still_disabled["extensions"]["auto_memory_status"]["gate_configured"] is True
        assert still_disabled["extensions"]["auto_memory"]["enabled"] is False
        denied = await client.patch("/management", json={"expected_revision": before["revision"],
                                                          "auto_memory": {"enabled": True}})
        assert denied.status_code == 422 and KEY not in denied.text
        coordinator.credentials.set("deepseek", "synthetic-deepseek-not-real")
        config = {"enabled": True, "threshold": 0.85, "timeout_seconds": 15}
        saved = await client.patch("/management", json={"expected_revision": before["revision"], "auto_memory": config})
        assert saved.status_code == 200, saved.text
        body = saved.json()
        assert body["extensions"]["auto_memory_status"]["status"] == "ready"
        assert body["extensions"]["auto_memory"]["threshold"] == .85
        assert KEY not in saved.text and KEY not in (tmp_path / "config.yaml").read_text()
        persisted = yaml.safe_load((tmp_path / "config.yaml").read_text())
        assert persisted["auto_memory"]["credential_ref"] == "keyring:aster/jev"
        assert (await management(client))["extensions"] == body["extensions"]
        stale = await client.patch("/management", json={"expected_revision": before["revision"],
                                                         "auto_memory": {"enabled": False}})
        assert stale.status_code == 409
        coordinator.credentials.delete("jev")
        missing = await management(client)
        assert missing["extensions"]["auto_memory_status"]["status"] == "unconfigured"
        disabled = await client.patch("/management", json={"expected_revision": body["revision"],
                                                            "auto_memory": {**config, "enabled": False}})
        assert disabled.status_code == 200
        assert disabled.json()["extensions"]["auto_memory_status"]["status"] == "disabled"
        assert coordinator.store.list_runs() == []


@pytest.mark.parametrize("value", [
    None, {"enabled": "yes"}, {"threshold": 1.1}, {"threshold": True},
    {"api_key": KEY}, {"credential_ref": KEY}, {"base_url": "https://elsewhere.example"},
    {"extractor_model": "missing-model", "enabled": True},
])
async def test_auto_memory_rejects_bad_fields_without_reflecting_secrets(tmp_path, monkeypatch, value):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    async with service(tmp_path) as (coordinator, _, client):
        coordinator.credentials.set("jev", KEY)
        coordinator.credentials.set("deepseek", "synthetic-deepseek-not-real")
        state = await management(client)
        response = await client.patch("/management", json={"expected_revision": state["revision"], "auto_memory": value})
        assert response.status_code == 422, response.text
        assert KEY not in response.text
        assert (await management(client))["revision"] == state["revision"]


async def test_auto_memory_does_not_trust_agent_configuration_for_status(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    agent = tmp_path / "agents" / "default"
    agent.mkdir(parents=True)
    (agent / "config.yaml").write_text("auto_memory:\n  enabled: true\n  credential_ref: env:WRONG_KEY\n", encoding="utf-8")
    async with service(tmp_path) as (_, _, client):
        state = await management(client)
        assert state["extensions"]["auto_memory"]["enabled"] is False
        assert state["extensions"]["auto_memory"]["credential_ref"] == "keyring:aster/jev"


async def test_auto_memory_requires_extractor_nonreasoning_capability(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    async with service(tmp_path) as (coordinator, _, client):
        coordinator.credentials.set("jev", KEY)
        coordinator.credentials.set("deepseek", "synthetic-deepseek-not-real")
        # Simulate a stored older catalogue; current catalogue writes add "none".
        with coordinator.store.transaction() as db:
            document = json.loads(db.execute("SELECT document FROM provider_profiles WHERE id='deepseek'").fetchone()[0])
            document["models"][0]["effort_levels"] = ["high"]
            db.execute("UPDATE provider_profiles SET document=? WHERE id='deepseek'", (json.dumps(document),))
        state = await management(client)
        assert state["extensions"]["auto_memory_status"]["extractor_configured"] is False
        response = await client.patch("/management", json={"expected_revision": state["revision"],
                                                           "auto_memory": {"enabled": True}})
        assert response.status_code == 422
