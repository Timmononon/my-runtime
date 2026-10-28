"""Owned Chromium CDP fixtures exercise user-browser leases without touching Chrome."""
import asyncio
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from playwright.async_api import async_playwright

from aster.domain import ToolContext
from aster.tools.browser import BrowserManager, build_browser_tools


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b'<title>Owned bridge fixture</title><input id="name"><button id="set">Set</button><p>Authorized page evidence</p><div id="hidden" style="display:none">Hidden</div>')

    def log_message(self, *args):
        pass


@pytest.fixture
async def owned_cdp(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_port}"
    with socket.socket() as selected:
        selected.bind(("127.0.0.1", 0))
        port = selected.getsockname()[1]
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=True, args=[f"--remote-debugging-port={port}"])
    context = await browser.new_context()
    authorized = await context.new_page()
    await authorized.goto(base + "/allowed")
    unrelated = await context.new_page()
    await unrelated.goto(base + "/private")
    endpoint = f"http://127.0.0.1:{port}"
    async with httpx.AsyncClient(trust_env=False) as http:
        assert (await http.get(endpoint + "/json/version")).status_code == 200
    try:
        yield {"endpoint": endpoint, "url": base + "/allowed", "private": base + "/private",
               "page": authorized, "unrelated": unrelated, "browser": browser, "context": context}
    finally:
        # Only the Chromium launched by this fixture is closed. Aster's bridge
        # must detach, leaving this independently-owned instance alive until here.
        await browser.close()
        await playwright.stop()
        server.shutdown()
        server.server_close()
        worker.join()


def context(tmp_path, fixture, session="alice", *, operator=False):
    return ToolContext(session, "run", tmp_path, tmp_path / (session + "-artifacts"), asyncio.Event(), services={
        "browser_config": {"mode": "bridge", "headless": True, "cdp_endpoint": fixture["endpoint"], "allowed_pages": [fixture["url"]]},
        "browser_operator": operator})


@pytest.mark.browser
async def test_real_cdp_lease_scope_allowlist_pause_resume_and_detach_preserve_user_browser(tmp_path, owned_cdp):
    manager = BrowserManager(tmp_path / "profiles")
    tools = {tool.spec.name: tool for tool in build_browser_tools(manager)}
    ctx = context(tmp_path, owned_cdp, operator=True)
    try:
        attached = await tools["browser_attach"].execute({"url": owned_cdp["url"]}, ctx)
        assert not attached.is_error, attached.content
        page_id = attached.data["page_id"]
        listed = await tools["browser_tabs"].execute({}, ctx)
        assert len(listed.data["pages"]) == 1 and owned_cdp["private"] not in listed.content
        other = context(tmp_path, owned_cdp, "bob")
        foreign = await tools["browser_snapshot"].execute({"page_id": page_id}, other)
        assert foreign.is_error and foreign.outcome == "known"
        leased = await tools["browser_attach"].execute({"url": owned_cdp["url"]}, other)
        assert leased.is_error and "leased" in leased.content
        denied = await tools["browser_attach"].execute({"url": owned_cdp["private"]}, ctx)
        assert denied.is_error
        assert not (await tools["browser_handoff"].execute({"operation": "pause"}, ctx)).is_error
        blocked = await tools["browser_fill"].execute({"page_id": page_id, "selector": "#name", "text": "automation"}, ctx)
        assert blocked.is_error and "paused" in blocked.content
        assert not (await tools["browser_snapshot"].execute({"page_id": page_id}, ctx)).is_error
        await owned_cdp["page"].fill("#name", "user control")
        model_ctx = context(tmp_path, owned_cdp)
        silent_resume = await tools["browser_handoff"].execute({"operation": "resume"}, model_ctx)
        assert silent_resume.is_error and "operator" in silent_resume.content
        assert not (await tools["browser_handoff"].execute({"operation": "resume"}, ctx)).is_error
        assert not (await tools["browser_fill"].execute({"page_id": page_id, "selector": "#name", "text": "resumed"}, model_ctx)).is_error
        assert await owned_cdp["page"].input_value("#name") == "resumed"
        closed_tab = await tools["browser_tabs"].execute({"operation": "close", "page_id": page_id}, ctx)
        assert closed_tab.is_error and not owned_cdp["page"].is_closed()
        redirected = await tools["browser_navigate"].execute({"page_id": page_id, "url": owned_cdp["private"]}, ctx)
        assert redirected.is_error and owned_cdp["page"].url == owned_cdp["url"]
        await tools["browser_close"].execute({}, ctx)
        assert owned_cdp["browser"].is_connected()
        assert not owned_cdp["page"].is_closed() and not owned_cdp["unrelated"].is_closed()
        assert await owned_cdp["page"].title() == "Owned bridge fixture"
        assert not manager._leases
    finally:
        await manager.close()
        assert owned_cdp["browser"].is_connected()


@pytest.mark.browser
async def test_bridge_cancellation_only_detaches_and_never_closes_user_context(tmp_path, owned_cdp):
    manager = BrowserManager(tmp_path / "profiles")
    tools = {tool.spec.name: tool for tool in build_browser_tools(manager)}
    ctx = context(tmp_path, owned_cdp)
    try:
        attached = await tools["browser_attach"].execute({"url": owned_cdp["url"]}, ctx)
        assert not attached.is_error, attached.content
        task = asyncio.create_task(tools["browser_wait"].execute({"page_id": attached.data["page_id"], "selector": "#hidden", "state": "visible", "timeout": 60}, ctx))
        await asyncio.sleep(0.1)
        ctx.cancel.set()
        result = await asyncio.wait_for(task, 15)
        assert result.is_error and result.error_category == "cancelled"
        assert owned_cdp["browser"].is_connected() and not owned_cdp["page"].is_closed()
        assert not manager._bridges and not manager._leases
    finally:
        await manager.close()


@pytest.mark.browser
async def test_bridge_post_dispatch_failure_unknown_without_replay_and_revoked_url_not_observed(tmp_path, owned_cdp):
    manager = BrowserManager(tmp_path / "profiles")
    tools = {tool.spec.name: tool for tool in build_browser_tools(manager)}
    ctx = context(tmp_path, owned_cdp)
    try:
        attached = await tools["browser_attach"].execute({"url": owned_cdp["url"]}, ctx)
        page_id = attached.data["page_id"]
        state = manager._pages[page_id]
        original = state.page.title

        async def failure():
            raise RuntimeError("fixture disconnected after material click")

        state.page.title = failure
        result = await tools["browser_click"].execute({"page_id": page_id, "selector": "#set"}, ctx)
        state.page.title = original
        assert result.is_error and result.outcome == "unknown"
        await owned_cdp["page"].goto(owned_cdp["private"])
        blocked = await tools["browser_snapshot"].execute({"page_id": page_id}, ctx)
        assert blocked.is_error and owned_cdp["private"] not in blocked.content
        assert json.loads((await tools["browser_tabs"].execute({}, ctx)).content) == []
        await tools["browser_close"].execute({}, ctx)
        assert owned_cdp["browser"].is_connected()
    finally:
        await manager.close()


@pytest.mark.browser
async def test_actual_user_page_click_leaving_scope_is_unknown_and_not_repeated(tmp_path, owned_cdp):
    manager = BrowserManager(tmp_path / "profiles")
    tools = {tool.spec.name: tool for tool in build_browser_tools(manager)}
    ctx = context(tmp_path, owned_cdp)
    try:
        attached = await tools["browser_attach"].execute({"url": owned_cdp["url"]}, ctx)
        await owned_cdp["page"].evaluate("url => document.querySelector('#set').onclick=()=>{location.href=url}", owned_cdp["private"])
        result = await tools["browser_click"].execute({"page_id": attached.data["page_id"], "selector": "#set"}, ctx)
        assert result.is_error and result.outcome == "unknown"
        assert owned_cdp["page"].url == owned_cdp["private"]
        assert owned_cdp["private"] not in result.content
        assert (await tools["browser_snapshot"].execute({"page_id": attached.data["page_id"]}, ctx)).is_error
        await tools["browser_close"].execute({}, ctx)
        assert owned_cdp["browser"].is_connected() and not owned_cdp["unrelated"].is_closed()
    finally:
        await manager.close()
