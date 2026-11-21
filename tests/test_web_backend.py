import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest

from aster.api import create_app
from aster.api.web import COOKIE_NAME, UIAuth
from aster.domain import ModelTurn
from aster.engine import FakeModel
from aster.service import RunCoordinator

ORIGIN = "http://127.0.0.1:8765"
TOKEN = "test-workbench-api-token-with-at-least-thirty-two-characters"
UI_HEADERS = {"X-Aster-UI": "1", "Origin": ORIGIN}


class EmptyCredentials:
    def get(self, *args, **kwargs):
        return None

    def resolve(self, reference):
        raise ValueError("No credentials in the workbench test")


@asynccontextmanager
async def workbench(tmp_path, *, authentication=None, model=None):
    coordinator = RunCoordinator(tmp_path / "state", tools=[], credentials=EmptyCredentials(),
        model_factory=(lambda _: model) if model else None)
    app = create_app(coordinator, token=TOKEN, ui_auth=authentication)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=ORIGIN,
    ) as client:
        yield client, coordinator, app


async def mint(client, **values):
    response = await client.post("/ui/tickets", headers={"Authorization": "Bearer " + TOKEN}, json=values)
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()["ticket"]


async def login(client, **values):
    ticket = await mint(client, **values)
    result = await client.post("/ui/auth", headers=UI_HEADERS, json={"ticket": ticket})
    assert result.status_code == 200, result.text
    return ticket, result


async def test_runtime_only_serves_api_and_advertises_the_desktop_workbench(tmp_path):
    async with workbench(tmp_path) as (client, _, _):
        response = await client.get("/health")
        assert response.status_code == 200 and response.json()["status"] == "ok"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-frame-options"] == "DENY"
        assert "script-src 'self'" in response.headers["content-security-policy"]
        assert (await client.head("/health")).status_code == 200
        bearer = {"Authorization": "Bearer " + TOKEN}
        capabilities = await client.get("/capabilities", headers=bearer)
        for result in (response, capabilities):
            assert "desktop_workbench_v1" in result.json()["capabilities"]
            assert "web_workbench_v1" not in result.json()["capabilities"]
        for path in ("/", "/assets/app.js", "/assets/app.css", "/assets/%2e%2e/index.html", "/api/unknown"):
            for method in ("GET", "HEAD"):
                public = await client.request(method, path)
                authorized = await client.request(method, path, headers=bearer)
                assert public.status_code == 401 and authorized.status_code == 404
                assert authorized.headers["content-type"].startswith("application/json")
        assert (await client.get("/ui/bootstrap")).status_code == 401
        assert (await client.get("/sessions")).status_code == 401


async def test_ticket_is_single_use_short_lived_and_never_contains_runtime_token(tmp_path):
    now = [10.0]
    authentication = UIAuth(clock=lambda: now[0], ticket_ttl=5, session_ttl=20)
    async with workbench(tmp_path, authentication=authentication) as (client, _, _):
        ticket = await mint(client)
        assert TOKEN not in ticket and ticket not in json.dumps(authentication._tickets, default=str)
        results = await asyncio.gather(*[
            client.post("/ui/auth", headers=UI_HEADERS, json={"ticket": ticket}) for _ in range(2)
        ])
        assert sorted(result.status_code for result in results) == [200, 401]
        good = next(result for result in results if result.status_code == 200)
        cookie = good.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Max-Age=20" in cookie
        assert "Domain=" not in cookie and TOKEN not in cookie and ticket not in cookie
        expired = await mint(client)
        now[0] += 5
        assert (await client.post("/ui/auth", headers=UI_HEADERS, json={"ticket": expired})).status_code == 401
        now[0] += 20
        assert (await client.get("/ui/bootstrap", headers=UI_HEADERS)).status_code == 401


async def test_exchange_origin_and_header_required_before_consuming_ticket(tmp_path):
    async with workbench(tmp_path) as (client, _, _):
        ticket = await mint(client)
        for headers in ({}, {"Origin": ORIGIN}, {"X-Aster-UI": "1"},
                        {"X-Aster-UI": "1", "Origin": "http://localhost:8765"},
                        {"X-Aster-UI": "1", "Origin": "https://attacker.invalid"}):
            result = await client.post("/ui/auth", headers=headers, json={"ticket": ticket})
            assert result.status_code == 403
        duplicated = [("X-Aster-UI", "1"), ("Origin", ORIGIN), ("Origin", "https://attacker.invalid")]
        assert (await client.post("/ui/auth", headers=duplicated, json={"ticket": ticket})).status_code == 403
        assert (await client.post("/ui/auth", headers=UI_HEADERS, json={"ticket": ticket})).status_code == 200
        assert (await client.get("/health", headers={"Host": "attacker.invalid"})).status_code == 403
        assert (await client.get("/", headers={"Origin": "null"})).status_code == 403


async def test_cookie_csrf_covers_reads_writes_logout_and_ticket_mint(tmp_path):
    async with workbench(tmp_path) as (client, _, _):
        await login(client)
        assert (await client.get("/sessions")).status_code == 403
        assert (await client.get("/sessions", headers={"X-Aster-UI": "1"})).status_code == 200
        for headers in ({}, {"X-Aster-UI": "1"}, {"Origin": ORIGIN},
                        {"X-Aster-UI": "1", "Origin": "http://localhost:8765"}):
            assert (await client.post("/sessions", headers=headers, json={})).status_code == 403
            assert (await client.post("/ui/logout", headers=headers)).status_code == 403
        assert (await client.post("/ui/tickets", headers=UI_HEADERS, json={})).status_code == 403
        created = await client.post("/sessions", headers=UI_HEADERS, json={"cwd": str(tmp_path)})
        assert created.status_code == 201
        # Wrong explicit bearer credentials never fall through to a valid cookie.
        assert (await client.get("/sessions", headers={**UI_HEADERS, "Authorization": "Bearer wrong"})).status_code == 401
        raw_cookie = client.cookies.get(COOKIE_NAME)
        logged_out = await client.post("/ui/logout", headers=UI_HEADERS)
        assert logged_out.status_code == 200 and logged_out.json() == {"authenticated": False}
        assert "Max-Age=0" in logged_out.headers["set-cookie"]
        assert (await client.get("/sessions", headers={**UI_HEADERS, "Cookie": COOKIE_NAME + "=" + raw_cookie})).status_code == 401
        # CLI bearer control remains independent of browser CSRF requirements.
        assert (await client.post("/sessions", headers={"Authorization": "Bearer " + TOKEN}, json={"cwd": str(tmp_path)})).status_code == 201


async def test_bootstrap_whitelist_no_key_and_workspace_from_launcher_ticket(tmp_path):
    async with workbench(tmp_path) as (client, coordinator, _):
        (coordinator.data_dir / "config.yaml").write_text("model: deepseek-flash\nprivate_note: must-not-leak\n")
        _, exchanged = await login(client, cwd=str(tmp_path))
        assert exchanged.json()["expires_in"] == 28800
        response = await client.get("/ui/bootstrap", headers=UI_HEADERS)
        assert response.status_code == 200
        body = response.json()
        assert set(body) == {"cwd", "model", "provider_configured", "instance_id", "schema_version", "ui_version", "providers", "protocols", "selection", "capabilities"}
        assert body["capabilities"] == ["session_permanent_delete_v1", "project_permanent_delete_v1"]
        assert {key: body[key] for key in ("cwd", "model", "provider_configured", "instance_id", "schema_version", "ui_version")} == {
            "cwd": str(tmp_path.resolve()), "model": "deepseek-flash", "provider_configured": False,
            "instance_id": coordinator.instance_id, "schema_version": 1, "ui_version": 1}
        assert body["selection"]["provider_id"] == "deepseek"
        assert all("api_key" not in profile for profile in body["providers"])
        assert "must-not-leak" not in response.text and TOKEN not in response.text
        assert coordinator.store.list_runs() == []
        assert response.headers["cache-control"] == "no-store"


async def test_cookie_origin_binding_and_restart_revoke_grants(tmp_path):
    authentication = UIAuth()
    async with workbench(tmp_path, authentication=authentication) as (client, _, _):
        await login(client)
        raw = client.cookies.get(COOKIE_NAME)
        wrong_host = await client.get("http://localhost:8765/ui/bootstrap", headers={
            "X-Aster-UI": "1", "Cookie": COOKIE_NAME + "=" + raw,
        })
        assert wrong_host.status_code == 401
    assert authentication.authenticate(raw, ORIGIN) is None


async def test_browser_cookie_runs_real_coordinator_and_fetch_sse(tmp_path):
    model = FakeModel([ModelTurn("actual runtime answer")])
    async with workbench(tmp_path, model=model) as (client, coordinator, _):
        await login(client)
        sid = (await client.post("/sessions", headers=UI_HEADERS, json={"cwd": str(tmp_path)})).json()["id"]
        response = await client.post(f"/sessions/{sid}/runs", headers=UI_HEADERS, json={"text": "hello"})
        assert response.status_code == 202
        rid = response.json()["id"]
        result = await coordinator.wait(rid, timeout=3)
        assert result.status == "completed", result.error
        events = await client.get(f"/runs/{rid}/events", headers={"X-Aster-UI": "1"})
        assert events.status_code == 200 and "event: run.finished" in events.text
        assert "actual runtime answer" in events.text
        assert len(model.calls) == 1


async def test_session_title_patch_is_scoped_metadata_and_persists_after_restart(tmp_path):
    from aster.state import Store

    async with workbench(tmp_path) as (client, coordinator, _):
        await login(client)
        original = (await client.post("/sessions", headers=UI_HEADERS, json={"cwd": str(tmp_path)})).json()
        sid = original["id"]
        assert (await client.patch(f"/sessions/{sid}", json={"title": "not authorized"})).status_code == 403
        changed = await client.patch(f"/sessions/{sid}", headers=UI_HEADERS, json={"title": "  第一项任务  "})
        assert changed.status_code == 200 and changed.json()["title"] == "第一项任务"
        assert {key: changed.json()[key] for key in ("id", "cwd", "agent_id", "project_id", "created_at")} == {
            key: original[key] for key in ("id", "cwd", "agent_id", "project_id", "created_at")}
        assert coordinator.store.events(session_id=sid)[-1]["type"] == "session.updated"
        assert (await client.patch("/sessions/missing", headers=UI_HEADERS, json={"title": "missing"})).status_code == 404
        assert (await client.patch(f"/sessions/{sid}", headers=UI_HEADERS, json={"title": "x" * 201})).status_code == 422
        assert (await client.patch(f"/sessions/{sid}", headers=UI_HEADERS, json={"title": "x", "cwd": str(tmp_path)})).status_code == 422
    store = Store(tmp_path / "state/runtime.db")
    try:
        assert store.get_session(sid)["title"] == "第一项任务"
        assert store.list_runs() == []
    finally:
        store.close()


def test_auth_memory_caps_do_not_revoke_another_active_login():
    authentication = UIAuth(max_tickets=1, max_sessions=1)
    ticket = authentication.mint(ORIGIN)
    with pytest.raises(Exception, match="429"):
        authentication.mint(ORIGIN)
    credential = authentication.redeem(ticket, ORIGIN)
    next_ticket = authentication.mint(ORIGIN)
    with pytest.raises(Exception, match="429"):
        authentication.redeem(next_ticket, ORIGIN)
    assert authentication.authenticate(credential, ORIGIN)
    authentication.revoke(credential)
    assert authentication.redeem(next_ticket, ORIGIN)
