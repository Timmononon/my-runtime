import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from aster.api import create_app, load_api_token
from aster.api.web import COOKIE_NAME, UIAuth
from aster.cli import main
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
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text('<!doctype html><html><script type="module" src="/assets/app-a1.js"></script></html>')
    (static / "assets" / "app-a1.js").write_text('document.body.dataset.ready="yes";')
    (static / "assets" / "app-a1.css").write_text("body{background:white}")
    (static / "assets" / "app-a1.js.map").write_text("source code must not be served")
    (static / "assets" / ".private.js").write_text("must not be served")
    coordinator = RunCoordinator(tmp_path / "state", tools=[], credentials=EmptyCredentials(),
        model_factory=(lambda _: model) if model else None)
    app = create_app(coordinator, token=TOKEN, static_root=static, ui_auth=authentication)
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


async def test_public_assets_are_bounded_and_api_never_falls_back_to_index(tmp_path):
    async with workbench(tmp_path) as (client, _, _):
        response = await client.get("/")
        assert response.status_code == 200 and "doctype html" in response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-frame-options"] == "DENY"
        assert "script-src 'self'" in response.headers["content-security-policy"]
        assert TOKEN not in response.text
        assert (await client.head("/")).content == b""
        assert (await client.head("/health")).status_code == 200
        for name in ("app-a1.js", "app-a1.css"):
            asset = await client.get("/assets/" + name)
            assert asset.status_code == 200
            assert asset.headers["cache-control"] == "public, max-age=3600"
        for path in ("/assets/app-a1.js.map", "/assets/.private.js", "/assets/%2e%2e/index.html", "/assets/missing.js"):
            response = await client.get(path)
            assert response.status_code == 404 and "doctype html" not in response.text
        for path in ("/sessions", "/sessions/typo", "/ui/bootstrap", "/webanything", "/api/unknown"):
            response = await client.get(path)
            assert response.status_code == 401 and "doctype html" not in response.text
        assert (await client.post("/assets/app-a1.js")).status_code == 401
        assert (await client.post("/")).status_code == 401


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


@pytest.mark.parametrize("no_browser,reused", [(True, False), (False, True)])
def test_web_launcher_start_reuse_ticket_fragment_without_secret_output(tmp_path, monkeypatch, capsys, no_browser, reused):
    status = {"host": "127.0.0.1", "port": 8799, "instance_id": "fixture-instance", "already_running": reused}
    started, requests, opened = [], [], []
    ticket = "one-time-login-ticket-" + "x" * 32
    def start(data_dir, argv, **kwargs):
        started.append((data_dir, argv, kwargs))
        return status
    def handle(request):
        requests.append(request)
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok", "instance_id": "fixture-instance"})
        if request.url.path == "/":
            return httpx.Response(200, text="<html>built app</html>")
        assert request.url.path == "/ui/tickets"
        assert request.headers["authorization"] == "Bearer " + TOKEN
        assert json.loads(request.content)["cwd"] == str(tmp_path.resolve())
        return httpx.Response(201, json={"ticket": ticket, "expires_in": 60})
    client = httpx.Client
    monkeypatch.setattr("aster.maintenance.start_service", start)
    monkeypatch.setattr("aster.cli.load_api_token", lambda _: TOKEN)
    monkeypatch.setattr("aster.cli.httpx.Client", lambda **kwargs: client(transport=httpx.MockTransport(handle), **kwargs))
    monkeypatch.setattr("aster.cli.webbrowser.open", lambda url, **kwargs: opened.append(url) or True)
    argv = ["--data-dir", str(tmp_path / "data"), "web", "--cwd", str(tmp_path), "--port", "8799"]
    assert main(argv + (["--no-browser"] if no_browser else [])) == 0
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["started"] == (not reused) and result["reused"] == reused
    assert result["browser_opened"] == (not no_browser)
    assert "#" not in result["url"] and TOKEN not in output.out + output.err and ticket not in output.out + output.err
    assert len(requests) == (2 if no_browser else 3)
    assert opened == ([] if no_browser else ["http://127.0.0.1:8799/#ticket=" + ticket])
    assert "daemon" in started[0][1] and "serve" in started[0][1]
    assert not (tmp_path / "data" / "runtime.db").exists()


def test_web_launcher_refuses_wrong_health_instance_before_reading_credentials(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("aster.maintenance.start_service", lambda *args, **kwargs: {
        "host": "127.0.0.1", "port": 8799, "instance_id": "owned", "already_running": True})
    def no_token(*args):
        raise AssertionError("must not read credentials for a different service")
    monkeypatch.setattr("aster.cli.load_api_token", no_token)
    client = httpx.Client
    monkeypatch.setattr("aster.cli.httpx.Client", lambda **kwargs: client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"instance_id": "other"})), **kwargs))
    assert main(["--data-dir", str(tmp_path), "web", "--cwd", str(tmp_path)]) == 2
    assert "does not match" in capsys.readouterr().err


@pytest.mark.windows
@pytest.mark.skipif(os.name != "nt", reason="Windows wrapper and actual daemon integration")
def test_windows_web_wrapper_starts_reuses_real_daemon_without_browser(tmp_path):
    from aster.maintenance import service_status, stop_service

    root = Path(__file__).resolve().parents[1]
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if not powershell:
        pytest.skip("PowerShell is not installed")
    assert (root / "src/aster/web/static/index.html").is_file(), "Build the bundled frontend before integration tests"
    data, workspace = tmp_path / "网页 数据", tmp_path / "工作 空间"
    workspace.mkdir()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    command = [powershell, "-NoProfile", "-NonInteractive", "-File", str(root / "scripts/Open-Aster.ps1"),
        "-DataDir", str(data), "-PythonExe", sys.executable, "-Cwd", str(workspace), "-Port", str(port), "-NoBrowser"]
    try:
        for reused in (False, True):
            process = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=35,
                                     creationflags=0x08000000, check=False)
            assert process.returncode == 0, process.stderr
            result = json.loads(process.stdout)
            assert result["reused"] is reused and result["started"] is not reused
            assert result["browser_opened"] is False and "#" not in result["url"]
            assert result["cwd"] == str(workspace.resolve())
        response = httpx.get(f"http://127.0.0.1:{port}/", trust_env=False)
        assert response.status_code == 200 and "text/html" in response.headers["content-type"]
        assert "no-store" in response.headers["cache-control"]
        local_token = load_api_token(data)
        sessions = httpx.get(f"http://127.0.0.1:{port}/sessions", trust_env=False,
                            headers={"Authorization": "Bearer " + local_token})
        assert sessions.status_code == 200 and sessions.json()["sessions"] == []
        for filename in ("service.stdout.log", "service.stderr.log"):
            contents = (data / filename).read_text(encoding="utf-8")
            assert "#ticket=" not in contents and local_token not in contents
    finally:
        if service_status(data)["running"]:
            assert stop_service(data, timeout=10)["stopped"]
