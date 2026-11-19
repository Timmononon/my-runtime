"""The chat stays writable while its completed turn checks whether to save a memory."""
import asyncio
from pathlib import Path

import httpx
import pytest
from playwright.async_api import async_playwright, expect
from test_auto_memory_runtime import configuration
from test_auto_memory_settings_browser import (
    auto_memory_settings_server as auto_memory_settings_server,  # noqa: PLC0414 - pytest fixture re-export
)
from test_execution_reconciliation_api import unknown_run
from test_web_workbench_browser import open_workbench


@pytest.mark.browser
async def test_completed_chat_accepts_followup_while_memory_gate_is_dispatching(auto_memory_settings_server):
    server = auto_memory_settings_server
    coordinator = server["coordinator"]
    store = coordinator.store
    configuration(Path(server["data_dir"]), auto_memory={"enabled": True, "timeout_seconds": 60})
    coordinator.credentials.set("jev", "synthetic-browser-jev-for-held-gate")
    entered, release = asyncio.Event(), asyncio.Event()

    async def hold_gate(request):
        entered.set()
        await release.wait()
        return httpx.Response(503)

    coordinator.auto_memory_client_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(hold_gate))
    coordinator.auto_memory_model_factory = lambda settings, pin: coordinator.model_factory(settings)
    chat = coordinator.create_session(server["cwd"], title="正在检查记忆")
    uncertain = coordinator.create_session(server["cwd"], title="确实需要核对")
    unknown_run(store, uncertain["id"])

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            first = await coordinator.submit(chat["id"], "我一直使用 Windows。")
            await asyncio.wait_for(entered.wait(), 10)
            assert store.get_run(first["id"])["status"] == "completed"
            [gate] = store.tool_executions(first["id"])
            assert (gate["tool_name"], gate["state"]) == ("memory_auto_gate", "dispatching")

            context, page, errors = await open_workbench(browser, server)
            await page.get_by_role("button", name=chat["title"], exact=True).click()
            await expect(page.get_by_test_id("message-assistant").get_by_text("offline", exact=True)).to_be_visible()
            notice = page.get_by_text("结果待核对", exact=True)
            await expect(notice).to_have_count(0)

            composer = page.get_by_role("textbox", name="输入消息")
            send = page.get_by_role("button", name="发送消息", exact=True)
            await composer.fill("请继续。")
            await expect(send).to_be_enabled()
            endpoint = f"{server['base_url']}/sessions/{chat['id']}/runs"
            async with page.expect_response(lambda r: r.request.method == "POST" and r.url == endpoint) as submitted:
                await send.click()
            response = await submitted.value
            assert response.status == 202
            second = await response.json()
            assert store.get_run(second["id"])["status"] == "queued"
            assert store.tool_execution(gate["id"])["state"] == "dispatching"
            release.set()
            assert (await coordinator.wait(second["id"], timeout=10)).status == "completed"
            settled = store.tool_execution(gate["id"])
            assert settled["state"] == "committed" and settled["result"]["outcome"] == "known"

            await page.get_by_role("button", name=uncertain["title"], exact=True).click()
            await expect(notice).to_be_visible()
            await composer.fill("等核对后再发送。")
            await expect(send).to_be_disabled()
            assert not errors
            await context.close()
        finally:
            release.set()
            await browser.close()
