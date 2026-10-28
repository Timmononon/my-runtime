"""Parallel chat surfaces against the actual offline Runtime and built frontend."""
from __future__ import annotations

import asyncio
from urllib.parse import urlsplit

import pytest
from playwright.async_api import async_playwright, expect
from test_web_audit_browser import geometry, sidebar
from test_web_workbench import client_for, create_session, wait_run
from test_web_workbench import workbench_server as workbench_server  # noqa: PLC0414
from test_web_workbench_browser import open_workbench, screenshot


async def seed(api, server, title="主对话保留在这里"):
    session = await create_session(api, server, title)
    result = await api.post(f"/sessions/{session['id']}/runs", json={"text": "先解释一下这个想法"})
    assert result.status_code == 202, result.text
    assert (await wait_run(api, result.json()["id"]))["status"] == "completed"
    return session, (await api.get(f"/sessions/{session['id']}")).json()["messages"]


async def open_saved(page, title):
    await sidebar(page)
    await page.locator(".recent-item").filter(has_text=title).first.click()


async def open_side_menu(page):
    await page.get_by_role("button", name="管理当前对话", exact=True).click()
    menu = page.get_by_role("dialog", name="当前对话操作", exact=True)
    await menu.get_by_role("button", name="侧边聊天", exact=True).click()


async def leave_panel(page):
    await page.get_by_role("button", name="收起侧边聊天", exact=True).click()
    await expect(page.get_by_role("complementary", name="侧边聊天", exact=True)).not_to_be_visible()
    await expect(page.get_by_role("dialog", name="删除侧边聊天", exact=True)).to_have_count(0)


async def start_side(page, parent):
    async with page.expect_response(lambda response: response.request.method == "POST"
            and urlsplit(response.url).path == f"/sessions/{parent['id']}/side-chat") as created:
        await open_side_menu(page)
    response = await created.value
    assert response.status == 201, await response.text()
    child = await response.json()
    panel = page.get_by_role("complementary", name="侧边聊天", exact=True)
    await expect(panel.get_by_role("textbox", name="侧边聊天消息", exact=True)).to_be_enabled()
    assert child["lineage"]["kind"] == "side_chat" and child["lineage"]["parent_id"] == parent["id"]
    return child, panel


async def send_side(page, panel, text, *, endpoint="runs"):
    sid = await panel.get_attribute("data-session-id")
    if endpoint == "runs":
        await expect(panel.get_by_role("button", name="停止侧边任务", exact=True)).to_have_count(0)
    await panel.get_by_role("textbox", name="侧边聊天消息", exact=True).fill(text)
    async with page.expect_response(lambda response: response.request.method == "POST"
            and urlsplit(response.url).path == f"/sessions/{sid}/{endpoint}") as sent:
        await panel.get_by_role("button", name="发送侧边聊天消息", exact=True).click()
    response = await sent.value
    assert response.status in {200, 201, 202}, await response.text()
    return await response.json()


@pytest.mark.browser
@pytest.mark.parametrize("width", [1440, 390])
async def test_side_panel_preserves_main_draft_context_and_reopens_saved_chat(workbench_server, width):
    server = workbench_server
    async with client_for(server) as api, async_playwright() as playwright:
        parent, _original = await seed(api, server)
        later = await api.post(f"/sessions/{parent['id']}/runs", json={"text": "主对话继续讨论"})
        await wait_run(api, later.json()["id"])
        parent_records = (await api.get(f"/sessions/{parent['id']}")).json()["messages"]
        other = await create_session(api, server, "另一个普通对话")
        browser = await playwright.chromium.launch(headless=True)
        try:
            context, page, errors = await open_workbench(browser, server, width=width, height=900)
            await open_saved(page, parent["title"])
            main = page.locator(".chat-area")
            await expect(main.locator(".assistant-group")).to_have_count(2)
            await main.get_by_label("输入消息", exact=True).fill("主对话草稿要保留")
            await main.locator('input[type="file"]').set_input_files({
                "name": "main-only.txt", "mimeType": "text/plain", "buffer": b"belongs to the main composer"})
            await expect(main.locator(".draft-attachments")).to_contain_text("main-only.txt")
            await main.evaluate("el => { window.originalMain = el; }")
            await expect(main.get_by_role("button", name="侧边聊天", exact=True)).to_have_count(0)
            await expect(page.get_by_role("button", name="打开侧边聊天面板", exact=True)).to_have_count(0)
            child, panel = await start_side(page, parent)
            await expect(page.locator(".sidebar .recent-item").filter(has_text=child["title"])).to_have_count(0)
            await expect(page.locator(".page-name")).to_have_text(parent["title"])
            assert await main.evaluate("el => el === window.originalMain")
            assert await main.get_by_label("输入消息", exact=True).input_value() == "主对话草稿要保留"
            await expect(panel.get_by_label("当前助手", exact=True)).to_have_text("默认助手")
            assert await panel.locator(".message").count() == 0
            child_records = (await api.get(f"/sessions/{child['id']}")).json()["messages"]
            assert len(child_records) == len(parent_records)
            assert [row["message"]["content"] for row in child_records] == [row["message"]["content"] for row in parent_records]
            if width > 1000:
                await expect(main).to_be_visible()
                left, right = await main.bounding_box(), await panel.bounding_box()
                assert left["x"] + left["width"] <= right["x"] + 1
            else:
                await expect(main).not_to_be_visible()
            sent = await send_side(page, panel, "只在侧边聊这个问题")
            assert sent["session_id"] == child["id"]
            await wait_run(api, sent["id"])
            await expect(panel.locator(".assistant-group .markdown").last).to_contain_text("离线验收")
            assert (await api.get(f"/sessions/{parent['id']}")).json()["messages"] == parent_records
            assert not await main.locator(".message.user").filter(has_text="只在侧边聊这个问题").count()
            await panel.get_by_label("侧边聊天消息", exact=True).fill("尚未发送的侧边草稿")
            await panel.get_by_label("侧边聊天附件", exact=True).set_input_files({
                "name": "keep-side-draft.txt", "mimeType": "text/plain", "buffer": b"retain when collapsed"})
            await expect(panel.locator(".draft-attachments")).to_contain_text("keep-side-draft.txt")
            collapse = await panel.get_by_role("button", name="收起侧边聊天", exact=True).bounding_box()
            close = await panel.get_by_role("button", name="关闭侧边聊天", exact=True).bounding_box()
            assert collapse["x"] + collapse["width"] <= close["x"] + 1
            await screenshot(page, f"side-chat-parallel-{width}.png")
            await geometry(page)
            assert await panel.evaluate("el=>el.scrollWidth <= el.clientWidth + 1")
            deletions = []
            page.on("request", lambda req: deletions.append(req.url) if req.method == "DELETE" else None)
            await panel.get_by_role("button", name="关闭侧边聊天", exact=True).click()
            confirmation = page.get_by_role("dialog", name="删除侧边聊天", exact=True)
            await expect(confirmation).to_contain_text("是否删除")
            await expect(confirmation).to_contain_text("主对话和共享文件会保留")
            assert deletions == []
            await screenshot(page, f"side-chat-delete-confirm-{width}.png")
            await confirmation.get_by_role("button", name="取消", exact=True).click()
            await expect(panel.get_by_label("侧边聊天消息", exact=True)).to_have_value("尚未发送的侧边草稿")
            assert (await api.get(f"/sessions/{child['id']}")).status_code == 200
            await leave_panel(page)
            await expect(main).to_be_visible()
            await expect(main.get_by_label("输入消息", exact=True)).to_have_value("主对话草稿要保留")
            await expect(main.locator(".draft-attachments")).to_contain_text("main-only.txt")
            await open_side_menu(page)
            await expect(panel.get_by_label("侧边聊天消息", exact=True)).to_have_value("尚未发送的侧边草稿")
            await expect(panel.locator(".draft-attachments")).to_contain_text("keep-side-draft.txt")
            assert deletions == []
            await panel.get_by_label("侧边聊天消息", exact=True).press("Escape")
            await expect(panel).not_to_be_visible()
            await expect(confirmation).to_have_count(0)
            assert deletions == []
            await open_saved(page, other["title"])
            await expect(page.locator(".page-name")).to_have_text(other["title"])
            await open_saved(page, parent["title"])
            await open_side_menu(page)
            await expect(page.locator(".page-name")).to_have_text(parent["title"])
            await expect(panel.get_by_label("侧边聊天消息", exact=True)).to_have_value("尚未发送的侧边草稿")
            await page.reload()
            await expect(page.get_by_label("输入消息", exact=True)).to_be_visible()
            await expect(page.locator(".sidebar .recent-item").filter(has_text=child["title"])).to_have_count(0)
            await open_saved(page, parent["title"])
            await open_side_menu(page)
            await expect(page.locator(".page-name")).to_have_text(parent["title"])
            await expect(panel.locator(".message.user")).to_contain_text("只在侧边聊这个问题")
            await panel.get_by_role("button", name="关闭侧边聊天", exact=True).click()
            async with page.expect_response(lambda r: r.request.method == "DELETE" and urlsplit(r.url).path == f"/sessions/{child['id']}") as deleted:
                await confirmation.get_by_role("button", name="永久删除", exact=True).click()
            assert (await deleted.value).status == 200
            await expect(panel).to_have_count(0)
            assert (await api.get(f"/sessions/{child['id']}")).status_code == 404
            # Fork remains the ordinary new-main-chat action with the exact boundary.
            await main.get_by_role("button", name="从这里分支", exact=True).first.click()
            dialog = page.get_by_role("dialog", name="从这里创建分支")
            await dialog.get_by_role("textbox").fill("普通分支继续走主对话")
            async with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/fork")) as forked:
                await dialog.get_by_role("button", name="创建分支", exact=True).click()
            fork = await (await forked.value).json()
            assert fork["lineage"]["kind"] == "fork"
            await expect(page.locator(".page-name")).to_have_text("普通分支继续走主对话")
            await expect(panel).not_to_be_visible()
            assert (await api.get(f"/sessions/{parent['id']}")).json()["messages"] == parent_records
            assert errors == []
            await context.close()
        finally:
            await browser.close()


@pytest.mark.browser
async def test_side_chat_late_creation_and_lost_send_response_do_not_replace_or_duplicate(workbench_server):
    server = workbench_server
    async with client_for(server) as api, async_playwright() as playwright:
        parent, original = await seed(api, server)
        other = await create_session(api, server, "晚到响应不能抢回这个对话")
        browser = await playwright.chromium.launch(headless=True)
        created, release = asyncio.Event(), asyncio.Event()
        child = None
        try:
            context, page, errors = await open_workbench(browser, server)
            await open_saved(page, parent["title"])

            async def delayed_creation(route):
                nonlocal child
                response = await route.fetch()
                assert response.status == 201
                child = await response.json()
                created.set()
                await release.wait()
                await route.fulfill(response=response)

            await page.route(f"**/sessions/{parent['id']}/side-chat", delayed_creation)
            await open_side_menu(page)
            await asyncio.wait_for(created.wait(), 5)
            await open_saved(page, other["title"])
            await expect(page.locator(".page-name")).to_have_text(other["title"])
            async with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/side-chat")):
                release.set()
            await expect(page.locator(".page-name")).to_have_text(other["title"])
            await expect(page.get_by_role("complementary", name="侧边聊天", exact=True)).to_have_count(0)
            assert child
            await open_saved(page, parent["title"])
            await open_side_menu(page)
            panel = page.get_by_role("complementary", name="侧边聊天", exact=True)
            await expect(panel.get_by_label("侧边聊天消息", exact=True)).to_be_enabled()
            attempts = []

            async def lost_response(route):
                attempts.append(route.request.post_data_json)
                response = await route.fetch()
                assert response.status == 202
                if len(attempts) == 1:
                    await route.abort("failed")
                else:
                    await route.fulfill(response=response)

            await page.route(f"**/sessions/{child['id']}/runs", lost_response)
            text = "已接收但响应丢失，手动重试不能产生两次任务"
            await panel.get_by_label("侧边聊天消息", exact=True).fill(text)
            await panel.get_by_role("button", name="发送侧边聊天消息", exact=True).click()
            await expect(panel.get_by_role("alert")).to_be_visible()
            await expect(panel.get_by_label("侧边聊天消息", exact=True)).to_have_value(text)
            await expect(panel.locator(".assistant-group .markdown")).to_contain_text("离线验收")
            await send_side(page, panel, text)
            assert len(attempts) == 2 and attempts[0] == attempts[1]
            await expect(panel.get_by_label("侧边聊天消息", exact=True)).to_have_value("")
            await expect(panel.locator(".message.user")).to_have_count(1)
            await expect(panel.locator(".assistant-group")).to_have_count(1)
            saved = (await api.get(f"/sessions/{child['id']}")).json()
            assert len(saved["runs"]) == 1
            assert (await api.get(f"/sessions/{parent['id']}")).json()["messages"] == original
            # Management navigation hides, rather than destroys, the panel.
            await panel.get_by_label("侧边聊天消息", exact=True).fill("跨页面保留的草稿")
            await page.get_by_role("navigation", name="主导航").get_by_role("button", name="Skills", exact=True).click()
            await expect(panel).not_to_be_visible()
            await page.get_by_role("navigation", name="主导航").get_by_role("button", name="对话", exact=True).click()
            await expect(panel.get_by_label("侧边聊天消息", exact=True)).to_have_value("跨页面保留的草稿")
            assert errors == []
            await context.close()
        finally:
            release.set()
            await browser.close()


@pytest.mark.browser
async def test_side_chat_archive_and_delete_do_not_change_main_history(workbench_server):
    server = workbench_server
    async with client_for(server) as api, async_playwright() as playwright:
        parent, original = await seed(api, server)
        browser = await playwright.chromium.launch(headless=True)
        try:
            context, page, errors = await open_workbench(browser, server)
            await open_saved(page, parent["title"])
            child, panel = await start_side(page, parent)
            sent = await send_side(page, panel, "这条侧边记录可以独立归档")
            await wait_run(api, sent["id"])
            await expect(panel.get_by_role("button", name="停止侧边任务", exact=True)).to_have_count(0)
            response = await api.post(f"/sessions/{child['id']}/archive")
            assert response.status_code == 200
            await expect(panel.get_by_label("侧边聊天消息", exact=True)).to_be_disabled()
            response = await api.post(f"/sessions/{child['id']}/unarchive")
            assert response.status_code == 200
            await expect(panel.get_by_label("侧边聊天消息", exact=True)).to_be_enabled()
            await panel.get_by_role("button", name="关闭侧边聊天", exact=True).click()
            dialog = page.get_by_role("dialog", name="删除侧边聊天", exact=True)
            await dialog.get_by_role("button", name="永久删除", exact=True).click()
            await expect(panel).to_have_count(0)
            await expect(page.locator(".page-name")).to_have_text(parent["title"])
            assert (await api.get(f"/sessions/{parent['id']}")).json()["messages"] == original
            assert (await api.get(f"/sessions/{child['id']}")).status_code == 404
            assert errors == []
            await context.close()
        finally:
            await browser.close()


@pytest.mark.browser
async def test_side_chat_requests_attachments_and_stop_are_session_scoped(workbench_server):
    server = workbench_server
    async with client_for(server) as api, async_playwright() as playwright:
        parent, original = await seed(api, server)
        browser = await playwright.chromium.launch(headless=True)
        try:
            context, page, errors = await open_workbench(browser, server)
            await open_saved(page, parent["title"])
            child, panel = await start_side(page, parent)
            main = page.locator(".chat-area")
            await main.get_by_label("输入消息", exact=True).fill("测试取消：主任务保持运行")
            async with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith(f"/{parent['id']}/runs")) as started:
                await main.get_by_role("button", name="发送消息", exact=True).click()
            main_run = await (await started.value).json()
            await expect(main.get_by_role("button", name="停止", exact=True)).to_be_visible()
            question = await send_side(page, panel, "测试提问")
            await expect(panel.locator(".pending-card.question")).to_be_visible()
            await panel.locator(".question-options").get_by_role("button", name="中文").click()
            await panel.get_by_role("button", name="确认回答", exact=True).click()
            assert (await wait_run(api, question["id"]))["status"] == "completed"
            await expect(panel.locator(".pending-card")).to_have_count(0)
            approval = await send_side(page, panel, "测试审批")
            await expect(panel.locator(".pending-card.approval")).to_be_visible()
            await screenshot(page, "side-chat-approval.png")
            await panel.get_by_role("button", name="允许这一次", exact=True).click()
            assert (await wait_run(api, approval["id"]))["status"] == "completed"
            await expect(panel.locator(".pending-card")).to_have_count(0)
            await expect(panel.locator(".side-chat-deliverables")).to_be_visible()
            await panel.get_by_label("侧边聊天附件", exact=True).set_input_files({
                "name": "side-only.txt", "mimeType": "text/plain", "buffer": b"side chat attachment"})
            await expect(panel.locator(".draft-attachments")).to_contain_text("side-only.txt")
            upload_run = await send_side(page, panel, "读侧边附件")
            await wait_run(api, upload_run["id"])
            await expect(panel.locator(".assistant-group .markdown").last).to_contain_text("side-only.txt")
            assert (await api.get(f"/sessions/{parent['id']}/attachments")).json()["attachments"] == []
            waiting = await send_side(page, panel, "测试取消：只停止侧边任务")
            await expect(panel.get_by_role("button", name="停止侧边任务", exact=True)).to_be_enabled()
            await panel.get_by_label("侧边聊天消息", exact=True).fill("等待中的补充消息")
            await send_side(page, panel, "等待中的补充消息", endpoint="messages")
            await expect(panel.locator(".side-chat-queued")).to_contain_text("等待中的补充消息")
            await panel.get_by_role("button", name="撤回", exact=True).click()
            await expect(panel.locator(".side-chat-queued")).to_have_count(0)
            await leave_panel(page)
            assert (await api.get(f"/runs/{waiting['id']}")).json()["status"] not in {"completed", "cancelled"}
            await open_side_menu(page)
            await panel.get_by_role("button", name="停止侧边任务", exact=True).click()
            assert (await wait_run(api, waiting["id"]))["status"] == "cancelled"
            assert (await api.get(f"/runs/{main_run['id']}")).json()["status"] not in {"completed", "cancelled"}
            # A rejected delete must retain the panel and must not cancel its parent.
            await panel.get_by_role("button", name="关闭侧边聊天", exact=True).click()
            confirmation = page.get_by_role("dialog", name="删除侧边聊天", exact=True)
            async with page.expect_response(lambda r: r.request.method == "DELETE") as refused:
                await confirmation.get_by_role("button", name="永久删除", exact=True).click()
            assert (await refused.value).status == 409
            assert urlsplit((await refused.value).url).path == f"/sessions/{child['id']}"
            await expect(confirmation.get_by_role("alert")).to_be_visible()
            await confirmation.get_by_role("button", name="取消", exact=True).click()
            await expect(panel).to_be_visible()
            await main.get_by_role("button", name="停止", exact=True).click()
            assert (await wait_run(api, main_run["id"]))["status"] == "cancelled"
            await expect(main.get_by_role("button", name="停止", exact=True)).to_have_count(0)
            current = (await api.get(f"/sessions/{parent['id']}")).json()["messages"]
            assert current[:len(original)] == original
            assert not any("只停止侧边任务" in item["message"]["content"] for item in current)
            assert errors == []
            await context.close()
        finally:
            await browser.close()


@pytest.mark.browser
async def test_menu_recovers_existing_project_side_chat_without_sidebar_leaks_or_duplicates(workbench_server):
    server = workbench_server
    async with client_for(server) as api, async_playwright() as playwright:
        project = (await api.post("/workbench/projects", json={"name": "项目内侧边聊天", "cwd": server["cwd"]})).json()
        parent = (await api.post("/sessions", json={"title": "项目主对话", "project_id": project["id"]})).json()
        ordinary = await create_session(api, server, "侧边聊天 · 普通对话可以使用这个名称")
        child = (await api.post(f"/sessions/{parent['id']}/side-chat", json={"message_index": 0, "title": "既有侧边记录"})).json()
        another = await create_session(api, server, "另一个来源")
        foreign = (await api.post(f"/sessions/{another['id']}/side-chat", json={"message_index": 0, "title": "其他来源侧边记录"})).json()
        browser = await playwright.chromium.launch(headless=True)
        try:
            context, page, errors = await open_workbench(browser, server)
            rows = page.locator(".sidebar .recent-item")
            await expect(rows.filter(has_text=ordinary["title"])).to_have_count(1)
            await expect(rows.filter(has_text=child["title"])).to_have_count(0)
            await expect(rows.filter(has_text=foreign["title"])).to_have_count(0)
            await open_saved(page, parent["title"])
            await page.get_by_label("输入消息", exact=True).fill("读取失败也要保留主草稿")
            created = []
            page.on("request", lambda req: created.append(req.url)
                if req.method == "POST" and urlsplit(req.url).path.endswith("/side-chat") else None)
            pattern = "**/sessions?include_side_chats=true&**"
            await page.route(pattern, lambda route: route.abort("connectionfailed"))
            await open_side_menu(page)
            await expect(page.get_by_role("alert")).to_contain_text("Failed to fetch")
            assert created == []
            await expect(page.get_by_label("输入消息", exact=True)).to_have_value("读取失败也要保留主草稿")
            await page.unroute(pattern)
            await page.get_by_role("button", name="管理当前对话", exact=True).click()
            menu = page.get_by_role("dialog", name="当前对话操作", exact=True)
            await expect(menu.get_by_role("button", name="侧边聊天", exact=True)).to_be_visible()
            await screenshot(page, "side-chat-overflow-menu.png")
            await menu.get_by_role("button", name="侧边聊天", exact=True).click()
            panel = page.get_by_role("complementary", name="侧边聊天", exact=True)
            await expect(panel).to_have_attribute("data-session-id", child["id"])
            await expect(panel.get_by_label("侧边聊天消息", exact=True)).to_be_enabled()
            assert created == []
            await expect(rows.filter(has_text=child["title"])).to_have_count(0)
            await screenshot(page, "side-chat-clean-project-sidebar.png")
            assert errors == []
            await context.close()
        finally:
            await browser.close()
