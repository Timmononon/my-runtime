import asyncio
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from aster.domain import ToolContext
from aster.tools.browser import BrowserManager, build_browser_tools


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/disconnect":
            self.connection.close()
            return
        if self.path == "/download":
            self.send_response(200)
            self.send_header("Content-Disposition", 'attachment; filename="test.txt"')
            self.end_headers()
            self.wfile.write(b"downloaded content")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b'''<!doctype html><title>Aster test</title><h1>Local page</h1>
          <input id="name"><button id="set" onclick="document.querySelector('#result').textContent=document.querySelector('#name').value">Set</button>
          <p id="result"></p><a id="download" href="/download">Download</a>
          <input id="upload" type="file"><select id="choice"><option value="a">A</option><option value="b">B</option></select>
          <div id="hidden" style="display:none">hidden</div>''')

    def log_message(self, *args):
        pass


@pytest.fixture
def local_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    worker.join()


@pytest.mark.browser
async def test_browser_open_url_navigates_and_failed_navigation_is_not_success(tmp_path, local_url):
    manager = BrowserManager(tmp_path / "profiles")
    tools = {tool.spec.name: tool for tool in build_browser_tools(manager)}
    context = ToolContext("open-url", "run", tmp_path, tmp_path / "artifacts", asyncio.Event())
    try:
        opened = await tools["browser_open"].execute({"url": local_url}, context)
        assert not opened.is_error, opened.content
        assert opened.data["context_id"] and opened.data["url"] == local_url + "/"
        assert opened.data["status"] == 200
        page = opened.data["pages"][0]
        assert page["page_id"] and page["url"] == local_url + "/"
        snapshot = await tools["browser_snapshot"].execute({"page_id": page["page_id"]}, context)
        assert not snapshot.is_error and "Local page" in snapshot.content
        failed = await tools["browser_open"].execute({"url": local_url + "/disconnect", "timeout": 3}, context)
        assert failed.is_error and failed.error_category == "browser" and failed.outcome == "known"
        denied = await tools["browser_open"].execute({"url": "file:///C:/Windows/win.ini"}, context)
        assert denied.is_error and "HTTP(S)" in denied.content
    finally:
        await manager.close()


@pytest.mark.browser
async def test_local_browser_workflow_and_identity_isolation(tmp_path, local_url):
    manager = BrowserManager(tmp_path / "profiles")
    tools = {tool.spec.name: tool for tool in build_browser_tools(manager)}
    context = ToolContext("alice", "run", tmp_path, tmp_path / "artifacts", asyncio.Event())
    try:
        opened = await tools["browser_open"].execute({}, context)
        assert not opened.is_error, opened.content
        repeated = await asyncio.gather(tools["browser_open"].execute({}, context),
                                        tools["browser_open"].execute({}, context))
        assert all(item.data["context_id"] == opened.data["context_id"] for item in repeated)
        assert len(manager._contexts) == 1
        page = opened.data["pages"][0]["page_id"]
        result = await tools["browser_navigate"].execute({"page_id": page, "url": local_url}, context)
        assert not result.is_error, result.content
        snapshot = await tools["browser_snapshot"].execute({"page_id": page}, context)
        assert "Local page" in snapshot.content
        filled = await tools["browser_fill"].execute({"page_id": page, "selector": "#name", "text": "中文"}, context)
        assert not filled.is_error, filled.content
        clicked = await tools["browser_click"].execute({"page_id": page, "selector": "#set"}, context)
        assert not clicked.is_error
        snapshot = await tools["browser_snapshot"].execute({"page_id": page}, context)
        assert "中文" in snapshot.content
        selected = await tools["browser_select"].execute({"page_id": page, "selector": "#choice", "values": ["b"]}, context)
        assert selected.data["values"] == ["b"]
        upload = tmp_path / "upload.txt"
        upload.write_text("upload")
        uploaded = await tools["browser_upload"].execute({"page_id": page, "selector": "#upload", "paths": [str(upload)]}, context)
        assert not uploaded.is_error
        downloaded = await tools["browser_download"].execute({"page_id": page, "selector": "#download"}, context)
        assert not downloaded.is_error, downloaded.content
        assert Path(downloaded.artifacts[0]["path"]).read_text() == "downloaded content"
        screenshot = await tools["browser_screenshot"].execute({"page_id": page}, context)
        assert not screenshot.is_error
        assert screenshot.artifacts[0]["width"] > 0 and screenshot.artifacts[0]["session_id"] == "alice"
        assert screenshot.data["content_parts"][1]["path"] == screenshot.artifacts[0]["path"]
        other = ToolContext("bob", "run2", tmp_path, tmp_path / "artifacts2", asyncio.Event())
        denied = await tools["browser_snapshot"].execute({"page_id": page}, other)
        assert denied.is_error
        old_version = snapshot.data["snapshot_version"]
        await tools["browser_navigate"].execute({"page_id": page, "url": local_url + "/next"}, context)
        stale = await tools["browser_click"].execute({"page_id": page, "ref": "e0", "snapshot_version": old_version}, context)
        assert stale.is_error and "Stale" in stale.content and stale.outcome == "known"
    finally:
        await manager.close()


@pytest.mark.browser
async def test_browser_cancel_closes_owned_context(tmp_path, local_url):
    manager = BrowserManager(tmp_path / "profiles")
    tools = {tool.spec.name: tool for tool in build_browser_tools(manager)}
    context = ToolContext("s", "r", tmp_path, tmp_path / "artifacts", asyncio.Event())
    try:
        opened = await tools["browser_open"].execute({}, context)
        assert not opened.is_error, opened.content
        page = opened.data["pages"][0]["page_id"]
        await tools["browser_navigate"].execute({"page_id": page, "url": local_url}, context)
        task = asyncio.create_task(tools["browser_wait"].execute(
            {"page_id": page, "selector": "#hidden", "state": "visible", "timeout": 60}, context))
        await asyncio.sleep(0.2)
        other = ToolContext("other", "r", tmp_path, tmp_path / "other", asyncio.Event())
        queued = asyncio.create_task(tools["browser_open"].execute({}, other))
        await asyncio.sleep(0.05)
        other.cancel.set()
        queued_result = await asyncio.wait_for(queued, 2)
        assert queued_result.outcome == "known" and "queued" in queued_result.content
        context.cancel.set()
        result = await asyncio.wait_for(task, 15)
        assert result.is_error and result.error_category == "cancelled"
        assert not manager._contexts
    finally:
        await manager.close()


@pytest.mark.browser
async def test_post_dispatch_browser_failure_is_unknown(tmp_path, local_url):
    manager = BrowserManager(tmp_path / "profiles")
    tools = {tool.spec.name: tool for tool in build_browser_tools(manager)}
    context = ToolContext("s", "r", tmp_path, tmp_path / "artifacts", asyncio.Event())
    try:
        opened = await tools["browser_open"].execute({}, context)
        assert not opened.is_error, opened.content
        page_id = opened.data["pages"][0]["page_id"]
        await tools["browser_navigate"].execute({"page_id": page_id, "url": local_url}, context)
        state = manager._pages[page_id]
        await state.page.locator("#set").evaluate("e => e.setAttribute('onclick', \"window.close()\")")
        # A page close after a dispatched action cannot safely be treated as a
        # guaranteed no-effect failure. Closing explicitly on click exercises it.
        state.page.on("request", lambda request: None)
        original = state.page.title

        async def disconnected():
            raise RuntimeError("simulated transport loss after click")

        state.page.title = disconnected
        result = await tools["browser_click"].execute({"page_id": page_id, "selector": "#set"}, context)
        state.page.title = original
        assert result.is_error and result.outcome == "unknown"
    finally:
        await manager.close()
