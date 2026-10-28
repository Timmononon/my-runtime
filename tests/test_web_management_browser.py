"""Management mutations through real Chromium, HTTP and the shared Runtime."""
import re
from pathlib import Path

import pytest
from playwright.async_api import async_playwright, expect
from test_web_workbench import client_for
from test_web_workbench import workbench_server as workbench_server  # noqa: PLC0414 - pytest fixture re-export
from test_web_workbench_browser import open_workbench, screenshot


async def navigate(page, name):
    await page.get_by_role("navigation", name="主导航").get_by_role("button", name=name).click()


async def assert_no_model_requests(api):
    events = (await api.get("/events?limit=1000")).json()["events"]
    assert not [event for event in events if event["type"] == "model.request"]
    sessions = (await api.get("/sessions?include_side_chats=true")).json()["sessions"]
    for session in sessions:
        for row in (await api.get(f"/sessions/{session['id']}")).json()["runs"]:
            run = (await api.get(f"/runs/{row['id']}")).json()
            assert run["usage"] == []
            assert run["budget"] is None or run["budget"]["model_requests"] == 0


@pytest.mark.browser
async def test_workbench_skill_preview_exact_digest_install_and_uninstall(workbench_server):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context, page, errors = await open_workbench(browser, workbench_server)
            async with client_for(workbench_server) as api:
                catalogue = (await api.get("/customization/skills")).json()
                initial = catalogue["installed"]
                expected = next(item for item in catalogue["catalog"] if item["name"] == "webapp-testing")
                await navigate(page, "Skills")
                card = page.locator("article.skill-card").filter(has=page.get_by_role("heading", name="webapp-testing", exact=True))
                await card.get_by_role("button", name="查看并安装").click()
                dialog = page.get_by_role("dialog", name="检查安装内容")
                await expect(dialog).to_be_visible()
                await expect(dialog.locator(".digest")).to_have_text(expected["digest"])
                await expect(dialog.get_by_text("SKILL.md", exact=True)).to_be_visible()
                assert (await api.get("/customization/skills")).json()["installed"] == initial
                async with page.expect_request(lambda req: req.method == "POST" and req.url.endswith("/skill_package_install")) as sent:
                    await dialog.get_by_role("button", name="确认安装", exact=True).click()
                payload = (await sent.value).post_data_json
                assert payload["approved"] is True and payload["arguments"]["expected_digest"] == expected["digest"]
                await expect(card.locator(".installed-tag")).to_have_text("已安装")
                installed = (await api.get("/customization/skills")).json()["installed"]
                added = [item for item in installed if item["name"] == "webapp-testing"]
                assert len(added) == 1 and added[0]["digest"] == expected["digest"]
                assert [item for item in installed if item["name"] != "webapp-testing"] == initial
                target = Path(workbench_server["data_dir"]) / "skills/webapp-testing/SKILL.md"
                assert target.is_file()
                await screenshot(page, "desktop-skills-installed.png")
                await card.get_by_role("button", name="卸载 webapp-testing", exact=True).click()
                await page.get_by_role("dialog", name="卸载 Skill").get_by_role("button", name="确认卸载", exact=True).click()
                await expect(card.get_by_role("button", name="查看并安装")).to_be_visible()
                assert (await api.get("/customization/skills")).json()["installed"] == initial and not target.exists()
                sessions = (await api.get("/sessions")).json()["sessions"]
                assert len(sessions) == 1
                runs = (await api.get(f"/sessions/{sessions[0]['id']}")).json()["runs"]
                journaled = [(await api.get(f"/runs/{run['id']}")).json()["tool_executions"][0] for run in runs]
                assert {item["tool_name"] for item in journaled} == {"skill_package_preview", "skill_package_install", "skill_package_uninstall"}
                assert all(item["state"] in {"committed", "checkpointed"} and not item["result"]["is_error"] for item in journaled)
                await assert_no_model_requests(api)
            assert errors == []
            await context.close()
        finally:
            await browser.close()


@pytest.mark.browser
async def test_workbench_memory_record_update_history_and_forget(workbench_server):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context, page, errors = await open_workbench(browser, workbench_server)
            async with client_for(workbench_server) as api:
                await navigate(page, "记忆")
                await page.get_by_role("button", name="记录事实", exact=True).click()
                form = page.get_by_role("dialog", name="记录一条事实")
                await form.get_by_label("实体", exact=True).fill("验收项目")
                await form.get_by_label("关系", exact=True).fill("负责人")
                await form.get_by_label("内容", exact=True).fill("Mina")
                await form.get_by_label("来源陈述", exact=True).fill("操作员提供：负责人是 Mina。")
                await form.get_by_role("button", name="确认记录", exact=True).click()
                await expect(form).not_to_be_visible()
                sid = (await api.get("/sessions")).json()["sessions"][0]["id"]
                first = (await api.get(f"/customization/sessions/{sid}/memory?scope=agent")).json()["facts"][0]
                assert first["value"] == "Mina" and first["evidence"][0]["verified"] is False
                await expect(page.locator(".fact-value")).to_have_text("Mina")
                async with page.expect_response(lambda response: "/memory?" in response.url and "scope=project" in response.url) as rejected_scope:
                    await page.get_by_role("combobox", name="记忆作用域").select_option("project")
                assert (await rejected_scope.value).status == 422
                await expect(page.get_by_role("alert")).to_be_visible()
                await expect(page.get_by_role("heading", name="这里还没有记录")).to_be_visible()
                await expect(page.locator(".fact-value")).to_have_count(0)
                await page.get_by_role("button", name="关闭错误提示", exact=True).click()
                await page.get_by_role("combobox", name="记忆作用域").select_option("agent")
                await expect(page.locator(".fact-value")).to_have_text("Mina")
                card = page.locator("article.memory-card")
                await card.get_by_role("button", name="更新", exact=True).click()
                form = page.get_by_role("dialog", name="更新这条事实")
                await screenshot(page, "memory-edit-open.png")
                assert errors == [], errors
                await expect(form).to_be_visible()
                await form.get_by_role("textbox", name=re.compile(r"^内容")).fill("Jun")
                await form.get_by_label("来源陈述", exact=True).fill("操作员更正：负责人改为 Jun。")
                await form.get_by_label("更新原因", exact=True).fill("项目交接已确认")
                await form.get_by_role("button", name="确认记录", exact=True).click()
                await expect(page.locator(".fact-value")).to_have_text("Jun")
                current = (await api.get(f"/customization/sessions/{sid}/memory?scope=agent")).json()["facts"][0]
                assert current["id"] != first["id"] and current["predecessor"] == first["id"]
                old_history = (await api.get(f"/customization/sessions/{sid}/memory/{first['id']}/history?scope=agent")).json()["revisions"]
                assert [r["action"] for r in old_history] == ["record", "supersede"]
                await card.get_by_role("button", name="修订历史", exact=True).click()
                history = page.get_by_role("dialog", name="可追溯的修订历史")
                await expect(history.get_by_text("项目交接已确认", exact=True)).to_be_visible()
                await screenshot(page, "desktop-memory-history.png")
                await history.get_by_role("button", name="关闭", exact=True).click()
                await card.get_by_role("button", name="撤回", exact=True).click()
                forget = page.get_by_role("dialog", name="撤回这条事实")
                await forget.get_by_label("撤回原因", exact=True).fill("项目结束，保留审计")
                await forget.get_by_role("button", name="确认撤回", exact=True).click()
                await expect(page.get_by_role("heading", name="这里还没有记录")).to_be_visible()
                assert (await api.get(f"/customization/sessions/{sid}/memory?scope=agent")).json()["facts"] == []
                revised = (await api.get(f"/customization/sessions/{sid}/memory/{current['id']}/history?scope=agent")).json()["revisions"]
                assert revised[-1]["action"] == "retract" and revised[-1]["snapshot"]["value"] == "Jun"
                await assert_no_model_requests(api)
            assert errors == []
            await context.close()
        finally:
            await browser.close()


@pytest.mark.browser
async def test_workbench_future_automation_create_pause_edit_preserves_policy_delete(workbench_server):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context, page, errors = await open_workbench(browser, workbench_server)
            async with client_for(workbench_server) as api:
                await navigate(page, "自动化")
                await page.get_by_role("button", name="创建自动化", exact=True).click()
                form = page.get_by_role("dialog", name="创建自动化")
                await form.get_by_label("希望 Aster 做什么", exact=True).fill("仅供验收的远期年度检查")
                await form.get_by_label("Cron 表达式").fill("0 0 1 1 *")
                await form.get_by_label("每天最多执行次数", exact=True).fill("2")
                await form.get_by_role("button", name="创建并启用", exact=True).click()
                card = page.locator("article.job-card")
                await expect(card.get_by_role("heading", name="仅供验收的远期年度检查", exact=True)).to_be_visible()
                job = (await api.get("/automations")).json()["jobs"][0]
                assert job["enabled"] and job["config"]["cron"] == "0 0 1 1 *"
                assert job["config"]["session_mode"] == "fresh"
                # Existing non-UI policy must survive a later prompt-only edit.
                policy = {"session_mode": "sticky", "overlap": "queue", "misfire": "bounded_catchup"}
                assert (await api.patch(f"/automations/{job['id']}", json={"config": policy})).status_code == 200
                await card.get_by_role("button", name="暂停任务", exact=True).click()
                await expect(card.locator(".job-state")).to_have_text("已暂停")
                await card.get_by_role("button", name="编辑任务", exact=True).click()
                form = page.get_by_role("dialog", name="编辑自动化")
                await screenshot(page, "automation-edit-open.png")
                assert errors == [], errors
                await expect(form).to_be_visible()
                await form.get_by_role("textbox", name=re.compile(r"^希望 Aster 做什么")).fill("年度验收描述已更新")
                await form.get_by_role("button", name="保存修改", exact=True).click()
                await expect(card.get_by_role("heading", name="年度验收描述已更新", exact=True)).to_be_visible()
                saved = (await api.get("/automations")).json()["jobs"][0]
                assert saved["enabled"] is False
                assert {key: saved["config"][key] for key in policy} == policy
                assert saved["config"]["cron"] == "0 0 1 1 *" and saved["config"]["max_runs_per_day"] == 2
                await screenshot(page, "desktop-automation-paused.png")
                await card.get_by_role("button", name="删除任务", exact=True).click()
                await page.get_by_role("dialog", name="删除自动化").get_by_role("button", name="确认删除", exact=True).click()
                await expect(page.get_by_role("heading", name="少一点重复，多一点专注。", exact=True)).to_be_visible()
                assert (await api.get("/automations")).json()["jobs"] == []
                await assert_no_model_requests(api)
            assert errors == []
            await context.close()
        finally:
            await browser.close()
