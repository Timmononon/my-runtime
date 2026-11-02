"""Real browser save/reopen coverage with isolated file credentials and no paid calls."""
import asyncio
import os
import socket
from pathlib import Path

import httpx
import pytest
import uvicorn
from playwright.async_api import async_playwright, expect
from test_web_audit_browser import geometry, sidebar, tab
from test_web_workbench import client_for
from test_web_workbench_browser import open_settings, open_workbench, screenshot

from aster.api import create_app
from aster.domain import ModelTurn
from aster.engine import FakeModel
from aster.service import RunCoordinator

KEY = "synthetic-browser-jev-not-a-real-secret"


@pytest.fixture
async def auto_memory_settings_server(tmp_path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    root, workspace = tmp_path / "runtime", tmp_path / "workspace"
    workspace.mkdir()
    coordinator = RunCoordinator(root, tools=[], model_factory=lambda _: FakeModel([ModelTurn("offline")]))
    coordinator.credentials.set("deepseek", "synthetic-browser-deepseek-not-real")
    # This fixture only operates settings. Any accidental network call fails.
    async def reject_outbound(request):
        raise AssertionError("Settings must not call paid services")
    coordinator.providers.client_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(reject_outbound))
    token = "auto-memory-settings-offline-browser-token-only"
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    static = Path(os.environ["ASTER_WEB_STATIC_ROOT"]) if os.environ.get("ASTER_WEB_STATIC_ROOT") else None
    app = create_app(coordinator, token=token, allowed_origins={origin}, static_root=static)
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False,
                                         proxy_headers=False, timeout_graceful_shutdown=2))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    await task
                    raise AssertionError("Settings fixture stopped during startup")
                await asyncio.sleep(.01)
        yield {"base_url": origin, "data_dir": str(root), "cwd": str(workspace), "token": token,
               "coordinator": coordinator}
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, 10)
        finally:
            await coordinator.close()
            listener.close()


@pytest.mark.browser
@pytest.mark.parametrize("width", [1440, 390])
async def test_auto_memory_key_enable_reopen_disable(auto_memory_settings_server, width):
    server = auto_memory_settings_server
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context, page, errors = await open_workbench(browser, server, width=width)
            await sidebar(page)
            dialog = await open_settings(page)
            panel = await tab(dialog, "搜索与记忆")
            card = panel.locator(".auto-memory-settings")
            toggle = card.get_by_label("每轮聊天后自动判断并保存记忆", exact=True)
            await expect(toggle).not_to_be_checked()
            await expect(card).to_contain_text("Jev 密钥未配置")
            await card.get_by_label("Jev API Key", exact=True).fill(KEY)
            # Draft survives saving the separate write-only key.
            await toggle.check()
            await card.get_by_label("记忆判断阈值", exact=True).fill("0.85")
            async with page.expect_response(lambda response: response.request.method == "POST"
                                            and response.url.endswith("/management/credentials")) as key_response:
                await card.get_by_role("button", name="保存 Jev 密钥", exact=True).click()
            response = await key_response.value
            assert response.status == 201 and KEY not in await response.text()
            await expect(card.get_by_label("Jev API Key", exact=True)).to_have_value("")
            await expect(toggle).to_be_checked()
            await expect(card.get_by_label("记忆判断阈值", exact=True)).to_have_value("0.85")
            await expect(card).to_contain_text("Jev 密钥已配置")
            async with client_for(server) as api:
                saved_key_only = (await api.get("/management")).json()
                assert saved_key_only["extensions"]["auto_memory"]["enabled"] is False
            async with page.expect_response(lambda response: response.request.method == "PATCH"
                                            and response.url.endswith("/management")) as saved:
                await panel.get_by_role("button", name="保存搜索、记忆与浏览器", exact=True).click()
            result = await saved.value
            assert result.status == 200, await result.text()
            assert KEY not in await result.text()
            await expect(card).to_contain_text("已启用，配置就绪（尚未在线验证）")
            await dialog.get_by_role("button", name="关闭", exact=True).click()
            await sidebar(page)
            dialog = await open_settings(page)
            panel = await tab(dialog, "搜索与记忆")
            card = panel.locator(".auto-memory-settings")
            await expect(card.get_by_label("每轮聊天后自动判断并保存记忆", exact=True)).to_be_checked()
            await expect(card.get_by_label("记忆判断阈值", exact=True)).to_have_value("0.85")
            await expect(card.get_by_label("Jev API Key", exact=True)).to_have_value("")
            await expect(card.get_by_label("DeepSeek 记忆提取模型", exact=True)).to_have_value("deepseek-flash")
            await geometry(page, dialog)
            await card.scroll_into_view_if_needed()
            await screenshot(page, f"jev-auto-memory-settings-{width}.png")
            await card.get_by_label("每轮聊天后自动判断并保存记忆", exact=True).uncheck()
            async with page.expect_response(lambda response: response.request.method == "PATCH"
                                            and response.url.endswith("/management")) as disabled:
                await panel.get_by_role("button", name="保存搜索、记忆与浏览器", exact=True).click()
            assert (await disabled.value).status == 200
            async with client_for(server) as api:
                response = await api.get("/management")
                assert response.status_code == 200 and KEY not in response.text
                assert response.json()["extensions"]["auto_memory"]["enabled"] is False
                assert response.json()["extensions"]["auto_memory_status"]["gate_configured"] is True
            assert server["coordinator"].store.list_runs() == []
            assert KEY not in (Path(server["data_dir"]) / "config.yaml").read_text()
            assert errors == []
            await context.close()
        finally:
            await browser.close()
