"""Real Chromium acceptance against the built UI and offline HTTP fixture."""
from __future__ import annotations

import asyncio
import io
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from PIL import Image
from playwright.async_api import async_playwright, expect
from test_web_workbench import client_for, create_session, wait_run
from test_web_workbench import workbench_server as workbench_server  # noqa: PLC0414 - pytest fixture re-export


async def open_workbench(browser, server, *, width=1440, height=1000, has_touch=False):
    async with client_for(server) as launcher:
        issued = await launcher.post("/ui/tickets", json={"cwd": server["cwd"]})
        assert issued.status_code == 201
    context = await browser.new_context(viewport={"width": width, "height": height}, accept_downloads=True,
                                        has_touch=has_touch, is_mobile=has_touch)
    page = await context.new_page()
    errors = []
    page.qa_network = {"responses": [], "failed": [], "console": []}
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("response", lambda response: page.qa_network["responses"].append((response.status, response.url)) if response.status >= 400 else None)
    page.on("requestfailed", lambda request: page.qa_network["failed"].append((request.failure, request.url)))
    page.on("console", lambda message: page.qa_network["console"].append(message.text) if message.type == "error" else None)
    await page.goto(server["base_url"] + "/#ticket=" + issued.json()["ticket"])
    await expect(page.get_by_role("textbox", name="输入消息")).to_be_visible()
    assert "ticket=" not in page.url
    assert "aster_ui=" not in await page.evaluate("document.cookie")
    return context, page, errors


async def send(page, text):
    await page.get_by_role("textbox", name="输入消息").fill(text)
    await page.get_by_role("button", name="发送消息", exact=True).click()


async def open_settings(page):
    # Settings belongs to the workspace profile menu; it must not remain a
    # separate sidebar action that bypasses the requested discovery path.
    await expect(page.locator(".sidebar").get_by_role("button", name="设置", exact=True)).to_have_count(0)
    await page.get_by_role("button", name="打开个人菜单", exact=True).click()
    menu = page.get_by_role("dialog", name="个人菜单", exact=True)
    await expect(menu.get_by_role("button", name="设置", exact=True)).to_be_visible()
    await menu.get_by_role("button", name="设置", exact=True).click()
    dialog = page.get_by_role("dialog", name="设置", exact=True)
    await expect(dialog).to_be_visible()
    return dialog


async def project_action(page, project_name, label):
    await page.locator(".project-row-wrap").filter(
        has=page.get_by_role("button", name=project_name, exact=True),
    ).hover()
    action = page.get_by_role("button", name=label, exact=True)
    await expect(action).to_have_css("opacity", "1")
    await action.click()


async def screenshot(page, name):
    evidence = os.environ.get("ASTER_WEB_EVIDENCE")
    if evidence:
        target = Path(evidence)
        target.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(target / name), full_page=True, animations="disabled")


async def assert_basics(page):
    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    unnamed = await page.locator("button:visible").evaluate_all("""items => items.filter(el =>
        !((el.getAttribute('aria-label') || el.innerText || el.getAttribute('title') || '').trim())
    ).map(el => el.outerHTML)""")
    assert not unnamed, unnamed


def assert_transport(page, *, upload_error=False, connection_fault=False):
    network = page.qa_network
    bad_status = [(status, url) for status, url in network["responses"]
                  if not (upload_error and status == 422 and "/attachments?" in url)]
    assert not bad_status, bad_status
    failed = [(reason, url) for reason, url in network["failed"]
              if reason != "net::ERR_ABORTED" and not connection_fault]
    assert not failed, failed
    console = [item for item in network["console"] if not (
        item.startswith("Failed to load resource:") and (upload_error or connection_fault))]
    assert not console, console


def attachment_image():
    output = io.BytesIO()
    Image.new("RGB", (120, 80), "#ff6900").save(output, "PNG")
    return output.getvalue()


@pytest.mark.browser
async def test_workbench_browser_persisted_snapshot_before_delayed_stream_does_not_duplicate_text(workbench_server):
    progress = "离线验收：准备创建本地报告，请确认文件写入。"
    final = "离线验收：报告已生成，可以下载或打开静态预览。"
    async with client_for(workbench_server) as api, async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        release_stream, release_snapshots = asyncio.Event(), asyncio.Event()
        buffered, blocked_snapshot = asyncio.Event(), asyncio.Event()
        state = {"served_prefix": False, "hold_snapshots": False}
        buffered_url = None
        try:
            context, page, errors = await open_workbench(browser, workbench_server)

            async def delay_stream(route):
                nonlocal buffered_url
                if state["served_prefix"]:
                    await route.continue_()
                    return
                state["served_prefix"] = True
                buffered_url = route.request.url
                run_id = urlsplit(route.request.url).path.split("/")[2]
                waiting = await wait_run(api, run_id, status="waiting_approval")
                assert waiting["pending_requests"]
                # Read the real finite event prefix at this boundary. No
                # event text, IDs, timestamps or API state are fabricated.
                response = await route.fetch(url=route.request.url + "&follow=false")
                assert response.status == 200
                body = await response.body()
                frames = [json.loads(line[6:]) for line in body.decode().splitlines() if line.startswith("data: ")]
                assert any(frame["type"] == "model.text.delta" for frame in frames)
                assert any(frame["type"].startswith("approval.") for frame in frames)
                buffered.set()
                await release_stream.wait()
                await route.fulfill(response=response, body=body)

            async def delay_new_snapshot(route):
                pieces = urlsplit(route.request.url).path.strip("/").split("/")
                if route.request.method != "GET" or len(pieces) != 2:
                    await route.continue_()
                    return
                response = await route.fetch()
                if state["hold_snapshots"]:
                    blocked_snapshot.set()
                    await release_snapshots.wait()
                await route.fulfill(response=response)

            await page.route("**/runs/*/events?**", delay_stream)
            await page.route("**/sessions/*", delay_new_snapshot)
            async with page.expect_response(lambda response: response.request.method == "POST" and urlsplit(response.url).path.endswith("/runs")) as submitted:
                await send(page, "测试审批：快照领先迟到的流事件")
            accepted = await (await submitted.value).json()
            await asyncio.wait_for(buffered.wait(), 5)
            await expect(page.get_by_role("heading", name="允许这次操作吗？", exact=True)).to_be_visible()
            await expect(page.get_by_text(progress, exact=True)).to_have_count(1)
            snapshot = (await api.get(f"/sessions/{accepted['session_id']}")).json()
            assert any(row["message"]["content"] == progress for row in snapshot["messages"])
            await page.evaluate("""({progress, final}) => {
                const evidence = {maxProgress: 0, maxFinal: 0, newRequestStreamSeen: false};
                const read = () => {
                    const paragraphs = [...document.querySelectorAll('[data-testid="message-assistant"] p')];
                    evidence.maxProgress = Math.max(evidence.maxProgress, paragraphs.filter(el => el.textContent === progress).length);
                    evidence.maxFinal = Math.max(evidence.maxFinal, paragraphs.filter(el => el.textContent === final).length);
                    if ([...document.querySelectorAll('.streaming')].some(el => el.textContent.includes('离线验收：报告已生成'))) {
                        evidence.newRequestStreamSeen = true;
                    }
                };
                read();
                const observer = new MutationObserver(read);
                observer.observe(document.querySelector('#root'), {subtree: true, childList: true, characterData: true});
                window.snapshotStreamRace = {evidence, observer};
            }""", {"progress": progress, "final": final})
            state["hold_snapshots"] = True
            # A reconnect opens a long-lived second response. Await only the
            # finite prefix deliberately held above, never that later stream.
            async with page.expect_response(lambda response: response.url == buffered_url) as delivered:
                release_stream.set()
            await asyncio.wait_for((await delivered.value).finished(), 5)
            await asyncio.wait_for(blocked_snapshot.wait(), 5)
            await page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
            race = await page.evaluate("window.snapshotStreamRace.evidence")
            await screenshot(page, "workspace-settings-snapshot-before-stream.png")
            if evidence := os.environ.get("ASTER_WEB_EVIDENCE"):
                Path(evidence, "workspace-settings-snapshot-stream-boundary.json").write_text(json.dumps(race, indent=2), encoding="utf-8")
            assert race["maxProgress"] == 1, race
            await expect(page.get_by_text(progress, exact=True)).to_have_count(1)
            state["hold_snapshots"] = False
            release_snapshots.set()
            await page.get_by_role("button", name="允许这一次", exact=True).click()
            await expect(page.get_by_text(final, exact=True)).to_have_count(1)
            await expect(page.get_by_text(final, exact=True)).to_be_visible()
            outcome = await wait_run(api, accepted["id"])
            assert outcome["status"] == "completed"
            detail = (await api.get(f"/sessions/{accepted['session_id']}")).json()
            assert sum(row["message"]["role"] == "assistant" for row in detail["messages"]) == 4
            assistant = page.get_by_test_id("message-assistant")
            await expect(assistant).to_have_count(1)
            await expect(assistant.locator("details.tool-group")).to_have_count(3)
            await page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
            race = await page.evaluate("window.snapshotStreamRace.evidence")
            assert race["maxProgress"] == 1 and race["maxFinal"] == 1, race
            assert race["newRequestStreamSeen"], race
            await page.evaluate("window.snapshotStreamRace.observer.disconnect()")
            await screenshot(page, "workspace-settings-snapshot-stream-complete.png")
            if evidence := os.environ.get("ASTER_WEB_EVIDENCE"):
                Path(evidence, "workspace-settings-snapshot-stream-complete.json").write_text(json.dumps(race, indent=2), encoding="utf-8")
            await assert_basics(page)
            assert not errors, errors
            assert_transport(page)
            await context.close()
        finally:
            release_stream.set()
            release_snapshots.set()
            await browser.close()

@pytest.mark.browser
async def test_workbench_browser_upload_approval_deliverable_and_branch(workbench_server, tmp_path):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context, page, errors = await open_workbench(browser, workbench_server)
            await assert_basics(page)
            await screenshot(page, "desktop-home.png")
            await page.get_by_role("textbox", name="输入消息").fill("附件失败时保留这段草稿")
            await page.get_by_role("button", name="更多输入选项", exact=True).click()
            async with page.expect_file_chooser() as chooser:
                await page.get_by_role("button", name="添加附件", exact=True).click()
            await (await chooser.value).set_files({"name": "invalid.pdf", "mimeType": "application/pdf", "buffer": b"not a PDF"})
            await expect(page.get_by_role("alert")).to_be_visible()
            await expect(page.get_by_role("textbox", name="输入消息")).to_have_value("附件失败时保留这段草稿")
            async with client_for(workbench_server) as api:
                sessions = (await api.get("/sessions")).json()["sessions"]
                assert len(sessions) == 1
                sid = sessions[0]["id"]
                await page.locator('input[type="file"]').set_input_files({"name": "diagram.png", "mimeType": "image/png", "buffer": attachment_image()})
                await expect(page.locator(".draft-attachments").get_by_text("diagram.png", exact=True)).to_be_visible()
                assert len((await api.get("/sessions")).json()["sessions"]) == 1
                await send(page, "测试附件")
                detail = (await api.get(f"/sessions/{sid}")).json()
                outcome = await wait_run(api, detail["runs"][0]["id"])
                assert outcome["status"] == "completed", outcome.get("error")
                await expect(page.get_by_text("离线验收：已读取附件 diagram.png；有效图片 1 张。", exact=True)).to_be_visible()
                await send(page, "测试审批")
                await expect(page.get_by_role("heading", name="允许这次操作吗？")).to_be_visible()
                detail = (await api.get(f"/sessions/{sid}")).json()
                pending = await wait_run(api, detail["runs"][0]["id"], status="waiting_approval")
                target = Path(workbench_server["cwd"]) / pending["pending_requests"][0]["payload"]["arguments"]["path"]
                assert not target.exists()
                await screenshot(page, "desktop-approval.png")
                await page.get_by_role("button", name="允许这一次", exact=True).click()
                await expect(page.get_by_text("离线验收：报告已生成，可以下载或打开静态预览。", exact=True)).to_be_visible()
                assert target.is_file()
                assert await page.locator("details.tool-group[open]").count() == 0
                await page.get_by_role("button", name="文件与成果").click()
                async with page.expect_download() as download:
                    await page.get_by_role("button", name="下载", exact=True).click()
                path = tmp_path / "downloaded.html"
                await (await download.value).save_as(path)
                assert "ASTER WORKBENCH VERIFIED" in path.read_text(encoding="utf-8")
                async with context.expect_page() as popup:
                    await page.get_by_role("button", name="静态预览", exact=True).click()
                preview = await popup.value
                await expect(preview.frame_locator("iframe[data-aster-preview]").get_by_role("heading", name="ASTER WORKBENCH VERIFIED")).to_be_visible()
                assert await preview.evaluate("window.opener === null")
                await screenshot(preview, "deliverable-preview.png")
                await preview.close()
                await screenshot(page, "desktop-deliverable.png")
                history = (await api.get(f"/sessions/{sid}")).json()["messages"]
                async with page.expect_request(lambda request: request.method == "POST" and request.url.endswith("/fork")) as fork_request:
                    await page.get_by_role("button", name="从这里分支", exact=True).last.click()
                    branch_input = page.get_by_role("dialog").get_by_role("textbox")
                    await branch_input.press_sequentially("QA Branch", delay=15)
                    await expect(branch_input).to_have_value("QA Branch")
                    await page.get_by_role("button", name="创建分支", exact=True).click()
                assert (await fork_request.value).post_data_json["message_index"] == len(history)
                await expect(page.get_by_text("从已有对话继续", exact=False)).to_be_visible()
                child = next(item for item in (await api.get("/sessions")).json()["sessions"] if item["title"] == "QA Branch")
                copied = (await api.get(f"/sessions/{child['id']}")).json()["messages"]
                assert len(copied) == len(history) and len(history) > 6
            await assert_basics(page)
            assert not errors, errors
            assert_transport(page, upload_error=True)
            await context.close()
        finally:
            await browser.close()


@pytest.mark.browser
async def test_workbench_browser_narrow_question_cancel_and_navigation(workbench_server):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context, page, errors = await open_workbench(browser, workbench_server, width=390, height=844)
            await assert_basics(page)
            await screenshot(page, "narrow-home.png")
            await page.get_by_role("button", name="打开导航", exact=True).click()
            await page.get_by_role("button", name=re.compile("Skills")).click()
            await expect(page.get_by_role("heading", name="让 Aster 多一项拿手好戏。", exact=True)).to_be_visible()
            async with asyncio.timeout(3):
                while True:
                    box = await page.locator(".sidebar").bounding_box()
                    if box is None or box["x"] + box["width"] <= 1:
                        break
                    await asyncio.sleep(0.02)
            await assert_basics(page)
            await screenshot(page, "narrow-skills.png")
            await page.get_by_role("button", name="打开导航", exact=True).click()
            await page.get_by_role("button", name="对话", exact=True).click()
            await send(page, "测试提问")
            await expect(page.get_by_role("heading", name="这份报告使用哪种语言？")).to_be_visible()
            await page.locator(".question-options").get_by_role("button", name="中文", exact=False).click()
            await page.get_by_role("button", name="确认回答", exact=False).click()
            await expect(page.get_by_text('离线验收：收到选择 "中文"', exact=True)).to_be_visible()
            await send(page, "测试取消")
            await expect(page.get_by_role("button", name="停止", exact=True)).to_be_visible()
            await page.get_by_role("button", name="停止", exact=True).click()
            await expect(page.get_by_text("任务已停止，已完成的操作和对话仍会保留。", exact=True)).to_be_visible()
            async with client_for(workbench_server) as api:
                session = (await api.get("/sessions")).json()["sessions"][0]
                run = (await api.get(f"/sessions/{session['id']}")).json()["runs"][0]
                assert run["status"] == "cancelled"
            await assert_basics(page)
            await screenshot(page, "narrow-cancelled.png")
            assert not errors, errors
            assert_transport(page)
            await context.close()
        finally:
            await browser.close()


@pytest.mark.browser
async def test_workbench_browser_sse_disconnect_recovers_without_duplicate_text(workbench_server):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context, page, errors = await open_workbench(browser, workbench_server)
            event_requests = []
            page.on("request", lambda request: event_requests.append(request.url) if "/events?after=" in request.url else None)
            await send(page, "测试重连")
            await expect(page.locator(".streaming .markdown")).to_contain_text("离线验收")
            # The fixture closes the real SSE body once after its first delta.
            # Model execution continues, so the next fetch must replay a cursor.
            expected = "离线验收：" + "事件重连应保持这段文字完整且只出现一次。" * 8
            await expect(page.get_by_text(expected, exact=True)).to_be_visible(timeout=18000)
            assert await page.get_by_text(expected, exact=True).count() == 1
            await expect(page.locator(".streaming")).to_have_count(0)
            assert any(not url.endswith("after=0") for url in event_requests), event_requests
            async with client_for(workbench_server) as api:
                sid = (await api.get("/sessions")).json()["sessions"][0]["id"]
                history = (await api.get(f"/sessions/{sid}")).json()["messages"]
                assert history[-1]["message"]["content"] == expected
            assert not errors, errors
            assert_transport(page, connection_fault=True)
            await context.close()
        finally:
            await browser.close()


@pytest.mark.browser
async def test_workbench_browser_response_loss_retry_keeps_single_run(workbench_server):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context, page, errors = await open_workbench(browser, workbench_server)
            admitted = []

            async def lose_first_response(route):
                if route.request.method == "POST" and not admitted:
                    response = await route.fetch()
                    assert response.status == 202
                    admitted.append({"run": await response.json(), "request": route.request.post_data_json})
                    # The real server accepted the request; only its response is lost.
                    await route.abort("failed")
                else:
                    await route.continue_()

            await page.route("**/sessions/*/runs", lose_first_response)
            prompt = "你好，验证响应丢失后的重试"
            await send(page, prompt)
            await expect(page.get_by_role("alert")).to_be_visible()
            await expect(page.get_by_role("textbox", name="输入消息")).to_have_value(prompt)
            assert len(admitted) == 1
            async with client_for(workbench_server) as api:
                await wait_run(api, admitted[0]["run"]["id"])
                await page.get_by_role("button", name="发送消息", exact=True).click()
                await expect(page.get_by_text("离线验收：你好，我是 Aster。聊天、事件和历史均来自当前临时 Runtime。", exact=True)).to_be_visible()
                sid = admitted[0]["run"]["session_id"]
                detail = (await api.get(f"/sessions/{sid}")).json()
                assert len(detail["runs"]) == 1
                assert sum(item["message"]["role"] == "user" for item in detail["messages"]) == 1
            assert not errors, errors
            assert_transport(page, connection_fault=True)
            await context.close()
        finally:
            await browser.close()


@pytest.mark.browser
async def test_workbench_browser_late_approval_not_reported_as_success(workbench_server):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context, page, errors = await open_workbench(browser, workbench_server)
            await send(page, "测试审批")
            await expect(page.get_by_role("heading", name="允许这次操作吗？")).to_be_visible()
            sent, release = asyncio.Event(), asyncio.Event()

            async def delay_decision(route):
                sent.set()
                await release.wait()
                await route.continue_()

            await page.route("**/approvals/*/decision", delay_decision)
            await page.get_by_role("button", name="允许这一次", exact=True).click()
            await asyncio.wait_for(sent.wait(), 3)
            async with client_for(workbench_server) as api:
                sid = (await api.get("/sessions")).json()["sessions"][0]["id"]
                run = (await api.get(f"/sessions/{sid}")).json()["runs"][0]
                pending = (await api.get(f"/runs/{run['id']}")).json()["pending_requests"][0]
                target = Path(workbench_server["cwd"]) / pending["payload"]["arguments"]["path"]
                assert (await api.post(f"/approvals/{pending['id']}/decision", json={"approved": False})).json()["accepted"]
                release.set()
                await expect(page.get_by_role("alert")).to_contain_text("已经结束或过期")
                assert (await wait_run(api, run["id"]))["status"] == "completed"
                assert not target.exists()
            assert not errors, errors
            assert_transport(page)
            await context.close()
        finally:
            await browser.close()


@pytest.mark.browser
async def test_workbench_browser_delayed_submit_cannot_leak_into_another_session(workbench_server):
    async with client_for(workbench_server) as api:
        first = await create_session(api, workbench_server, "会话 A")
        second = await create_session(api, workbench_server, "会话 B")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        release = asyncio.Event()
        try:
            context, page, errors = await open_workbench(browser, workbench_server)
            await page.get_by_role("button", name="会话 A", exact=True).click()
            received = asyncio.Event()

            async def delayed_response(route):
                response = await route.fetch()
                received.set()
                await release.wait()
                await route.fulfill(response=response)

            await page.route(f"**/sessions/{first['id']}/runs", delayed_response)
            await send(page, "只属于会话 A 的输入")
            await asyncio.wait_for(received.wait(), 3)
            await page.get_by_role("button", name="会话 B", exact=True).click()
            await expect(page.locator(".page-name")).to_have_text("会话 B")
            release.set()
            await asyncio.sleep(0.8)
            await expect(page.locator(".page-name")).to_have_text("会话 B")
            assert await page.get_by_text("只属于会话 A 的输入", exact=True).count() == 0
            assert await page.locator(".streaming").count() == 0
            assert await page.get_by_role("button", name="停止", exact=True).count() == 0
            async with client_for(workbench_server) as api:
                assert (await api.get(f"/sessions/{second['id']}")).json()["messages"] == []
                run = (await api.get(f"/sessions/{first['id']}")).json()["runs"][0]
                assert (await wait_run(api, run["id"]))["status"] == "completed"
            assert not errors, errors
            assert_transport(page)
            await context.close()
        finally:
            release.set()
            await browser.close()


@pytest.mark.browser
async def test_workbench_browser_delayed_creation_preserves_new_chat_selection(workbench_server):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        release = asyncio.Event()
        try:
            context, page, errors = await open_workbench(browser, workbench_server)
            received = asyncio.Event()

            async def delayed_creation(route):
                if route.request.method == "POST":
                    response = await route.fetch()
                    received.set()
                    await release.wait()
                    await route.fulfill(response=response)
                else:
                    await route.continue_()

            await page.route("**/sessions", delayed_creation)
            await send(page, "旧建会话请求")
            await asyncio.wait_for(received.wait(), 3)
            await page.get_by_role("button", name="开启新对话", exact=False).click()
            await page.get_by_role("textbox", name="输入消息").fill("新的未发送草稿")
            release.set()
            await asyncio.sleep(0.8)
            await expect(page.get_by_role("textbox", name="输入消息")).to_have_value("新的未发送草稿")
            await expect(page.get_by_role("heading", name="有什么可以帮你？", exact=True)).to_be_visible()
            assert await page.locator(".conversation").get_by_text("旧建会话请求", exact=True).count() == 0
            assert not errors, errors
            assert_transport(page)
            await context.close()
        finally:
            release.set()
            await browser.close()
