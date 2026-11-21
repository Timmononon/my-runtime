"""Authenticated native auth diagnostics never create work or expose credentials."""
from __future__ import annotations

import asyncio
import copy
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

from aster.api import create_app
from aster.api import feishu as feishu_api
from aster.domain import ToolResult
from aster.service import RunCoordinator
from aster.tools import feishu as feishu_cli

TOKEN = "offline-feishu-auth-api-token-with-over-thirty-two-characters"
ORIGIN = "http://127.0.0.1:8765"
BEARER = {"Authorization": "Bearer " + TOKEN}
UI_HEADERS = {"X-Aster-UI": "1", "Origin": ORIGIN}
STATUS = {
    "cli_installed": True,
    "app_configured": True,
    "user": {"available": False, "token_status": "missing", "scope_count": 0, "scopes": []},
    "bot": {"available": True, "token_status": "unknown"},
    "business_access_verified": False,
    "diagnostic_only": True,
}


@asynccontextmanager
async def api(tmp_path, monkeypatch, result=None):
    def forbid_model(*args):
        raise AssertionError("A local CLI diagnostic must never request a model")

    coordinator = RunCoordinator(tmp_path / "state", browser_enabled=False, model_factory=forbid_model)
    tool, = [item for item in coordinator.factory.tools if item.spec.name == "feishu_cli"]
    calls, registry_calls = [], []
    tool.diagnostic_business_calls = []

    async def diagnostic(context):
        calls.append(context)
        return result or ToolResult("must not be included in API response", data=copy.deepcopy(STATUS))

    def registry_metadata():
        registry_calls.append(True)
        return {"supported": True, "key_exists": True, "value_count": 3}

    monkeypatch.setattr(tool, "connection_status", diagnostic, raising=False)

    async def business(arguments, context):
        tool.diagnostic_business_calls.append((arguments, context))
        return ToolResult("sensitive calendar must not appear", data={
            "ok": True, "data": {"items": [{"id": "private-event-id", "title": "private-calendar-title"}]},
        })

    monkeypatch.setattr(tool, "execute", business)
    monkeypatch.setattr(feishu_api, "_windows_registry_metadata", registry_metadata)
    app = create_app(coordinator, token=TOKEN, manage_lifecycle=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
        try:
            yield client, coordinator, tool, calls, registry_calls
        finally:
            await coordinator.close()


async def test_status_checks_real_factory_without_model_run_or_database_mutation(tmp_path, monkeypatch):
    async with api(tmp_path, monkeypatch) as (client, coordinator, tool, calls, registry_calls):
        before = list(coordinator.store.connection.iterdump())
        response = await client.post("/feishu/check", headers=BEARER, json={})
        assert response.status_code == 200, response.text
        value = response.json()
        assert value["cli_installed"] and value["app_configured"]
        assert value["user"]["available"] is False
        assert value["diagnostic_only"] is True and value["business_access_verified"] is False
        assert value["windows_registry"] == {"supported": True, "key_exists": True, "value_count": 3}
        assert not value["is_error"] and value["outcome"] == "known"
        assert "must not be included" not in response.text
        assert len(calls) == len(registry_calls) == 1
        assert calls[0].cwd == coordinator.data_dir
        assert calls[0].session_id == "feishu-diagnostic"
        assert not tool.diagnostic_business_calls
        assert list(coordinator.store.connection.iterdump()) == before
        assert response.headers["cache-control"] == "no-store"
        assert "feishu_cli_status_v1" in (await client.get("/health")).json()["capabilities"]


async def test_status_is_explicit_post_and_unauthorized_requests_never_probe(tmp_path, monkeypatch):
    async with api(tmp_path, monkeypatch) as (client, _, _, calls, registry_calls):
        assert (await client.post("/feishu/check", json={})).status_code == 401
        assert (await client.post("/feishu/check", headers={"Authorization": "Bearer wrong"})).status_code == 401
        assert (await client.get("/feishu/check", headers=BEARER)).status_code == 405
        assert (await client.post("/feishu/check", headers={**BEARER, "Origin": "https://external.invalid"})).status_code == 403
        assert not calls and not registry_calls


@pytest.mark.parametrize("body", [{"argv": ["im", "+messages-send"]}, {"exe": "other.exe"},
                                  {"env": {"TOKEN": "never-echo-this"}}, {"profile": "other"}])
async def test_status_rejects_caller_controlled_execution_fields(tmp_path, monkeypatch, body):
    async with api(tmp_path, monkeypatch) as (client, _, _, calls, registry_calls):
        response = await client.post("/feishu/check", headers=BEARER, json=body)
        assert response.status_code == 422
        assert "never-echo-this" not in response.text
        assert not calls and not registry_calls
        assert (await client.post("/feishu/check?profile=other", headers=BEARER)).status_code == 422
        assert not calls and not registry_calls


async def test_cookie_status_requires_exact_origin_and_ui_header(tmp_path, monkeypatch):
    async with api(tmp_path, monkeypatch) as (client, _, _, calls, registry_calls):
        ticket = (await client.post("/ui/tickets", headers=BEARER, json={})).json()["ticket"]
        assert (await client.post("/ui/auth", headers=UI_HEADERS, json={"ticket": ticket})).status_code == 200
        assert (await client.post("/feishu/check", json={})).status_code == 403
        assert (await client.post("/feishu/check", headers={"X-Aster-UI": "1"}, json={})).status_code == 403
        assert (await client.post("/feishu/check", headers={"Origin": ORIGIN}, json={})).status_code == 403
        assert not calls and not registry_calls
        assert (await client.post("/feishu/check", headers=UI_HEADERS, json={})).status_code == 200
        assert len(calls) == len(registry_calls) == 1


async def test_failed_or_malformed_native_result_does_not_claim_success_or_leak(tmp_path, monkeypatch):
    raw = "never-echo-user-or-credential-material"
    failure = ToolResult(raw, True, "configuration", "not_executed", data={
        "cli_installed": False, "app_configured": False, "error_type": "configuration",
        "app_id": raw, "token": raw, "verification_url": raw,
    })
    async with api(tmp_path, monkeypatch, failure) as (client, _, tool, _, _):
        response = await client.post("/feishu/check", headers=BEARER)
        assert response.status_code == 503 and response.json()["is_error"]
        assert response.json()["outcome"] == "not_executed" and raw not in response.text

        async def malformed(context):
            return ToolResult(raw, data={"cli_installed": True, "token": raw})

        monkeypatch.setattr(tool, "connection_status", malformed)
        response = await client.post("/feishu/check", headers=BEARER)
        assert response.status_code == 503 and response.json()["error_type"] == "protocol"
        assert raw not in response.text and not response.json()["business_access_verified"]


@pytest.mark.parametrize("is_error", [False, True])
async def test_unknown_scope_count_is_preserved_as_unknown_without_discarding_diagnostics(tmp_path, monkeypatch, is_error):
    status = copy.deepcopy(STATUS)
    status["user"]["scope_count"] = None
    result = ToolResult("not returned", is_error, "configuration" if is_error else None, data=status)
    async with api(tmp_path, monkeypatch, result) as (client, _, _, _, _):
        response = await client.post("/feishu/check", headers=BEARER)
        assert response.status_code == (503 if is_error else 200), response.text
        assert response.json()["user"]["scope_count"] is None
        assert response.json()["app_configured"] is True


@pytest.mark.parametrize("kind", ["success", "error", "malformed"])
async def test_business_verification_is_one_fixed_read_and_never_returns_calendar_data(tmp_path, monkeypatch, kind):
    async with api(tmp_path, monkeypatch) as (client, coordinator, tool, calls, _):
        if kind != "success":
            async def business(arguments, context):
                tool.diagnostic_business_calls.append((arguments, context))
                return ToolResult("private-calendar-title private-event-id", kind == "error", "api" if kind == "error" else None,
                                  data={"error": {"type": "api", "subtype": "missing_scopes", "code": 99991672,
                                                  "message": "private-calendar-title", "hint": "private-event-id"}}
                                  if kind == "error" else {"items": [{"title": "private-calendar-title"}]})

            monkeypatch.setattr(tool, "execute", business)
        before = list(coordinator.store.connection.iterdump())
        response = await client.post("/feishu/check", headers=BEARER, json={"verify_business": True})
        assert response.status_code == (200 if kind == "success" else 503), response.text
        value = response.json()
        assert value["business_access_verified"] is (kind == "success")
        assert value["business_probe"]["attempted"] is True
        assert value["business_probe"]["success"] is (kind == "success")
        assert value["is_error"] is (kind != "success")
        if kind == "error":
            assert value["business_probe"]["error_code"] == 99991672
        assert "private-calendar-title" not in response.text and "private-event-id" not in response.text
        assert "sensitive calendar" not in response.text
        assert len(tool.diagnostic_business_calls) == 1
        arguments, context = tool.diagnostic_business_calls[0]
        assert arguments == {"argv": ["calendar", "+agenda"], "identity": "user"}
        assert context is calls[0]
        assert list(coordinator.store.connection.iterdump()) == before


@pytest.mark.parametrize("value", ["true", 1])
async def test_business_verification_requires_an_explicit_json_boolean(tmp_path, monkeypatch, value):
    async with api(tmp_path, monkeypatch) as (client, _, tool, calls, registry_calls):
        response = await client.post("/feishu/check", headers=BEARER, json={"verify_business": value})
        assert response.status_code == 422
        assert not calls and not registry_calls and not tool.diagnostic_business_calls


@pytest.mark.parametrize("verify_business", [False, True])
async def test_diagnostic_observes_daemon_shutdown_without_creating_a_run(tmp_path, monkeypatch, verify_business):
    async with api(tmp_path, monkeypatch) as (client, coordinator, tool, _, _):
        started = asyncio.Event()
        observed = []

        async def wait_for_shutdown(context):
            started.set()
            await asyncio.wait_for(context.cancel.wait(), 2)
            observed.append(True)
            return ToolResult("cancelled", True, "cancelled", "known", data={"error_type": "cancelled"})

        monkeypatch.setattr(tool, "connection_status", wait_for_shutdown)
        before = list(coordinator.store.connection.iterdump())
        task = asyncio.create_task(client.post("/feishu/check", headers=BEARER, json={"verify_business": verify_business}))
        await asyncio.wait_for(started.wait(), 1)
        coordinator._closed = True
        response = await asyncio.wait_for(task, 2)
        assert observed and response.status_code == 503
        assert not tool.diagnostic_business_calls
        assert list(coordinator.store.connection.iterdump()) == before


@pytest.mark.parametrize("kind,expected", [
    ("present", {"supported": True, "key_exists": True, "value_count": 3}),
    ("missing", {"supported": True, "key_exists": False, "value_count": 0}),
    ("denied", {"supported": True, "key_exists": None, "value_count": None}),
])
def test_windows_registry_uses_only_key_metadata_without_credential_reads(monkeypatch, kind, expected):
    events = []

    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def open_key(root, path, reserved, access):
        events.append((root, path, reserved, access))
        if kind == "missing":
            raise FileNotFoundError
        if kind == "denied":
            raise PermissionError
        return Key()

    # No EnumValue, QueryValueEx or decrypt method exists on this stand-in.
    fake = SimpleNamespace(HKEY_CURRENT_USER="test-current-user", KEY_QUERY_VALUE=1,
                           OpenKey=open_key, QueryInfoKey=lambda key: (0, 3, 0))
    monkeypatch.setitem(sys.modules, "winreg", fake)
    monkeypatch.setattr(feishu_cli, "sys", SimpleNamespace(platform="win32"))
    assert feishu_api._windows_registry_metadata() == expected
    assert events == [("test-current-user", r"Software\LarkCli\keychain\lark-cli", 0, 1)]


def test_registry_diagnostic_is_unsupported_without_import_or_reads_on_other_platforms(monkeypatch):
    monkeypatch.setattr(feishu_cli, "sys", SimpleNamespace(platform="linux"))
    assert feishu_api._windows_registry_metadata() == {
        "supported": False, "key_exists": None, "value_count": None,
    }
