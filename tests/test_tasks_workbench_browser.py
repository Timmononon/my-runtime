"""Local task board acceptance uses the real temporary Runtime and scripted models."""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest
from playwright.async_api import async_playwright, expect
from test_web_workbench import client_for
from test_web_workbench import workbench_server as workbench_server  # noqa: PLC0414
from test_web_workbench_browser import assert_basics, open_workbench, screenshot


async def open_tasks(page):
    toggle = page.get_by_role("button", name="打开导航", exact=True)
    if await toggle.is_visible():
        await toggle.click()
    await page.get_by_role("button", name="任务", exact=True).click()
    await expect(page.get_by_role("heading", name="让每项工作都有着落。", exact=True)).to_be_visible()


async def create_task(page, *, title, workers):
    await page.get_by_role("button", name="创建任务", exact=True).click()
    dialog = page.get_by_role("dialog", name="创建任务", exact=True)
    await dialog.get_by_label("任务名称", exact=True).fill(title)
    await dialog.get_by_label("总目标与交付要求", exact=True).fill("离线验收；按分配工作独立执行并保留结果。")
    for index, (name, goal) in enumerate(workers):
        if index:
            await dialog.get_by_role("button", name="增加 Worker", exact=True).click()
        fieldset = dialog.get_by_role("group", name=f"Worker {index + 1}", exact=True)
        await fieldset.get_by_label("工作名称", exact=True).fill(name)
        await fieldset.get_by_label("独立工作目标", exact=True).fill(goal)
    await dialog.get_by_role("button", name="创建并开始", exact=True).click()
    await expect(dialog).not_to_be_visible()
    await expect(page.get_by_role("region", name="任务详情").get_by_role("heading", name=title, exact=True)).to_be_visible()


async def wait_ticket(server, *, title, state):
    async with client_for(server) as api, asyncio.timeout(20):
        while True:
            response = await api.get("/tasks")
            assert response.status_code == 200, response.text
            task = next((item for item in response.json()["tickets"] if item["title"] == title), None)
            if task and task["state"] == state:
                return task
            await asyncio.sleep(0.08)


@pytest.mark.browser
async def test_tasks_desktop_parallel_results_followup_and_worker_navigation(workbench_server):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context, page, errors = await open_workbench(browser, workbench_server)
            await open_tasks(page)
            await create_task(page, title="并行检查资料", workers=[("检查甲", "检查资料甲并给出结果"), ("检查乙", "检查资料乙并给出结果")])
            task = await wait_ticket(workbench_server, title="并行检查资料", state="completed")
            assert len({worker["session_id"] for worker in task["workers"]}) == 2
            assert len({worker["cwd"] for worker in task["workers"]}) == 2
            detail = page.get_by_role("region", name="任务详情")
            await expect(detail.get_by_role("heading", name="结果汇总", exact=True)).to_be_visible(timeout=15000)
            await expect(detail.locator(".task-worker")).to_have_count(2)
            await detail.get_by_label("补充任务要求", exact=True).fill("补充核验原来的结果并报告。")
            await detail.get_by_role("button", name="发送补充", exact=True).click()
            await expect(detail.get_by_label("补充任务要求", exact=True)).to_have_value("")
            await wait_ticket(workbench_server, title="并行检查资料", state="completed")
            # The Runtime can finish between the independent overview/detail
            # polls. Verify both rendered snapshots converge before evidence.
            await expect(detail.locator(".task-detail-title > .task-status")).to_have_text("已完成", timeout=15000)
            await expect(detail.get_by_role("heading", name="结果汇总", exact=True)).to_be_visible()
            await expect(page.locator(".task-row.selected .task-status")).to_have_text("已完成", timeout=15000)
            await expect(page.locator(".task-stats > div").nth(1).locator("strong")).to_have_text("0")
            await expect(page.locator(".task-stats > div").nth(2).locator("strong")).to_have_text("1")
            await assert_basics(page)
            await screenshot(page, "tasks-desktop-results.png")
            await page.locator(".task-board-scroll").evaluate("element => element.scrollTo(0, 0)")
            await screenshot(page, "tasks-desktop-overview.png")
            await detail.get_by_role("button", name="打开工作对话", exact=True).first.click()
            await expect(page.locator(".conversation-scroll")).to_be_visible()
            await expect(page.get_by_text("这是任务的工作对话", exact=True)).to_be_visible()
            await expect(page.get_by_role("textbox", name="输入消息", exact=True)).to_be_hidden()
            await page.get_by_role("button", name="返回所属任务", exact=True).click()
            await expect(page.get_by_role("region", name="任务详情").get_by_role("heading", name="并行检查资料", exact=True)).to_be_visible()
            assert not errors
            await context.close()
        finally:
            await browser.close()


@pytest.mark.browser
async def test_tasks_narrow_real_approvals_questions_and_cancellation(workbench_server):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context, page, errors = await open_workbench(browser, workbench_server, width=390, height=844, has_touch=True)
            await open_tasks(page)
            await create_task(page, title="等待人工处理", workers=[("生成报告", "测试审批"), ("确认语言", "测试提问")])
            detail = page.get_by_role("region", name="任务详情")
            await expect(detail.get_by_role("button", name="允许这一次", exact=True)).to_be_visible(timeout=15000)
            await expect(detail.get_by_role("heading", name="这份报告使用哪种语言？", exact=True)).to_be_visible()
            await screenshot(page, "tasks-narrow-attention.png")
            await detail.get_by_role("button", name="允许这一次", exact=True).click()
            await detail.get_by_role("button", name=re.compile(r"^中文")).click()
            await detail.get_by_role("button", name="确认回答", exact=True).click()
            task = await wait_ticket(workbench_server, title="等待人工处理", state="completed")
            report_worker = next(worker for worker in task["workers"] if worker["title"] == "生成报告")
            assert list(Path(report_worker["cwd"]).glob("workbench-report-*.html"))
            await expect(detail.get_by_role("heading", name="结果汇总", exact=True)).to_be_visible(timeout=15000)
            await assert_basics(page)
            await create_task(page, title="可取消的工作", workers=[("等待工作", "测试取消")])
            await wait_ticket(workbench_server, title="可取消的工作", state="working")
            cancel_attempts = []

            async def version_conflict_once(route):
                cancel_attempts.append(route.request.url)
                if len(cancel_attempts) == 1:
                    await route.fulfill(status=409, content_type="application/json", body=json.dumps({"error": "state_conflict", "detail": "Task changed; reload before changing it"}))
                else:
                    await route.continue_()

            await page.route("**/tasks/*/cancel", version_conflict_once)
            await detail.get_by_role("button", name="取消任务", exact=True).click()
            async with page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/cancel") and response.status == 200) as cancellation:
                await page.get_by_role("dialog", name="取消任务", exact=True).get_by_role("button", name="确认取消", exact=True).click()
            response = await cancellation.value
            assert response.status == 200, await response.text()
            await wait_ticket(workbench_server, title="可取消的工作", state="cancelled")
            assert len(cancel_attempts) == 2
            await expect(detail.get_by_role("button", name="发送补充", exact=True)).to_have_count(0)
            await assert_basics(page)
            assert not errors
            await context.close()
        finally:
            await browser.close()


@pytest.mark.browser
async def test_tasks_observe_rules_and_read_error_recovery_do_not_call_model(workbench_server):
    async with client_for(workbench_server) as api, async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context, page, errors = await open_workbench(browser, workbench_server)

            async def unavailable(route):
                await route.fulfill(status=503, content_type="application/json", body=json.dumps({"error": "fixture_read_unavailable"}))

            await page.route("**/tasks/overview", unavailable)
            await open_tasks(page)
            await expect(page.get_by_role("alert")).to_contain_text("fixture_read_unavailable")
            await page.unroute("**/tasks/overview", unavailable)
            await page.get_by_role("button", name="重新读取", exact=True).click()
            await page.get_by_role("button", name="接单规则", exact=True).click()
            policy = page.get_by_role("dialog", name="接单规则", exact=True)
            await policy.get_by_role("button", name="增加规则", exact=True).click()
            await policy.get_by_label("规则标识", exact=True).fill("offline-report")
            await policy.get_by_label("匹配词（每行一个）", exact=True).fill("检查\n报告")
            await policy.get_by_role("button", name="保存规则", exact=True).click()
            await expect(policy).not_to_be_visible()
            await page.get_by_role("button", name="接单规则", exact=True).click()
            policy = page.get_by_role("dialog", name="接单规则", exact=True)
            await expect(policy.get_by_role("combobox", name="主动接单方式", exact=True)).to_have_value("observe")
            await policy.locator(".task-candidate-test > summary").click()
            await policy.get_by_label("候选文本", exact=True).fill("检查刚生成的报告")
            await policy.get_by_role("button", name="按已保存规则评估", exact=True).click()
            await expect(policy.get_by_role("status")).to_contain_text("已记录观察结果")
            assert (await api.get("/tasks")).json()["tickets"] == []
            receipts = Path(workbench_server["data_dir"]) / "fixture-model-calls.jsonl"
            assert not receipts.exists() or not receipts.read_text(encoding="utf-8").strip()
            await screenshot(page, "tasks-observe-policy.png")
            assert not errors
            await context.close()
        finally:
            await browser.close()
