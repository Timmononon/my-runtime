"""Built conversation UI explains canonical tool failures without raw payloads."""
from __future__ import annotations

import pytest
from playwright.async_api import async_playwright, expect
from test_web_workbench import client_for, create_session
from test_web_workbench import workbench_server as workbench_server  # noqa: PLC0414 - pytest fixture re-export
from test_web_workbench_browser import assert_basics, assert_transport, open_workbench, screenshot


def failed_tool_records():
    """Fixture-only history projection; no operation or provider is invoked."""
    private = "private-tool-payload-must-not-render"
    failures = [
        ("github_read", "validation", private, {"reason": "source_not_found"}),
        ("shell_exec", "permission", "Approval is required but no attended approval channel is available\n" + private, {}),
        ("file_read", "io", "[WinError 2] " + private, {}),
        ("web_fetch", "http_status", 'HTTP: 404\n\n{"message":"This repository is empty.","token":"' + private + '"}', {}),
    ]
    calls = [{"id": "failed-" + str(i), "name": tool, "arguments": {"secret": "private-arguments-must-not-render"}}
             for i, (tool, _, _, _) in enumerate(failures)]
    messages = [
        {"role": "user", "content": "检查工具执行失败的原因。", "tool_calls": [], "metadata": {}},
        {"role": "assistant", "content": "正在检查仓库。", "tool_calls": calls,
         "metadata": {"reasoning": "private-reasoning-must-not-render"}},
        *[{"role": "tool", "content": text, "tool_calls": [], "tool_call_id": "failed-" + str(i),
           "metadata": {"is_error": True, "error_category": category, "outcome": "not_executed", "data": data}}
          for i, (_, category, text, data) in enumerate(failures)],
        {"role": "assistant", "content": "检查结束。", "tool_calls": [], "metadata": {}},
    ]
    return [{"id": "fixture-" + str(i), "seq": i + 1, "run_id": None, "created_at": 1, "message": message}
            for i, message in enumerate(messages)]


@pytest.mark.browser
@pytest.mark.parametrize("width", [1440, 390])
async def test_failed_tool_reasons_are_readable_and_safe_in_built_ui(workbench_server, width):
    async with client_for(workbench_server) as api:
        session = await create_session(api, workbench_server, "工具错误验收")

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context, page, errors = await open_workbench(browser, workbench_server, width=width)

            async def fixture_history(route):
                response = await route.fetch()
                value = await response.json()
                value["messages"] = failed_tool_records()
                value["runs"] = []
                await route.fulfill(response=response, json=value)

            await page.route(f"**/sessions/{session['id']}", fixture_history)
            toggle = page.get_by_role("button", name="打开导航", exact=True)
            if await toggle.is_visible():
                await toggle.click()
            await page.get_by_role("button", name="工具错误验收", exact=True).click()
            group = page.locator(".tool-group")
            await expect(group).to_have_count(1)
            await group.locator("summary").click()
            await expect(page.get_by_label("github_read 错误原因")).to_contain_text("仓库名和 Issue ID 不能代替来源 ID")
            await expect(page.get_by_label("shell_exec 错误原因")).to_contain_text("没有可用的审批通道")
            await expect(page.get_by_label("file_read 错误原因")).to_contain_text("文件或目录不存在")
            await expect(page.get_by_label("web_fetch 错误原因")).to_contain_text("GitHub 仓库尚无已提交内容")
            await expect(page.get_by_label("web_fetch 错误原因")).to_contain_text("HTTP 404")
            html = await page.content()
            for private in ("private-tool-payload", "private-arguments", "private-reasoning"):
                assert private not in html
            await assert_basics(page)
            assert await group.evaluate("element => element.getBoundingClientRect().right <= innerWidth + 1")
            await screenshot(page, "tool-errors-desktop.png" if width == 1440 else "tool-errors-narrow.png")
            assert not errors, errors
            assert_transport(page)
            async with client_for(workbench_server) as api:
                stored = (await api.get(f"/sessions/{session['id']}")).json()
                assert stored["messages"] == [] and stored["runs"] == []
            await context.close()
        finally:
            await browser.close()
