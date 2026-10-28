"""Projects, real context actions and provider selection through staged Chromium UI."""
from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from playwright.async_api import async_playwright, expect
from test_web_workbench import client_for, create_session, wait_run
from test_web_workbench import workbench_server as workbench_server  # noqa: PLC0414
from test_web_workbench_browser import (
    assert_basics,
    assert_transport,
    open_settings,
    open_workbench,
    project_action,
    screenshot,
    send,
)


def receipts(server):
    path = Path(server["data_dir"]) / "fixture-model-calls.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


@pytest.mark.browser
async def test_workspace_profile_settings_menu_and_left_navigation_responsive(workbench_server):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            context, page, errors = await open_workbench(browser, workbench_server)
            sidebar = page.locator(".sidebar")
            profile = sidebar.get_by_role("button", name="打开个人菜单", exact=True)
            await expect(profile).to_contain_text("我的工作空间")
            await expect(profile).to_have_attribute("title", "我的工作空间")
            await expect(profile.locator("small")).to_have_count(0)
            assert "default" not in await profile.inner_text()
            assert "本地 Runtime" not in await profile.inner_text()
            await expect(sidebar.get_by_role("button", name="设置", exact=True)).to_have_count(0)
            draft = page.get_by_role("textbox", name="输入消息", exact=True)
            await draft.fill("Keep draft while opening workspace settings")
            await screenshot(page, "workspace-settings-profile-footer.png")
            await profile.click()
            menu = page.get_by_role("dialog", name="个人菜单", exact=True)
            await expect(menu.get_by_role("button")).to_have_count(1)
            await expect(menu.get_by_role("button", name="设置", exact=True)).to_be_in_viewport()
            await expect(menu.locator("svg")).to_have_count(1)
            await screenshot(page, "workspace-settings-profile-menu-gear.png")
            await menu.get_by_role("button", name="设置", exact=True).click()
            await expect(menu).to_have_count(0)
            dialog = page.get_by_role("dialog", name="设置", exact=True)
            nav = dialog.get_by_role("tablist", name="设置类别", exact=True)
            await expect(nav).to_have_attribute("aria-orientation", "vertical")
            await expect(nav.get_by_role("tab", name="模型提供商", exact=True)).to_have_attribute("aria-selected", "true")
            await expect(dialog.get_by_role("tabpanel", name="模型提供商", exact=True)).to_be_visible()
            layouts = []
            for width, height in ((1440, 1000), (390, 844)):
                await page.set_viewport_size({"width": width, "height": height})
                await expect(dialog.get_by_role("button", name="关闭", exact=True)).to_be_in_viewport()
                await expect(dialog.get_by_role("heading", name="设置", exact=True)).to_be_in_viewport()
                nav_box, content_box = await nav.bounding_box(), await dialog.locator(".settings-content").bounding_box()
                assert nav_box is not None and content_box is not None
                assert nav_box["x"] + nav_box["width"] <= content_box["x"] + 1
                assert content_box["x"] + content_box["width"] <= width
                assert await dialog.locator(".settings-content").evaluate("el => el.scrollWidth <= el.clientWidth + 1")
                await expect(dialog.get_by_role("combobox", name="选择提供商", exact=True)).to_be_visible()
                await screenshot(page, f"workspace-settings-left-nav-providers-{width}.png")
                await nav.get_by_role("tab", name="已归档", exact=True).click()
                await expect(dialog.get_by_role("tabpanel", name="已归档", exact=True)).to_be_visible()
                await expect(dialog.get_by_role("region", name="已归档对话", exact=True)).to_be_visible()
                await expect(nav.get_by_role("tab", name="已归档", exact=True)).to_have_attribute("aria-selected", "true")
                await screenshot(page, f"workspace-settings-left-nav-archives-{width}.png")
                layouts.append({"viewport": {"width": width, "height": height}, "nav": nav_box, "content": content_box})
                await nav.get_by_role("tab", name="模型提供商", exact=True).click()
                await assert_basics(page)
            if evidence := os.environ.get("ASTER_WEB_EVIDENCE"):
                Path(evidence, "workspace-settings-layout.json").write_text(json.dumps(layouts, indent=2), encoding="utf-8")
            await page.keyboard.press("Escape")
            await expect(dialog).to_have_count(0)
            await expect(draft).to_have_value("Keep draft while opening workspace settings")
            await expect(sidebar.get_by_role("button", name="设置", exact=True)).to_have_count(0)
            async with client_for(workbench_server) as api:
                assert (await api.get("/sessions")).json()["sessions"] == []
            assert receipts(workbench_server) == []
            assert not errors, errors
            assert_transport(page)
            await context.close()
        finally:
            await browser.close()


@pytest.mark.browser
async def test_workbench_v2_project_folder_crud_preserves_existing_session(workbench_server):
    first = Path(workbench_server["cwd"]) / "项目 one"
    second = Path(workbench_server["cwd"]) / "项目 two"
    first.mkdir()
    second.mkdir()
    protected = first / "user-owned.txt"
    protected.write_text("unchanged", encoding="utf-8")
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            context, page, errors = await open_workbench(browser, workbench_server)
            await page.get_by_role("button", name="创建项目", exact=True).click()
            await page.get_by_label("项目名称", exact=True).fill("研究项目")
            await page.get_by_label("本地文件夹", exact=False).fill(str(first))
            await screenshot(page, "v2-project-create.png")
            await page.get_by_role("button", name="浏览", exact=True).click()
            await expect(page.get_by_role("textbox", name="当前目录路径")).to_have_value(str(first.resolve()))
            await page.get_by_role("button", name="选择此文件夹", exact=True).click()
            await page.get_by_role("button", name="保存项目", exact=True).click()
            await expect(page.get_by_role("button", name="研究项目", exact=True)).to_be_visible()
            async with client_for(workbench_server) as api:
                assert (await api.get("/sessions")).json()["sessions"] == []
                project = (await api.get("/workbench/projects")).json()["projects"][0]
                await project_action(page, "研究项目", "在项目 研究项目 中新建对话")
                await send(page, "项目第一次对话")
                await expect(page.get_by_text("离线验收：你好，我是 Aster。聊天、事件和历史均来自当前临时 Runtime。", exact=True)).to_be_visible()
                old = (await api.get("/sessions")).json()["sessions"][0]
                assert old["cwd"] == str(first.resolve()) and old["project_id"] == project["id"]
                history = (await api.get(f"/sessions/{old['id']}")).json()["messages"]
                await page.get_by_role("button", name="管理项目 研究项目", exact=True).click()
                await page.get_by_role("button", name="编辑项目", exact=True).click()
                await page.get_by_label("项目名称", exact=True).fill("新项目名")
                await page.get_by_label("本地文件夹", exact=False).fill(str(second))
                await page.get_by_role("button", name="保存项目", exact=True).click()
                await expect(page.get_by_role("button", name="新项目名", exact=True)).to_be_visible()
                await project_action(page, "新项目名", "在项目 新项目名 中新建对话")
                await send(page, "项目第二次对话")
                await expect(page.get_by_text("离线验收：你好，我是 Aster。聊天、事件和历史均来自当前临时 Runtime。", exact=True)).to_be_visible()
                sessions = (await api.get("/sessions")).json()["sessions"]
                assert len(sessions) == 2
                assert next(row for row in sessions if row["id"] != old["id"])["cwd"] == str(second.resolve())
                await screenshot(page, "v2-project-sidebar.png")
                await page.get_by_role("button", name="管理项目 新项目名", exact=True).click()
                await page.get_by_role("button", name="删除项目", exact=True).click()
                await page.get_by_role("button", name="确认删除", exact=True).click()
                await expect(page.get_by_role("button", name="管理项目 新项目名", exact=True)).to_have_count(0)
                preserved = (await api.get(f"/sessions/{old['id']}")).json()
                assert preserved["project_id"] == project["id"] and preserved["cwd"] == str(first.resolve())
                assert preserved["messages"] == history and protected.read_text() == "unchanged"
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.get_by_role("button", name="打开导航", exact=True).click()
            await page.get_by_role("button", name="创建项目", exact=True).click()
            await expect(page.get_by_role("heading", name="创建项目", exact=True)).to_be_in_viewport()
            await expect(page.get_by_role("button", name="关闭", exact=True)).to_be_in_viewport()
            await screenshot(page, "v2-narrow-project-dialog.png")
            await page.get_by_role("button", name="取消", exact=True).click()
            assert not errors
            await assert_basics(page)
            assert_transport(page)
            await context.close()
        finally:
            await browser.close()


@pytest.mark.browser
async def test_workbench_v2_provider_key_model_effort_and_one_assistant_group(workbench_server):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            context, page, errors = await open_workbench(browser, workbench_server)
            await page.get_by_role("button", name="更多输入选项", exact=True).click()
            await page.get_by_role("button", name=re.compile("选择 Skill")).click()
            await expect(page.get_by_role("button", name=re.compile("qa-selected-skill"))).to_be_visible()
            await page.get_by_role("button", name=re.compile("不指定 Skill")).click()
            async with client_for(workbench_server) as api:
                assert (await api.get("/sessions")).json()["sessions"] == []
            await screenshot(page, "permanent-lifecycle-settings-entry.png")
            await open_settings(page)
            await page.get_by_role("button", name="添加提供商", exact=True).click()
            await page.get_by_label("显示名称", exact=True).fill("离线模型服务")
            await page.get_by_role("combobox", name=re.compile("API 协议")).select_option("openai_responses")
            await page.get_by_label("API 地址", exact=True).fill("https://offline-workbench.invalid/v1")
            key_input = page.locator('input[name="provider-key-write-only"]')
            await key_input.fill("synthetic-qa-provider-key")
            await page.get_by_role("textbox", name="手动添加模型 ID", exact=True).fill("qa-manual-model")
            await page.get_by_role("textbox", name="模型显示名称", exact=True).fill("手动验收模型")
            await page.get_by_role("button", name="添加模型到目录", exact=True).click()
            await page.get_by_text("模型能力", exact=True).click()
            await page.get_by_label("high", exact=True).check()
            await page.get_by_label("支持图片输入", exact=True).check()
            await page.get_by_role("button", name="保存提供商", exact=True).click()
            await expect(page.get_by_text("已保存。后续运行将使用新的配置。", exact=True)).to_be_visible()
            await expect(key_input).to_have_value("")
            await page.get_by_text("模型能力", exact=True).click()
            await page.get_by_role("button", name="保存提供商", exact=True).scroll_into_view_if_needed()
            await expect(page.get_by_role("heading", name="设置", exact=True)).to_be_in_viewport()
            await expect(page.get_by_role("button", name="关闭", exact=True)).to_be_in_viewport()
            await screenshot(page, "v2-provider-settings.png")
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.get_by_role("button", name="保存提供商", exact=True).scroll_into_view_if_needed()
            await expect(page.get_by_role("heading", name="设置", exact=True)).to_be_in_viewport()
            await expect(page.get_by_role("button", name="关闭", exact=True)).to_be_in_viewport()
            await assert_basics(page)
            await screenshot(page, "v2-narrow-provider-settings.png")
            await page.set_viewport_size({"width": 1440, "height": 1000})
            async with client_for(workbench_server) as api:
                profiles = (await api.get("/providers")).json()["providers"]
                provider = next(p for p in profiles if p["name"] == "离线模型服务")
                assert provider["configured"] and "synthetic-qa-provider-key" not in json.dumps(profiles)
                assert provider["models"][0]["effort_levels"] == ["high"]
                await page.get_by_role("button", name="关闭", exact=True).click()
                await page.get_by_role("button", name="选择模型", exact=True).click()
                await page.get_by_role("button", name=re.compile("手动验收模型")).click()
                await page.get_by_role("button", name="选择推理强度", exact=True).click()
                await page.get_by_role("button", name="高", exact=True).click()
                await page.get_by_role("button", name="更多输入选项", exact=True).click()
                await screenshot(page, "v2-composer-menu.png")
                await page.get_by_role("textbox", name="输入消息").click()
                await send(page, "测试审批")
                await expect(page.get_by_role("heading", name="允许这次操作吗？")).to_be_visible()
                await page.get_by_role("button", name="允许这一次", exact=True).click()
                await expect(page.get_by_text("离线验收：报告已生成，可以下载或打开静态预览。", exact=True)).to_be_visible()
                sessions = (await api.get("/sessions")).json()["sessions"]
                detail = (await api.get(f"/sessions/{sessions[0]['id']}")).json()
                assert detail["selection"]["provider_id"] == provider["id"]
                assert detail["selection"]["model"] == "qa-manual-model"
                assert detail["selection"]["reasoning_effort"] == "high"
                with closing(sqlite3.connect(Path(workbench_server["data_dir"]) / "runtime.db")) as db:
                    config = json.loads(db.execute("SELECT config_json FROM runs WHERE id=?", (detail["runs"][0]["id"],)).fetchone()[0])
                    payload = json.loads(db.execute("SELECT payload_json FROM run_requests WHERE run_id=?", (detail["runs"][0]["id"],)).fetchone()[0])
                assert "qa-manual-model" in json.dumps(config)
                assert payload["selection"]["model"] == "qa-manual-model" and payload["selection"]["reasoning_effort"] == "high"
                actual = receipts(workbench_server)
                assert len(actual) >= 4 and all(r["model"] == "qa-manual-model" and r["reasoning_effort"] == "high" and r["provider_id"] == provider["id"] for r in actual)
                assistant = page.get_by_test_id("message-assistant")
                await expect(assistant).to_have_count(1)
                assert await assistant.locator(".message-avatar").count() == 1
                assert await assistant.locator("details.tool-group").count() == 3
                assert sum(row["message"]["role"] == "assistant" for row in detail["messages"]) == 4
                await screenshot(page, "v2-grouped-conversation.png")
                await open_settings(page)
                await page.get_by_role("combobox", name="选择提供商", exact=True).select_option(label="离线模型服务")
                await page.get_by_role("button", name="获取模型", exact=True).click()
                await expect(page.get_by_text("已读取并更新供应商返回的模型目录。", exact=True)).to_be_visible()
                assert (await api.get("/providers")).json()["providers"][-1]["models"][0]["id"] == "qa-catalog-model"
                await page.get_by_role("button", name="移除保存的密钥", exact=True).click()
                await expect(page.get_by_text("已移除保存的密钥。", exact=True)).to_be_visible()
                assert not next(p for p in (await api.get("/providers")).json()["providers"] if p["id"] == provider["id"])["configured"]
            assert not errors
            await assert_basics(page)
            assert_transport(page)
            await context.close()
        finally:
            await browser.close()


@pytest.mark.browser
async def test_workbench_v2_manual_compact_and_installed_skill_narrow(workbench_server):
    async with client_for(workbench_server) as api:
        session = await create_session(api, workbench_server, "长对话与技能")
        for index in range(8):
            run = (await api.post(f"/sessions/{session['id']}/runs", json={"text": f"测试长对话 {index}"})).json()
            completed = await wait_run(api, run["id"])
            assert completed["status"] == "completed", {"error": completed.get("error"), "answer": completed.get("answer"), "index": index}
        before = (await api.get(f"/sessions/{session['id']}")).json()["messages"]
        calls = len(receipts(workbench_server))
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                context, page, errors = await open_workbench(browser, workbench_server, width=390, height=844)
                await page.get_by_role("button", name="打开导航", exact=True).click()
                await page.get_by_role("button", name="长对话与技能", exact=True).click()
                await expect(page.get_by_test_id("message-user")).to_have_count(8)
                await page.get_by_role("button", name="更多输入选项", exact=True).click()
                await screenshot(page, "v2-narrow-composer.png")
                async with page.expect_response(lambda response: response.url.endswith("/compact")) as response:
                    await page.get_by_role("button", name=re.compile("压缩对话")).click()
                compact = await (await response.value).json()
                assert compact["changed"] and compact["after_tokens"] < compact["before_tokens"] and compact["preserved_archive"]
                await expect(page.get_by_role("status").filter(has_text="已在本地整理")).to_be_visible()
                assert len(receipts(workbench_server)) == calls
                assert (await api.get(f"/sessions/{session['id']}")).json()["messages"] == before
                await page.get_by_role("button", name="更多输入选项", exact=True).click()
                await page.get_by_role("button", name=re.compile("选择 Skill")).click()
                await page.get_by_role("button", name=re.compile("qa-selected-skill")).click()
                async with page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/runs")) as submitted:
                    await send(page, "测试技能权限")
                accepted = await (await submitted.value).json()
                finished = await wait_run(api, accepted["id"])
                assert finished["status"] == "completed", {"status": finished["status"], "error": finished.get("error")}
                assert finished["answer"] == "离线验收：已执行所选技能的权限检查。"
                completed_history = (await api.get(f"/sessions/{session['id']}")).json()["messages"]
                assert completed_history[-1]["message"]["content"] == finished["answer"]
                await expect(page.get_by_text("离线验收：已执行所选技能的权限检查。", exact=True)).to_be_visible()
                await expect(page.get_by_text("离线验收：已执行所选技能的权限检查。", exact=True)).to_be_in_viewport()
                await expect(page.locator(".topbar")).to_be_in_viewport(ratio=1)
                layout = await page.locator(".topbar").evaluate("""el => {
                    const r = el.getBoundingClientRect();
                    return {topbar:{x:r.x,y:r.y,width:r.width,height:r.height,bottom:r.bottom},
                            viewport:{width:innerWidth,height:innerHeight},documentScrollTop:document.scrollingElement.scrollTop};
                }""")
                assert layout["topbar"]["y"] >= 0 and layout["topbar"]["bottom"] <= layout["viewport"]["height"]
                assert layout["documentScrollTop"] == 0
                if evidence := os.environ.get("ASTER_WEB_EVIDENCE"):
                    Path(evidence, "v2-narrow-viewport.json").write_text(json.dumps(layout, indent=2), encoding="utf-8")
                await screenshot(page, "v2-narrow-viewport-verified.png")
                actual = receipts(workbench_server)[calls:]
                assert actual and all(row["skill_instruction_seen"] for row in actual)
                detail = (await api.get(f"/sessions/{session['id']}")).json()
                blocked = next(row["message"] for row in detail["messages"] if row["message"].get("tool_call_id") == "skill-denied")
                assert blocked["metadata"]["is_error"] and "skill" in blocked["content"].lower()
                assert not (Path(workbench_server["cwd"]) / "skill-must-not-write.txt").exists()
                await screenshot(page, "v2-narrow-skill-complete.png")
                await assert_basics(page)
                assert_transport(page)
                assert not errors
                await context.close()
            finally:
                await browser.close()
