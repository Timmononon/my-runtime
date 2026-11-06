"""Real Chromium GitHub workflows with real Runtime and fixed-origin MockTransport."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest
from playwright.async_api import async_playwright, expect
from test_web_workbench import client_for
from test_web_workbench_browser import assert_basics, open_workbench, screenshot


@pytest.fixture
def github_workbench_server(tmp_path):
    ready, stop, log = (tmp_path / name for name in ("ready.json", "stop", "fixture.log"))
    root = Path(__file__).resolve().parents[1]
    environment = {key: value for key, value in os.environ.items() if key not in {"DEEPSEEK_API_KEY", "ASTER_API_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"}}
    environment["PYTHONIOENCODING"] = "utf-8"
    with log.open("w", encoding="utf-8") as output:
        process = subprocess.Popen([sys.executable, str(root / "scripts/github_web_fixture.py"), "--ready-file", str(ready), "--stop-file", str(stop)],
                                   cwd=root, env=environment, stdout=output, stderr=subprocess.STDOUT,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            deadline = time.monotonic() + 20
            while not ready.exists():
                assert process.poll() is None, log.read_text(encoding="utf-8")
                assert time.monotonic() < deadline, "Offline GitHub fixture did not start"
                time.sleep(0.05)
            value = json.loads(ready.read_text(encoding="utf-8"))
            yield value
        finally:
            stop.touch()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=5)
            assert process.returncode == 0, log.read_text(encoding="utf-8")
            assert not Path(value["data_dir"]).exists() if "value" in locals() else True


async def open_github(page):
    toggle = page.get_by_role("button", name="打开导航", exact=True)
    if await toggle.is_visible():
        await toggle.click()
    await page.locator("nav[aria-label='主导航']").get_by_role("button", name="GitHub", exact=True).click()
    await expect(page.get_by_role("heading", name="GitHub", exact=True)).to_be_visible()


async def create_source(page, *, token=""):
    await page.get_by_role("button", name="添加仓库来源", exact=True).click()
    dialog = page.get_by_role("dialog", name="添加 GitHub 来源", exact=True)
    await expect(dialog.get_by_label("启用后台轮询", exact=True)).not_to_be_checked()
    await expect(dialog.get_by_label("允许审阅后发送评论或 PR", exact=True)).not_to_be_checked()
    await expect(dialog.get_by_role("combobox", name="来源模式", exact=True)).to_have_value("read_only")
    await dialog.get_by_role("textbox", name="GitHub 仓库", exact=True).fill("fixture/repo")
    if token:
        await dialog.get_by_label("GitHub Token", exact=True).fill(token)
    await dialog.get_by_role("button", name="保存来源", exact=True).click()
    await expect(dialog).not_to_be_visible()
    await expect(page.get_by_role("region", name="GitHub 来源详情").get_by_role("heading", name="fixture/repo", exact=True)).to_be_visible()


async def enable_reviewed_writes(page):
    detail = page.get_by_role("region", name="GitHub 来源详情")
    await detail.get_by_role("button", name="配置来源", exact=True).click()
    dialog = page.get_by_role("dialog", name="配置 GitHub 来源", exact=True)
    await expect(dialog.get_by_label("GitHub Token", exact=True)).to_have_value("")
    await dialog.get_by_role("combobox", name="来源模式", exact=True).select_option("tasks")
    await dialog.get_by_label("启用后台轮询", exact=True).check()
    await dialog.get_by_label("允许审阅后发送评论或 PR", exact=True).check()
    await dialog.get_by_role("button", name="保存来源", exact=True).click()
    await expect(dialog).not_to_be_visible()
    await detail.get_by_role("button", name="检查连接", exact=True).click()
    await expect(page.get_by_role("status")).to_contain_text("当前账号 fixture-user")


async def prepare_comment(page, text):
    detail = page.get_by_role("region", name="GitHub 来源详情")
    await detail.get_by_role("button", name=re.compile(r"^待审阅 ")).click()
    await detail.get_by_role("button", name="准备草稿", exact=True).click()
    dialog = page.get_by_role("dialog", name="准备 GitHub 草稿", exact=True)
    await dialog.get_by_label("Issue／PR 编号", exact=True).fill("7")
    await dialog.get_by_label("草稿正文", exact=True).fill(text)
    await dialog.get_by_role("button", name="只保存草稿", exact=True).click()
    await expect(dialog).not_to_be_visible()
    await expect(detail.locator(".github-draft")).to_have_count(1)


@pytest.mark.browser
async def test_github_desktop_anonymous_read_only_baseline_local_reads_and_drafts(github_workbench_server):
    async with client_for(github_workbench_server) as api, async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context, page, errors = await open_workbench(browser, github_workbench_server)
            await open_github(page)
            await create_source(page)
            source = (await api.get("/github/sources")).json()["sources"][0]
            assert source["enabled"] is False and source["mode"] == "read_only" and source["allow_writes"] is False
            assert (await api.get("/fixture/github/state")).json()["requests"] == []
            detail = page.get_by_role("region", name="GitHub 来源详情")
            await expect(detail.get_by_text("匿名读取 · 未保存令牌", exact=True)).to_be_visible()
            await detail.get_by_role("button", name="检查连接", exact=True).click()
            await expect(page.get_by_role("status")).to_contain_text("匿名读取公有仓库")
            await detail.get_by_role("button", name="只读抓取", exact=True).click()
            await expect(page.get_by_role("status")).to_contain_text("没有启动模型任务")
            await expect(detail.locator(".github-event").first).to_be_visible()
            before = (await api.get("/fixture/github/state")).json()
            assert before["model_calls"] == 0 and before["remote_writes"] == 0
            assert all(item["method"] == "GET" for item in before["requests"])
            await page.get_by_role("button", name="刷新 GitHub 本地状态", exact=True).click()
            after = (await api.get("/fixture/github/state")).json()
            assert after["requests"] == before["requests"]
            await assert_basics(page)
            await page.locator(".github-scroll").evaluate("element => element.scrollTo(0, 0)")
            await screenshot(page, "github-desktop-readonly.png")
            await prepare_comment(page, "仅保存在本机的草稿，不发送到 GitHub。")
            await expect(detail.get_by_role("button", name="审阅并发送", exact=True)).to_have_count(0)
            assert (await api.get("/fixture/github/state")).json()["remote_writes"] == 0
            assert not errors
            await context.close()
        finally:
            await browser.close()


@pytest.mark.browser
async def test_github_independent_triage_settings_conflict_and_visible_decisions(github_workbench_server):
    async with client_for(github_workbench_server) as api, async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context, page, errors = await open_workbench(browser, github_workbench_server)
            await open_github(page)
            await page.get_by_role("button", name="任务判断模型", exact=True).click()
            settings = page.get_by_role("dialog", name="任务判断模型", exact=True)
            await expect(settings.get_by_role("combobox", name="判断模型提供商", exact=True)).to_have_value("deepseek")
            await expect(settings.get_by_role("combobox", name="判断模型", exact=True)).to_have_value("deepseek-flash")
            before = (await api.get("/fixture/github/state")).json()
            saved = (await api.get("/tasks/triage/settings")).json()
            updated = await api.patch("/tasks/triage/settings", json={"expected_revision": saved["revision"],
                "processing_scope": "其他窗口已保存的范围"})
            assert updated.status_code == 200, updated.text
            await settings.get_by_label("自动处理范围", exact=True).fill("旧窗口编辑的范围")
            await settings.get_by_role("button", name="保存判断模型", exact=True).click()
            await expect(settings.get_by_role("alert")).to_be_visible()
            await expect(settings.get_by_role("button", name="保存判断模型", exact=True)).to_be_disabled()
            await settings.get_by_role("button", name="重新读取已保存配置", exact=True).click()
            await expect(settings.get_by_label("自动处理范围", exact=True)).to_have_value("其他窗口已保存的范围")
            await settings.get_by_label("自动处理范围", exact=True).fill("处理本仓库的缺陷检查与修复，需求细节由 Worker 调查。")
            await screenshot(page, "github-triage-model-settings.png")
            await settings.get_by_role("button", name="保存判断模型", exact=True).click()
            await expect(settings).not_to_be_visible()
            after = (await api.get("/fixture/github/state")).json()
            assert before["requests"] == after["requests"] == []
            assert before["triage_calls"] == after["triage_calls"] == 0
            assert after["model_calls"] == after["remote_writes"] == 0

            await create_source(page)
            detail = page.get_by_role("region", name="GitHub 来源详情")
            await detail.get_by_role("button", name="只读抓取", exact=True).click()
            await expect(page.get_by_role("status")).to_contain_text("没有启动模型任务")
            await detail.get_by_role("button", name="配置来源", exact=True).click()
            editor = page.get_by_role("dialog", name="配置 GitHub 来源", exact=True)
            await editor.get_by_role("combobox", name="来源模式", exact=True).select_option("tasks")
            await editor.get_by_label("启用后台轮询", exact=True).check()
            await editor.get_by_label(re.compile(r"^允许提出工作请求的账号")).fill("alice")
            await editor.get_by_label("机器人账号（识别 @ 提及，可选）", exact=True).fill("aster-bot")
            await editor.get_by_role("button", name="保存来源", exact=True).click()
            await expect(editor).not_to_be_visible()
            response = await api.post("/fixture/github/control", json={"issue_body": "@aster-bot 修复这个问题，但没有提供复现步骤。",
                "issue_updated": "2026-11-05T12:00:00Z", "triage_decision": "error"})
            assert response.status_code == 200
            await detail.get_by_role("button", name="抓取并按规则处理", exact=True).click()
            await expect(detail.get_by_text("模型判断：判断失败", exact=True)).to_be_visible()
            await expect(detail.get_by_text("Task triage unavailable; manual review required", exact=True)).to_be_visible()
            await expect(detail.get_by_text("模型判断：待补充", exact=True)).to_have_count(0)
            await detail.get_by_role("button", name="抓取并按规则处理", exact=True).click()
            state = (await api.get("/fixture/github/state")).json()
            assert state["triage_calls"] == 1 and state["model_calls"] == state["remote_writes"] == 0
            assert (await api.get("/tasks")).json()["tickets"] == []
            await api.post("/fixture/github/control", json={"issue_body": "@aster-bot 这只是补充说明，无需修改。",
                "issue_updated": "2026-11-05T12:01:00Z", "triage_decision": "ignore"})
            await detail.get_by_role("button", name="抓取并按规则处理", exact=True).click()
            await expect(detail.get_by_text("模型判断：忽略", exact=True)).to_be_visible()
            await expect(detail.get_by_text("该讨论无需仓库改动。", exact=True)).to_be_visible()
            state = (await api.get("/fixture/github/state")).json()
            assert state["triage_calls"] == 2 and state["model_calls"] == state["remote_writes"] == 0
            await assert_basics(page)
            await detail.locator(".task-triage-decision").last.scroll_into_view_if_needed()
            await screenshot(page, "github-triage-decisions.png")
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.get_by_role("button", name="任务判断模型", exact=True).click()
            await expect(settings.get_by_role("combobox", name="判断模型", exact=True)).to_have_value("deepseek-flash")
            await expect(settings.get_by_label("自动处理范围", exact=True)).to_have_value("处理本仓库的缺陷检查与修复，需求细节由 Worker 调查。")
            await assert_basics(page)
            await screenshot(page, "github-triage-model-settings-narrow.png")
            await settings.get_by_role("button", name="取消", exact=True).click()
            assert not errors
            await context.close()
        finally:
            await browser.close()


@pytest.mark.browser
async def test_github_narrow_write_only_token_edited_comment_and_reviewed_pr(github_workbench_server):
    synthetic = "ghp_offline_fixture_token_for_browser_only"
    async with client_for(github_workbench_server) as api, async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context, page, errors = await open_workbench(browser, github_workbench_server, width=390, height=844, has_touch=True)
            await open_github(page)
            await create_source(page, token=synthetic)
            assert synthetic not in (await api.get("/github/sources")).text
            assert synthetic not in await page.evaluate("JSON.stringify([Object.entries(localStorage),Object.entries(sessionStorage)])")
            await enable_reviewed_writes(page)
            configured = await api.post("/fixture/github/control", json={"require_publish_approval": True})
            assert configured.status_code == 200
            await prepare_comment(page, "初稿正文")
            detail = page.get_by_role("region", name="GitHub 来源详情")
            await detail.get_by_role("button", name="编辑草稿", exact=True).click()
            editor = page.get_by_role("dialog", name="编辑 GitHub 草稿", exact=True)
            await screenshot(page, "github-narrow-edit-diagnostic.png")
            assert not errors
            await expect(editor).to_be_visible()
            await editor.get_by_role("textbox", name="草稿正文", exact=True).fill("已审阅的最终评论正文。")
            await editor.get_by_role("button", name="只保存草稿", exact=True).click()
            await expect(editor).not_to_be_visible()
            assert (await api.get("/fixture/github/state")).json()["remote_writes"] == 0
            await detail.get_by_role("button", name="审阅并发送", exact=True).click()
            review = page.get_by_role("dialog", name="确认发送到 GitHub", exact=True)
            await expect(review.get_by_text("已审阅的最终评论正文。", exact=True)).to_be_visible()
            await expect(review.get_by_text("Issue／PR #7 的评论", exact=True)).to_be_visible()
            await screenshot(page, "github-narrow-review-comment.png")
            await review.get_by_role("button", name="确认发送这份草稿", exact=True).click()
            await expect(review).not_to_be_visible()
            await expect(detail.locator(".github-draft-state")).to_have_text("已发送")
            assert (await api.get("/fixture/github/state")).json()["remote_writes"] == 1
            await assert_basics(page)
            await detail.get_by_role("button", name="准备草稿", exact=True).click()
            editor = page.get_by_role("dialog", name="准备 GitHub 草稿", exact=True)
            await editor.get_by_role("combobox", name="草稿类型", exact=True).select_option("pull_request")
            await editor.get_by_label("PR 标题", exact=True).fill("离线审核的 PR")
            await editor.get_by_label("来源分支", exact=True).fill("feature")
            await editor.get_by_label("草稿正文", exact=True).fill("仅通过离线 Mock GitHub 创建的 PR。")
            await editor.get_by_role("button", name="只保存草稿", exact=True).click()
            await expect(editor).not_to_be_visible()
            pr_card = detail.locator(".github-draft").filter(has=page.get_by_role("heading", name="离线审核的 PR", exact=True))
            await pr_card.get_by_role("button", name="审阅并发送", exact=True).click()
            review = page.get_by_role("dialog", name="确认发送到 GitHub", exact=True)
            await expect(review.get_by_text("feature → main 的 Pull request", exact=True)).to_be_visible()
            await review.get_by_role("button", name="确认发送这份草稿", exact=True).click()
            await expect(review).not_to_be_visible()
            await expect(pr_card.locator(".github-draft-state")).to_have_text("已发送")
            state = (await api.get("/fixture/github/state")).json()
            assert state["remote_writes"] == 2 and state["comment_count"] == 1 and state["pull_count"] == 1
            assert state["model_calls"] == 0
            await page.locator(".github-scroll").evaluate("element => element.scrollTo(0, 0)")
            await screenshot(page, "github-narrow-sent-drafts.png")
            await assert_basics(page)
            assert not errors
            await context.close()
        finally:
            await browser.close()


@pytest.mark.browser
async def test_github_unknown_send_blocks_replay_and_read_only_reconciles(github_workbench_server):
    async with client_for(github_workbench_server) as api, async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context, page, errors = await open_workbench(browser, github_workbench_server)
            await open_github(page)
            await create_source(page, token="ghp_offline_unknown_outcome_fixture")
            await enable_reviewed_writes(page)
            await prepare_comment(page, "远端接收后发生超时的离线评论。")
            await api.post("/fixture/github/control", json={"ambiguous_next_send": True, "omit_reconcile_match": True})
            detail = page.get_by_role("region", name="GitHub 来源详情")
            await detail.get_by_role("button", name="审阅并发送", exact=True).click()
            review = page.get_by_role("dialog", name="确认发送到 GitHub", exact=True)
            await review.get_by_role("button", name="确认发送这份草稿", exact=True).click()
            await expect(review.get_by_role("alert")).to_be_visible()
            await expect(review.get_by_role("button", name="确认发送这份草稿", exact=True)).to_be_disabled()
            await review.get_by_role("button", name="返回检查", exact=True).click()
            await expect(detail.locator(".github-draft-state")).to_have_text("发送结果待核验")
            await expect(detail.get_by_role("button", name="审阅并发送", exact=True)).to_have_count(0)
            await expect(detail.get_by_role("button", name="编辑草稿", exact=True)).to_have_count(0)
            assert (await api.get("/fixture/github/state")).json()["remote_writes"] == 1
            await detail.get_by_role("button", name="只读核验结果", exact=True).click()
            await expect(page.get_by_role("status")).to_contain_text("尚未找到足够证据")
            await expect(detail.locator(".github-draft-state")).to_have_text("发送结果待核验")
            await screenshot(page, "github-unknown-no-replay.png")
            await api.post("/fixture/github/control", json={"omit_reconcile_match": False})
            await detail.get_by_role("button", name="只读核验结果", exact=True).click()
            await expect(page.get_by_role("status")).to_contain_text("找到了匹配回执")
            await expect(detail.locator(".github-draft-state")).to_have_text("已发送")
            state = (await api.get("/fixture/github/state")).json()
            assert state["remote_writes"] == 1 and state["comment_count"] == 1 and state["model_calls"] == 0
            await assert_basics(page)
            assert not errors
            await context.close()
        finally:
            await browser.close()
