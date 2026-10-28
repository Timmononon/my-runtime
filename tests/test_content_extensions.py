import asyncio
import base64
import io
import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from PIL import Image, ImageDraw

from aster.domain import Message, ToolContext
from aster.maintenance import backup_bundle, prune_orphan_artifacts, restore_bundle, validate_database
from aster.platform.filesystem import digest
from aster.platform.processes import ProcessSupervisor
from aster.providers.deepseek import encode_messages
from aster.service.attachments import AttachmentService
from aster.service.previews import PreviewManager
from aster.state import Store
from aster.tools.browser import BrowserManager, build_browser_tools
from aster.tools.deliverables import DeliverableTool
from aster.tools.files import build_file_tools
from aster.tools.http import HTTPTool
from aster.tools.images import ImageTool


def save_evidence(name, source):
    root = os.environ.get("ASTER_CONTENT_EVIDENCE")
    if root:
        target = Path(root)
        target.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target / name)


def png_bytes():
    image = Image.new("RGB", (300, 150), "white")
    ImageDraw.Draw(image).rectangle((10, 10, 140, 140), fill="red")
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def setup_store(tmp_path):
    data, cwd = tmp_path / "state", tmp_path / "workspace"
    cwd.mkdir(parents=True)
    store = Store(data / "runtime.db")
    session = store.create_session(cwd)
    attachments = AttachmentService(store, data)
    return data, cwd, store, session, attachments


def test_attachment_scope_branch_restore_wire_and_gc(tmp_path):
    from functools import partial

    from aster.maintenance import resolve_artifact
    data, cwd, store, session, attachments = setup_store(tmp_path)
    record = attachments.register_bytes(session["id"], "图像.png", png_bytes(), "image/png")
    message = attachments.resolve_message(session["id"], "Describe the red region", [record["attachment_id"]])
    store.append_message(session["id"], None, message)
    store.append_message(session["id"], None, Message("assistant", "Ready"))
    child = store.fork_session(session["id"])
    unrelated = store.create_session(cwd)
    assert attachments.resolve(child["id"], record["attachment_id"])[0].is_file()
    with pytest.raises(PermissionError):
        attachments.resolve(unrelated["id"], record["attachment_id"])
    later = attachments.register_bytes(session["id"], "later.txt", b"future attachment")
    with pytest.raises(PermissionError):
        attachments.resolve(child["id"], later["attachment_id"])
    wire = encode_messages([message], data / "artifacts", partial(resolve_artifact, data))
    image = wire[0]["content"][1]
    assert base64.b64decode(image["image_url"].split(",", 1)[1]) == png_bytes()
    store.close()
    report = prune_orphan_artifacts(data, 0, dry_run=True)
    assert not any(record["attachment_id"] in json.dumps(item) for item in report["candidates"])
    package = backup_bundle(data, tmp_path / "backup.zip")
    restored = tmp_path / "恢复 State"
    restore_bundle(package, restored)
    new_store = Store(restored / "runtime.db")
    try:
        service = AttachmentService(new_store, restored)
        path, _ = service.resolve(child["id"], record["attachment_id"])
        assert path.is_relative_to(restored) and path.read_bytes() == png_bytes()
        inherited = service.resolve_message(child["id"], "Continue", [record["attachment_id"]])
        encoded = encode_messages([inherited], restored / "artifacts", partial(resolve_artifact, restored))
        assert encoded[0]["content"][1]["image_url"].startswith("data:image/png;base64,")
    finally:
        new_store.close()


@pytest.mark.parametrize("filename,content,mime", [
    ("../escape.txt", b"x", "text/plain"), ("CON.txt", b"x", "text/plain"),
    ("photo.jpg", png_bytes(), "image/jpeg"), ("photo.png", png_bytes(), "text/html"),
    ("fake.pdf", b"hello", "application/pdf"), ("binary.txt", b"hello\0world", "text/plain"),
    ("archive.docx", b"PK\x03\x04bad", None), ("empty.txt", b"", None),
])
def test_attachment_validation_rejects_spoofed_or_unsafe_content(tmp_path, filename, content, mime):
    _, _, store, session, service = setup_store(tmp_path)
    try:
        with pytest.raises((ValueError, OSError, KeyError)):
            service.register_bytes(session["id"], filename, content, mime)
        assert not service.list(session["id"])
    finally:
        store.close()


async def test_attachment_exact_read_grant_never_grants_write(tmp_path):
    data, cwd, store, session, service = setup_store(tmp_path)
    try:
        record = service.register_bytes(session["id"], "code.py", b"print('source data')\n")
        ctx = ToolContext(session["id"], "run", cwd, data / "artifacts" / "run", asyncio.Event(),
                          services={"attachment_service": service})
        tools = {tool.spec.name: tool for tool in build_file_tools()}
        read = await tools["file_read"].execute({"path": record["path"]}, ctx)
        assert not read.is_error and "source data" in read.content
        changed = await tools["file_edit"].execute({"path": record["path"], "expected_hash": record["sha256"],
                                                     "old_text": "source", "new_text": "injected"}, ctx)
        assert changed.is_error and changed.error_category == "permission"
        msg = service.resolve_message(session["id"], "Review", [record["attachment_id"]])
        assert "untrusted source data" in msg.content and "<attachment-data-" in msg.content
        Path(record["path"]).write_bytes(b"changed")
        with pytest.raises(ValueError, match="changed"):
            service.resolve(session["id"], record["attachment_id"])
    finally:
        store.close()


@pytest.mark.windows
async def test_real_windows_pdf_pages_and_image_wire(tmp_path):
    source = tmp_path / "彩色 pages.pdf"
    first = Image.new("RGB", (360, 240), "white")
    ImageDraw.Draw(first).rectangle((30, 30, 250, 160), fill="red")
    second = Image.new("RGB", (240, 360), "blue")
    first.save(source, "PDF", save_all=True, append_images=[second])
    image_path = tmp_path / "图像.png"
    image_path.write_bytes(png_bytes())
    context = ToolContext("session", "run", tmp_path, tmp_path / "artifacts", asyncio.Event())
    supervisor = ProcessSupervisor()
    try:
        result = await ImageTool(supervisor, pdf=True).execute({"path": str(source), "pages": "1-2", "max_pages": 2}, context)
        assert not result.is_error, result.content
        assert result.data["total_pages"] == 2 and [x["page"] for x in result.artifacts] == [1, 2]
        save_evidence("pdf-page-1.jpg", result.artifacts[0]["path"])
        for record in result.artifacts:
            with Image.open(record["path"]) as image:
                assert image.width <= 1600 and image.height <= 1600
                red, _green, blue = image.getpixel((image.width // 2, image.height // 2))
                assert (red > 180 and blue < 80) if record["page"] == 1 else (blue > 180 and red < 80)
        image_result = await ImageTool(supervisor).execute({"path": str(image_path), "max_dimension": 256}, context)
        assert not image_result.is_error, image_result.content
        message = Message("user", "Inspect", metadata={"content_parts": image_result.data["content_parts"], "artifacts": image_result.artifacts})
        encoded = encode_messages([message], context.artifact_dir)
        assert encoded[0]["content"][1]["type"] == "input_image"
        with Image.open(io.BytesIO(base64.b64decode(encoded[0]["content"][1]["image_url"].split(",", 1)[1]))) as image:
            assert image.width == 256
        for args in ({"path": str(source), "pages": "3"}, {"path": str(source), "pages": "1-11"},
                     {"path": str(source), "pages": "2-1"}):
            assert (await ImageTool(supervisor, pdf=True).execute(args, context)).is_error
        broken = tmp_path / "broken.pdf"
        broken.write_bytes(b"%PDF-1.7\nnot a valid PDF")
        assert (await ImageTool(supervisor, pdf=True).execute({"path": str(broken)}, context)).is_error
    finally:
        await supervisor.close()


class Handler(BaseHTTPRequestHandler):
    calls: list = []  # noqa: RUF012 - reset per fixture; shared request evidence is intentional.

    def do_GET(self):
        type(self).calls.append((self.path, self.headers.get("Authorization")))
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/echo")
            self.end_headers()
            return
        if self.path == "/slow":
            time.sleep(0.4)
        self.send_response(200)
        self.send_header("Content-Type", "text/html" if self.path == "/page" else "text/plain")
        self.end_headers()
        content = (b"<title>Evidence</title><h1>ORCHID 7318</h1><script>secret script text</script><a href='/next'>Next</a>"
                   if self.path == "/page" else (self.headers.get("Authorization") or "no-auth").encode()
                   if self.path == "/echo" else b"x" * 10000)
        try:
            self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        type(self).calls.append(("POST " + self.path, self.headers.get("Authorization")))
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path == "/slow-write":
            time.sleep(0.4)
        if self.path == "/disconnect":
            self.connection.shutdown(2)
            self.connection.close()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        try:
            self.wfile.write(b'{"created":true}')
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):
        pass


@pytest.fixture
def http_server():
    Handler.calls = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    thread.join()


async def test_http_extract_limits_credentials_redirect_and_cancel(tmp_path, http_server):
    ctx = ToolContext("s", "r", tmp_path, tmp_path / "artifacts", asyncio.Event(),
                      services={"http_allowed_hosts": ["127.0.0.1", "localhost"],
                                "http_credential_origins": {"env:TEST": [http_server, http_server.replace("127.0.0.1", "localhost")]},
                                "credential_resolver": lambda ref: "fixture-secret-never-log"})
    tool = HTTPTool(fetch=True)
    result = await tool.execute({"url": http_server + "/page"}, ctx)
    assert not result.is_error, result.content
    assert "ORCHID 7318" in result.content and "secret script text" not in result.content
    assert result.data["title"] == "Evidence" and result.data["links"] == [http_server + "/next"]
    assert Path(result.artifacts[-1]["path"]).read_bytes().startswith(b"<title>")
    denied = await HTTPTool(fetch=True).execute({"url": http_server}, ToolContext("s", "r", tmp_path, ctx.artifact_dir, asyncio.Event()))
    assert denied.is_error and denied.error_category == "permission"
    oversize = await tool.execute({"url": http_server, "max_bytes": 10}, ctx)
    assert oversize.is_error and "max_bytes" in oversize.content
    secret = await tool.execute({"url": http_server + "/echo", "credential_headers": {"Authorization": "env:TEST"}}, ctx)
    assert not secret.is_error and "[REDACTED]" in secret.content
    assert "fixture-secret-never-log" not in json.dumps(secret.data)
    assert b"fixture-secret-never-log" not in Path(secret.artifacts[-1]["path"]).read_bytes()
    redirected = await tool.execute({"url": http_server.replace("127.0.0.1", "localhost") + "/redirect",
                                      "credential_headers": {"Authorization": "env:TEST"}}, ctx)
    assert not redirected.is_error and "no-auth" in redirected.content
    assert Handler.calls[-1] == ("/echo", None)
    task = asyncio.create_task(tool.execute({"url": http_server + "/slow"}, ctx))
    await asyncio.sleep(0.05)
    ctx.cancel.set()
    cancelled = await task
    assert cancelled.is_error and cancelled.error_category == "cancelled" and cancelled.outcome != "unknown"


async def test_http_write_unknown_is_not_replayed_and_classification(tmp_path, http_server):
    ctx = ToolContext("s", "r", tmp_path, tmp_path / "artifacts", asyncio.Event(), services={"http_allowed_hosts": ["127.0.0.1"]})
    tool = HTTPTool()
    assert tool.spec.risk == "write" and tool.classify({"method": "GET"}).risk == "read"
    assert tool.classify({"method": "POST"}).risk == "write"
    assert tool.classify({"method": "GET", "credential_headers": {"Authorization": "env:TEST"}}).risk == "external"
    result = await tool.execute({"url": http_server + "/disconnect", "method": "POST", "body": "action"}, ctx)
    assert result.is_error and result.outcome == "unknown"
    assert [x[0] for x in Handler.calls].count("POST /disconnect") == 1
    unsafe = await tool.execute({"url": http_server, "headers": {"Authorization": "inline-secret"}}, ctx)
    assert unsafe.is_error and "inline-secret" not in unsafe.content
    request = asyncio.create_task(tool.execute({"url": http_server + "/slow-write", "method": "POST"}, ctx))
    for _ in range(200):
        if any(item[0] == "POST /slow-write" for item in Handler.calls):
            break
        await asyncio.sleep(0.01)
    ctx.cancel.set()
    timed = await request
    assert timed.outcome == "unknown" and timed.error_category == "cancelled"
    assert [item[0] for item in Handler.calls].count("POST /slow-write") == 1


@pytest.mark.browser
async def test_deliverable_preview_browser_csp_scope_and_close(tmp_path):
    preview, browser = PreviewManager(), BrowserManager(tmp_path / "profiles")
    source = tmp_path / "entry.html"
    source.write_text("<h1>Local artifact</h1><button onclick=\"document.querySelector('h1').textContent='Clicked'\">Go</button>", encoding="utf-8")
    ctx = ToolContext("s", "r", tmp_path, tmp_path / "artifacts", asyncio.Event(), services={"preview_manager": preview})
    tools = {tool.spec.name: tool for tool in build_browser_tools(browser)}
    try:
        shown = await DeliverableTool().execute({"path": str(source), "interactive": True}, ctx)
        assert not shown.is_error, shown.content
        url = shown.data["preview_url"]
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.get(url)
            assert response.status_code == 200 and "data-aster-preview" in response.text
            assert "connect-src 'none'" in response.headers["content-security-policy"]
            assert (await client.get(url.rsplit("/", 1)[0] + "/other.txt")).status_code == 404
            assert (await client.get(url, headers={"Host": "evil.invalid"})).status_code == 403
        opened = await tools["browser_open"].execute({"url": Path(shown.artifacts[0]["path"]).as_uri()}, ctx)
        assert not opened.is_error, opened.content
        page_id = opened.data["pages"][0]["page_id"]
        clicked = await tools["browser_click"].execute({"page_id": page_id, "selector": "button"}, ctx)
        assert not clicked.is_error, clicked.content
        state = await tools["browser_snapshot"].execute({"page_id": page_id}, ctx)
        assert "Clicked" in state.content
        other = ToolContext("other", "r2", tmp_path, ctx.artifact_dir, asyncio.Event(), services=ctx.services)
        denied = await tools["browser_open"].execute({"url": Path(shown.artifacts[0]["path"]).as_uri()}, other)
        assert denied.is_error and denied.error_category == "permission"
        preview.close_session("s")
        async with httpx.AsyncClient(trust_env=False) as client:
            assert (await client.get(url)).status_code == 404
    finally:
        await browser.close()
        await preview.close()


@pytest.mark.browser
async def test_preview_external_assets_csp_blocks_network_and_filters_private_files(tmp_path):
    preview, browser = PreviewManager(), BrowserManager(tmp_path / "profiles")
    site = tmp_path / "site"
    site.mkdir()
    (site / "assets").mkdir()
    (site / "assets" / "script.js").write_text("document.querySelector('h1').textContent='External JS ready';", encoding="utf-8")
    (site / "assets" / "style.css").write_text("h1 { color: rgb(1, 91, 173); }", encoding="utf-8")
    (site / ".env.json").write_text('{"token":"synthetic-never-publish"}')
    (site / "Private").mkdir()
    (site / "Private" / "secret.json").write_text('{"private":true}')
    (site / "credentials.json").write_text('{"must":"not-publish"}')
    (site / "secret.js").write_text('const secret="must-not-publish"')
    (site / "notes.txt").write_text("not a browser asset")
    source = site / "index.html"
    source.write_text('<!doctype html><link rel="stylesheet" href="assets/style.css"><h1>Loading</h1>'
                      '<script src="assets/script.js"></script>', encoding="utf-8")
    ctx = ToolContext("asset-session", "r", tmp_path, tmp_path / "artifacts", asyncio.Event(), services={"preview_manager": preview})
    tools = {tool.spec.name: tool for tool in build_browser_tools(browser)}
    try:
        shown = await DeliverableTool().execute({"path": str(source), "include_assets": True, "interactive": True}, ctx)
        assert not shown.is_error, shown.content
        assert len(shown.artifacts) == 3
        opened = await tools["browser_open"].execute({"url": shown.data["preview_url"]}, ctx)
        assert not opened.is_error, opened.content
        page_id = opened.data["pages"][0]["page_id"]
        page = browser._pages[page_id].page
        frame = page.frame_locator("iframe[data-aster-preview]")
        assert await frame.locator("h1").inner_text() == "External JS ready"
        assert await frame.locator("h1").evaluate("e => getComputedStyle(e).color") == "rgb(1, 91, 173)"
        blocked = await frame.locator("body").evaluate("async () => { try { await fetch('http://127.0.0.1:9/no-api-token'); return false; } catch(e) {return true;} }")
        assert blocked
        screenshot = await tools["browser_screenshot"].execute({"page_id": page_id}, ctx)
        assert not screenshot.is_error
        save_evidence("html-external-assets.png", screenshot.artifacts[0]["path"])
        base = shown.data["preview_url"].rsplit("/", 1)[0]
        async with httpx.AsyncClient(trust_env=False) as client:
            for relative in (".env.json", "Private/secret.json", "credentials.json", "secret.js", "notes.txt", "%2e%2e/secret.txt", "assets/"):
                assert (await client.get(base + "/" + relative)).status_code == 404
    finally:
        await browser.close()
        await preview.close()


async def test_upload_run_branch_sidechat_restore_and_preview_reissue(tmp_path):
    from functools import partial

    from aster.api import create_app
    from aster.domain import ModelTurn, ToolCall
    from aster.engine import FakeModel
    from aster.maintenance import resolve_artifact
    from aster.service import RunCoordinator

    data, workspace = tmp_path / "runtime", tmp_path / "project"
    workspace.mkdir()
    (workspace / "report.html").write_text("<h1>Persisted deliverable</h1>", encoding="utf-8")
    observed = []

    def verify_wire(messages, specs):
        wire = encode_messages(messages, data / "artifacts", partial(resolve_artifact, data))
        images = [part for item in wire if item.get("role") == "user" and isinstance(item.get("content"), list)
                  for part in item["content"] if part.get("type") == "input_image"]
        assert images and base64.b64decode(images[-1]["image_url"].split(",", 1)[1]) == png_bytes()
        observed.append(True)
        return ModelTurn(tool_calls=[ToolCall("deliver", "present_deliverable", {"path": "report.html"})])

    model = FakeModel([verify_wire, ModelTurn("Delivered")])
    coordinator = RunCoordinator(data, model_factory=lambda cfg: model, browser_enabled=False)
    token = "fixture-local-bearer-token-at-least-32"
    app = create_app(coordinator, token=token)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
        base_url="http://127.0.0.1:8765", headers={"Authorization": "Bearer " + token}) as client:
        sid = (await client.post("/sessions", json={"cwd": str(workspace)})).json()["id"]
        other = (await client.post("/sessions", json={"cwd": str(workspace)})).json()["id"]
        rejected = await client.post(f"/sessions/{sid}/attachments?filename=spoof.pdf", content=b"not pdf", headers={"Content-Type": "application/pdf"})
        assert rejected.status_code == 422
        uploaded = await client.post(f"/sessions/{sid}/attachments?filename=figure.png", content=png_bytes(), headers={"Content-Type": "image/png"})
        assert uploaded.status_code == 201, uploaded.text
        attachment = uploaded.json()
        run = await client.post(f"/sessions/{sid}/runs", json={"text": "Read the image and present report.html", "attachments": [attachment["attachment_id"]]})
        assert run.status_code == 202, run.text
        result = await coordinator.wait(run.json()["id"], timeout=10)
        assert result.status == "completed" and observed == [True], result
        delivery = (await client.get(f"/sessions/{sid}/deliverables")).json()["deliverables"][0]
        asset_id = delivery["artifact_id"]
        assert (await client.get(f"/artifacts/{asset_id}")).content == (workspace / "report.html").read_bytes()
        assert (await client.post(f"/sessions/{other}/previews", json={"artifact_id": asset_id})).status_code == 403
        child = coordinator.branch(sid)
        side = coordinator.branch(child["id"], side_chat=True)
        assert coordinator.attachments.resolve(side["id"], attachment["attachment_id"])[0].is_file()
        preview = (await client.post(f"/sessions/{side['id']}/previews", json={"artifact_id": asset_id})).json()["preview_url"]
        async with httpx.AsyncClient(trust_env=False) as browser_client:
            assert (await browser_client.get(preview)).status_code == 200
            coordinator.close_side_chat(side["id"])
            assert (await browser_client.get(preview)).status_code == 404
        assert (await client.post(f"/sessions/{side['id']}/previews", json={"artifact_id": asset_id})).status_code == 403
        assert (await client.post(f"/sessions/{side['id']}/attachments?filename=late.txt", content=b"late",
                                  headers={"Content-Type": "text/plain"})).status_code == 403
        assert (await client.post(f"/sessions/{side['id']}/runs", json={"text": "late", "attachments": [attachment["attachment_id"]]})).status_code in {403, 409}
    package = backup_bundle(data, tmp_path / "bundle.zip")
    restored = tmp_path / "restored"
    restore_bundle(package, restored)
    new = RunCoordinator(restored, tools=None, browser_enabled=False, model_factory=lambda cfg: FakeModel([]))
    restored_app = create_app(new, token=token)
    async with restored_app.router.lifespan_context(restored_app), httpx.AsyncClient(transport=httpx.ASGITransport(app=restored_app),
        base_url="http://127.0.0.1:8765", headers={"Authorization": "Bearer " + token}) as client:
        assert (await client.get(f"/artifacts/{attachment['artifact_id']}")).content == png_bytes()
        response = await client.post(f"/sessions/{child['id']}/previews", json={"artifact_id": asset_id})
        assert response.status_code == 201, response.text
        async with httpx.AsyncClient(trust_env=False) as browser_client:
            assert "data-aster-preview" in (await browser_client.get(response.json()["preview_url"])).text
        messages = new.store.history(child["id"])
        encoded = encode_messages(messages, restored / "artifacts", partial(resolve_artifact, restored))
        assert any(isinstance(item.get("content"), list) for item in encoded)
    assert not prune_orphan_artifacts(restored, 0, dry_run=True)["candidates"]


async def test_http_dynamic_policy_approval_journal_and_late_cancel(tmp_path, http_server):
    from aster.domain import ToolCall
    from aster.engine.dispatcher import Dispatcher
    from aster.policy import ApprovalBroker, PermissionPolicy
    from aster.tools.registry import Registry
    data, cwd, store, session, _ = setup_store(tmp_path)
    run, _ = store.create_run(session["id"])
    tool = HTTPTool(allowed_hosts=["127.0.0.1"])
    broker = ApprovalBroker(store)
    dispatcher = Dispatcher(Registry([tool]), store, PermissionPolicy(), broker)
    context = ToolContext(session["id"], run["id"], cwd, data / "artifacts", asyncio.Event())
    task = None
    try:
        read = (await dispatcher.execute([ToolCall("read", "http_request", {"url": http_server + "/page"})], context))[0]
        assert not read.is_error
        assert store.tool_executions(run["id"])[0]["risk"] == "read"
        call = ToolCall("write", "http_request", {"url": http_server + "/create", "method": "POST", "body": "action"})
        task = asyncio.create_task(dispatcher.execute([call], context))
        for _ in range(100):
            pending = store.pending_requests(run["id"])
            if pending:
                break
            await asyncio.sleep(0.01)
        assert pending and not any(item[0].startswith("POST") for item in Handler.calls)
        assert broker.resolve(pending[0]["id"], True)
        created = (await task)[0]
        assert not created.is_error
        writes = [entry for entry in store.tool_executions(run["id"]) if entry["tool_name"] == "http_request" and entry["risk"] == "write"]
        assert len(writes) == 1 and writes[0]["state"] == "committed"
        task = asyncio.create_task(dispatcher.execute([ToolCall("cancelled-write", "http_request", {"url": http_server + "/cancel", "method": "POST"})], context))
        for _ in range(100):
            pending = store.pending_requests(run["id"])
            if pending:
                break
            await asyncio.sleep(0.01)
        context.cancel.set()
        denied = (await task)[0]
        assert denied.is_error and not broker.resolve(pending[0]["id"], True)
        assert not any(item[0] == "POST /cancel" for item in Handler.calls)
    finally:
        context.cancel.set()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        store.close()


def test_restore_genuine_v1_database_migrates_copy_only(tmp_path):
    """Use the actual v1 DDL, without constructing a v2 Store or downgrading its PRAGMA."""
    import re
    import zipfile

    from aster.maintenance import restore_database
    from aster.state.store import SCHEMA_VERSION
    # Core v1 DDL remains additive and unchanged; extension tables are installed separately.
    source = Path(__file__).parents[1] / "src" / "aster" / "state" / "store.py"
    ddl = re.search(r'self.connection.executescript\("""(.*?)"""\)', source.read_text(), re.DOTALL)[1]
    old = tmp_path / "old"
    old.mkdir()
    database = old / "runtime.db"
    db = sqlite3.connect(database)
    db.executescript(ddl)
    db.execute("PRAGMA user_version=1")
    db.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?)", ("legacy", str(tmp_path), "default", None, "original v1", 1, 1))
    db.commit()
    assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='session_lineage'").fetchone()
    db.close()
    original = digest(database)
    package = backup_bundle(old, tmp_path / "v1.zip")
    with zipfile.ZipFile(package) as archive:
        assert json.loads(archive.read("manifest.json"))["schema_version"] == 1
    restored = tmp_path / "restored"
    restore_bundle(package, restored)
    assert validate_database(restored / "runtime.db")["schema_version"] == SCHEMA_VERSION
    copy = Store(restored / "runtime.db")
    assert copy.get_session("legacy")["title"] == "original v1"
    assert copy.lineage("legacy") is None
    copy.close()
    restore_database(tmp_path / "db-only", database)
    assert validate_database(tmp_path / "db-only" / "runtime.db")["schema_version"] == SCHEMA_VERSION
    assert digest(database) == original and validate_database(database)["schema_version"] == 1


@pytest.mark.windows
async def test_windows_junctions_cannot_escape_attachment_or_preview_scope(tmp_path):
    if os.name != "nt":
        pytest.skip("Windows junction integration")
    data, cwd, store, session, attachments = setup_store(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside boundary", encoding="utf-8")
    link = cwd / "escape"
    created = await asyncio.to_thread(subprocess.run, ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
                                      capture_output=True, check=False)
    assert created.returncode == 0, created.stderr.decode(errors="replace")
    previews = PreviewManager()
    entry = cwd / "index.html"
    entry.write_text("<h1>Allowed</h1>", encoding="utf-8")
    try:
        with pytest.raises(PermissionError):
            attachments.register_path(session["id"], link / "secret.txt")
        url = previews.register(entry, session["id"], root=cwd)
        async with httpx.AsyncClient(trust_env=False) as client:
            escaped = await client.get(url.rsplit("/", 1)[0] + "/escape/secret.txt")
            assert escaped.status_code == 404 and "outside boundary" not in escaped.text
        artifact_root = data / "artifacts"
        artifact_root.mkdir()
        storage_link = artifact_root / "attachments"
        created = await asyncio.to_thread(subprocess.run, ["cmd", "/c", "mklink", "/J", str(storage_link), str(outside)],
                                          capture_output=True, check=False)
        assert created.returncode == 0
        try:
            with pytest.raises(PermissionError):
                attachments.register_bytes(session["id"], "new.txt", b"must not escape")
            assert sorted(item.name for item in outside.iterdir()) == ["secret.txt"]
        finally:
            storage_link.rmdir()
    finally:
        await previews.close()
        link.rmdir()
        store.close()


def test_attachment_pixel_and_byte_budgets(tmp_path):
    import struct
    import zlib

    from aster.service.attachments import MAX_ATTACHMENT_BYTES, inspect_content
    with pytest.raises(ValueError, match="25 MiB"):
        inspect_content("large.txt", b"x" * (MAX_ATTACHMENT_BYTES + 1))
    image = bytearray(png_bytes())
    image[16:24] = struct.pack(">II", 10000, 7000)
    image[29:33] = struct.pack(">I", zlib.crc32(image[12:29]))
    with pytest.raises(ValueError, match="megapixels"):
        inspect_content("bomb.png", bytes(image))


async def test_http_credentials_require_approval_and_exact_trusted_destination(tmp_path, http_server):
    from aster.domain import ToolCall
    from aster.engine.dispatcher import Dispatcher
    from aster.policy import ApprovalBroker, PermissionPolicy
    from aster.tools.registry import Registry
    data, cwd, store, session, _ = setup_store(tmp_path)
    run, _ = store.create_run(session["id"])
    resolved = []

    def resolver(ref):
        resolved.append(ref)
        return "bound-fixture-secret"

    ctx = ToolContext(session["id"], run["id"], cwd, data / "artifacts", asyncio.Event(), services={
        "http_allowed_hosts": ["127.0.0.1"], "secret_resolver": resolver,
        "http_credential_origins": {"env:BOUND": [http_server]},
    })
    tool = HTTPTool(fetch=True)
    call = ToolCall("auth", "web_fetch", {"url": http_server + "/echo", "credential_headers": {"Authorization": "env:BOUND"}})
    task = None
    try:
        denied = (await Dispatcher(Registry([tool]), store, PermissionPolicy()).execute([call], ctx))[0]
        assert denied.is_error and denied.error_category == "permission"
        assert not resolved and not Handler.calls
        broker = ApprovalBroker(store)
        dispatcher = Dispatcher(Registry([tool]), store, PermissionPolicy(), broker)
        task = asyncio.create_task(dispatcher.execute([call], ctx))
        for _ in range(100):
            pending = store.pending_requests(run["id"])
            if pending:
                break
            await asyncio.sleep(0.01)
        assert pending and not resolved and not Handler.calls
        broker.resolve(pending[0]["id"], True)
        result = (await task)[0]
        assert not result.is_error, result.content
        assert resolved == ["env:BOUND"] and Handler.calls == [("/echo", "bound-fixture-secret")]
        assert "bound-fixture-secret" not in json.dumps(store.tool_executions(run["id"]))
        for reference, destination in (("env:UNKNOWN", http_server), ("env:DEEPSEEK_API_KEY", http_server),
                                       ("keyring:aster/deepseek", http_server), ("env:BOUND", "http://example.invalid")):
            rejected = await tool.execute({"url": destination, "credential_headers": {"Authorization": reference}}, ctx)
            assert rejected.is_error and rejected.error_category == "permission"
        assert resolved == ["env:BOUND"] and len(Handler.calls) == 1
    finally:
        ctx.cancel.set()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        store.close()


@pytest.mark.browser
@pytest.mark.parametrize("attack,interactive", [(attack, True) for attack in ("location", "top", "meta", "link", "form", "popup")] + [("meta", False)])
async def test_preview_plain_chromium_cannot_navigate_or_post_to_other_origin(tmp_path, http_server, attack, interactive):
    """No BrowserManager routing: the URL must be safe in an ordinary browser too."""
    from playwright.async_api import async_playwright
    target = http_server + "/exfil?value=synthetic-preview-only"
    attacks = {
        "location": f"<script>location.href={json.dumps(target)}</script>",
        "top": f"<script>window.top.location.href={json.dumps(target)}</script>",
        "meta": f'<meta http-equiv="refresh" content="0;url={target}">',
        "link": f'<a id="attack" href="{target}" target="_top">Navigate</a>',
        "form": f'<form action="{target}" method="post"><button id="attack">Submit</button></form>',
        "popup": f'<button id="attack" onclick=\'window.open({json.dumps(target)})\'>Popup</button>',
    }
    source = tmp_path / "hostile.html"
    source.write_text("<!doctype html><h1>Untrusted preview</h1>" + attacks[attack], encoding="utf-8")
    previews = PreviewManager()
    url = previews.register(source, "session", interactive=interactive)
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                response = await page.goto(url)
                assert response.status == 200
                if attack in {"link", "form", "popup"}:
                    await page.frame_locator("iframe[data-aster-preview]").locator("#attack").click()
                await asyncio.sleep(0.15)
                assert page.url == url and len(browser.contexts[0].pages) == 1
                assert Handler.calls == [], f"Preview escaped via {attack}: {Handler.calls}"
                raw_path = await page.locator("iframe[data-aster-preview]").get_attribute("src")
                raw_url = urlsplit_for_test(url) + raw_path
                direct = await page.goto(raw_url)
                assert direct.status == 403
                await asyncio.sleep(0.05)
                assert not Handler.calls
            finally:
                await browser.close()
    finally:
        await previews.close()


def urlsplit_for_test(url):
    from urllib.parse import urlsplit
    value = urlsplit(url)
    return f"{value.scheme}://{value.netloc}"


def test_application_json_cannot_poison_artifact_registry_backup_or_gc(tmp_path):
    from aster.domain import ToolCall, ToolResult
    from aster.maintenance import registered_artifacts, resolve_artifact
    data, _cwd, store, session, attachments = setup_store(tmp_path)
    record = attachments.register_bytes(session["id"], "real.txt", b"authoritative data")
    run, _ = store.create_run(session["id"])
    counterfeit = {"artifact_id": record["artifact_id"], "path": str(tmp_path / "not-authoritative")}
    entry = store.prepare_tool(session["id"], run["id"], ToolCall("memory", "memory_record", {"value": counterfeit}), risk="write", replay_safe=False)
    store.mark_dispatching(entry["id"])
    result = ToolResult("Saved memory", data={"memory": {"value": counterfeit}})
    store.commit_tool(entry["id"], result)
    store.append_message(session["id"], run["id"], Message("tool", "Memory result", tool_call_id="memory", metadata={"data": result.data}))
    store.emit_event(session["id"], run["id"], "memory.updated", {"value": counterfeit})
    store.checkpoint(session["id"], run["id"], store.history(session["id"]))
    store.set_run_status(run["id"], "completed")
    store.close()
    assert set(registered_artifacts(data / "runtime.db")) == {record["artifact_id"]}
    assert resolve_artifact(data, artifact_id=record["artifact_id"]).read_bytes() == b"authoritative data"
    assert not prune_orphan_artifacts(data, 0, dry_run=True)["candidates"]
    package = backup_bundle(data, tmp_path / "safe.zip")
    restored = tmp_path / "restored"
    restore_bundle(package, restored)
    assert resolve_artifact(restored, artifact_id=record["artifact_id"]).read_bytes() == b"authoritative data"


async def _assert_preview_peer_disconnected(reader):
    try:
        remaining = await asyncio.wait_for(reader.read(), 2)
    except ConnectionResetError as error:
        # Windows may report EOF as a reset when the server shuts a partial request.
        if getattr(error, "winerror", None) not in {64, 10054}:
            raise
    else:
        assert remaining == b""


async def _close_preview_client_writer(writer):
    writer.close()
    try:
        await writer.wait_closed()
    except ConnectionResetError as error:
        # Accept only the Windows peer-shutdown errors, not arbitrary socket failures.
        if getattr(error, "winerror", None) not in {64, 10054}:
            raise


@pytest.mark.parametrize("connection_count", [1, 4])
async def test_preview_shutdown_closes_owned_incomplete_http_connections(tmp_path, connection_count):
    from urllib.parse import urlsplit
    source = tmp_path / "index.html"
    source.write_text("<p>Local</p>")
    manager = PreviewManager()
    url = urlsplit(manager.register(source, "s"))
    clients = [await asyncio.open_connection(url.hostname, url.port) for _ in range(connection_count)]
    thread = manager._thread
    try:
        for _reader, writer in clients:
            writer.write(b"GET /pending HTTP/1.1\r\n")
        await asyncio.gather(*(writer.drain() for _reader, writer in clients))
        for _ in range(50):
            if len(manager._connections) == connection_count:
                break
            await asyncio.sleep(0.01)
        assert len(manager._connections) == connection_count
        await asyncio.wait_for(manager.close(), 2)
        await asyncio.gather(*(_assert_preview_peer_disconnected(reader) for reader, _writer in clients))
        assert not thread.is_alive() and not manager._connections
    finally:
        try:
            await asyncio.gather(*(_close_preview_client_writer(writer) for _reader, writer in clients))
        finally:
            await manager.close()


@pytest.mark.parametrize("winerror", [64, 10054])
async def test_preview_client_cleanup_accepts_only_windows_peer_reset_without_masking_assertions(winerror):
    error = ConnectionResetError("peer shut down")
    error.winerror = winerror

    class ResetPeer:
        closed = False

        async def read(self):
            raise error

        def close(self):
            self.closed = True

        async def wait_closed(self):
            raise error

    peer = ResetPeer()
    await _assert_preview_peer_disconnected(peer)
    with pytest.raises(AssertionError, match="original resource assertion"):
        try:
            raise AssertionError("original resource assertion")
        finally:
            await _close_preview_client_writer(peer)
    assert peer.closed


@pytest.mark.parametrize("operation", ["read", "close"])
@pytest.mark.parametrize("error,winerror", [
    (ConnectionResetError("ordinary reset"), None),
    (ConnectionResetError("other Windows reset"), 123),
    (OSError("other socket error"), 64),
    (ValueError("unexpected failure"), None),
])
async def test_preview_client_cleanup_propagates_unrelated_failures(operation, error, winerror):
    if winerror is not None:
        error.winerror = winerror
    class FailedPeer:
        async def read(self):
            raise error

        def close(self):
            pass

        async def wait_closed(self):
            raise error

    with pytest.raises(type(error)) as caught:
        if operation == "read":
            await _assert_preview_peer_disconnected(FailedPeer())
        else:
            await _close_preview_client_writer(FailedPeer())
    assert caught.value is error


async def test_preview_peer_disconnected_requires_eof_when_read_succeeds():
    class OpenPeer:
        async def read(self):
            return b"unexpected response"

    with pytest.raises(AssertionError):
        await _assert_preview_peer_disconnected(OpenPeer())


@pytest.mark.browser
async def test_default_static_preview_disables_inline_scripts_and_webrtc_udp(tmp_path):
    import socket

    from playwright.async_api import async_playwright
    sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sink.bind(("127.0.0.1", 0))
    sink.settimeout(0.5)
    source = tmp_path / "static.html"
    source.write_text('<h1>Static content</h1><button onclick="document.querySelector(\'h1\').textContent=\'unsafe\'">Try</button>'
                      '<script>window.previewScriptExecuted=true;'
                      'document.querySelector("h1").textContent="unsafe";'
                      f'window.pc=new RTCPeerConnection({{iceServers:[{{urls:"stun:127.0.0.1:{sink.getsockname()[1]}"}}]}});'
                      'pc.createDataChannel("local-probe");pc.createOffer().then(x=>pc.setLocalDescription(x));</script>', encoding="utf-8")
    previews = PreviewManager()
    assert DeliverableTool().classify({"path": str(source)}).risk == "read"
    assert DeliverableTool().classify({"path": str(source), "interactive": True}).risk == "external"
    try:
        url = previews.register(source, "s")
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.goto(url)
                element = await page.locator("iframe[data-aster-preview]").element_handle()
                assert await element.get_attribute("sandbox") == ""
                frame = await element.content_frame()
                await frame.locator("button").click()
                assert await frame.locator("h1").inner_text() == "Static content"
                assert not await frame.evaluate("Boolean(window.previewScriptExecuted || window.pc)")
                with pytest.raises(TimeoutError):
                    await asyncio.to_thread(sink.recvfrom, 2048)
            finally:
                await browser.close()
    finally:
        sink.close()
        await previews.close()


async def test_interactive_deliverable_requires_dispatcher_approval_before_lease(tmp_path):
    from aster.domain import ToolCall
    from aster.engine.dispatcher import Dispatcher
    from aster.policy import ApprovalBroker, PermissionPolicy
    from aster.tools.registry import Registry
    data, cwd, store, session, _ = setup_store(tmp_path)
    run, _ = store.create_run(session["id"])
    source = cwd / "index.html"
    source.write_text("<h1>Review me</h1>", encoding="utf-8")
    previews = PreviewManager()
    ctx = ToolContext(session["id"], run["id"], cwd, data / "artifacts", asyncio.Event(),
                      services={"preview_manager": previews, "store": store})
    call = ToolCall("interactive-preview", "present_deliverable", {"path": str(source), "interactive": True})
    task = None
    try:
        registry = Registry([DeliverableTool()])
        denied = (await Dispatcher(registry, store, PermissionPolicy()).execute([call], ctx))[0]
        assert denied.is_error and denied.error_category == "permission"
        assert not previews._leases and not ctx.artifact_dir.exists()
        broker = ApprovalBroker(store)
        dispatcher = Dispatcher(registry, store, PermissionPolicy(), broker)
        task = asyncio.create_task(dispatcher.execute([call], ctx))
        pending = []
        for _ in range(100):
            pending = store.pending_requests(run["id"])
            if pending:
                break
            await asyncio.sleep(0.01)
        assert pending and not previews._leases and not ctx.artifact_dir.exists()
        broker.resolve(pending[0]["id"], True)
        result = (await task)[0]
        assert not result.is_error, result.content
        assert result.data["interactive"] and result.data["preview_url"]
        assert len(previews._leases) == 1
        assert next(iter(previews._leases.values()))["interactive"] is True
    finally:
        ctx.cancel.set()
        if task:
            await asyncio.gather(task, return_exceptions=True)
        await previews.close()
        store.close()
