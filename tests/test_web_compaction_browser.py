"""Explicit summary action through the real offline HTTP workbench."""
import asyncio
import re
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from playwright.async_api import async_playwright, expect
from test_web_workbench import client_for, create_session, wait_run
from test_web_workbench import workbench_server as workbench_server  # noqa: PLC0414
from test_web_workbench_browser import assert_basics, assert_transport, open_workbench, screenshot
from test_web_workbench_v2_browser import receipts


@pytest.mark.browser
@pytest.mark.parametrize("width", [1440, 390])
async def test_model_summary_action_preserves_archive_and_accounts_helpers(workbench_server, width):
    async with client_for(workbench_server) as api:
        session = await create_session(api, workbench_server, "摘要压缩验收")
        sid = session["id"]
        for index in range(8):
            response = await api.post(f"/sessions/{sid}/runs", json={"text": f"测试长对话 {index}"})
            assert response.status_code == 202, response.text
            assert (await wait_run(api, response.json()["id"]))["status"] == "completed"
        archive = (await api.get(f"/sessions/{sid}")).json()["messages"]
        original_calls = len(receipts(workbench_server))
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                context, page, errors = await open_workbench(browser, workbench_server, width=width, height=900)
                if width < 700:
                    await page.get_by_role("button", name="打开导航", exact=True).click()
                await page.get_by_role("button", name="摘要压缩验收", exact=True).click()
                await expect(page.get_by_test_id("message-user")).to_have_count(8)
                await page.get_by_role("button", name="更多输入选项", exact=True).click()
                choice = page.get_by_role("button", name=re.compile("模型摘要压缩"))
                await expect(choice).to_contain_text("计入 API 用量")
                await expect(choice).to_be_in_viewport()
                await screenshot(page, f"compaction-menu-{width}.png")
                async with page.expect_response(lambda value: value.url.endswith("/compact")) as completed:
                    await choice.click()
                response = await completed.value
                assert response.status == 200, await response.text()
                result = await response.json()
                assert result["changed"] and result["semantic_applied"]
                assert result["after_tokens"] < result["before_tokens"]
                await expect(page.get_by_role("status").filter(has_text="已通过模型摘要")).to_be_visible()
                assert (await api.get(f"/sessions/{sid}")).json()["messages"] == archive
                helpers = receipts(workbench_server)[original_calls:]
                assert helpers and all(row["helper"] and row["tool_count"] == 0 for row in helpers)
                run = (await api.get(f"/runs/{result['run_id']}")).json()
                assert run["status"] == "completed" and run["budget"]["model_requests"] == len(helpers)
                await assert_basics(page)
                await screenshot(page, f"compaction-complete-{width}.png")
                assert not errors, errors
                await context.close()
            finally:
                await browser.close()


def checkpoint(server, session_id):
    with closing(sqlite3.connect(Path(server["data_dir"]) / "runtime.db")) as db:
        return db.execute("SELECT archive_through,messages_json,metadata_json FROM checkpoints WHERE session_id=?",
                          (session_id,)).fetchone()


@pytest.mark.browser
@pytest.mark.parametrize("width", [1440, 390])
async def test_model_summary_can_be_cancelled_from_ui_without_cross_session_effects(workbench_server, width):
    async with client_for(workbench_server) as api:
        session = await create_session(api, workbench_server, "摘要取消验收")
        sid = session["id"]
        other = await create_session(api, workbench_server, "无关会话")
        for index in range(8):
            response = await api.post(f"/sessions/{sid}/runs", json={"text": f"测试长对话 {index} QA_BLOCK_SUMMARY_UNTIL_CANCEL"})
            assert response.status_code == 202, response.text
            assert (await wait_run(api, response.json()["id"]))["status"] == "completed"
        archive = (await api.get(f"/sessions/{sid}")).json()["messages"]
        before_checkpoint = checkpoint(workbench_server, sid)
        original_calls = len(receipts(workbench_server))
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                context, page, errors = await open_workbench(browser, workbench_server, width=width, height=900)

                async def select(title):
                    if width < 700:
                        await page.get_by_role("button", name="打开导航", exact=True).click()
                    await page.get_by_role("button", name=title, exact=True).click()

                await select("摘要取消验收")
                await expect(page.get_by_test_id("message-user")).to_have_count(8)
                await page.get_by_role("button", name="更多输入选项", exact=True).click()
                async with page.expect_response(lambda value: value.url.endswith(f"/sessions/{sid}/compact")) as pending_compact:
                    await page.get_by_role("button", name=re.compile("模型摘要压缩")).click()
                    await expect(page.get_by_role("status").filter(has_text="正在生成模型摘要")).to_be_visible()
                    cancel = page.get_by_role("button", name="取消压缩", exact=True)
                    await expect(cancel).to_be_visible()
                    await expect(cancel).to_be_enabled(timeout=10000)
                    await expect(page.get_by_role("button", name="发送补充消息", exact=True)).to_be_disabled()
                    detail = (await api.get(f"/sessions/{sid}")).json()
                    active = next(run for run in detail["runs"] if run["status"] == "running")
                    rid = active["id"]
                    await screenshot(page, f"compaction-cancellable-{width}.png")

                    # The pending action belongs to its originating session.
                    await select("无关会话")
                    await expect(page.get_by_role("button", name="取消压缩", exact=True)).to_have_count(0)
                    await expect(page.get_by_role("status").filter(has_text="正在生成模型摘要")).to_have_count(0)
                    assert (await api.get(f"/sessions/{other['id']}")).json()["runs"] == []
                    await select("摘要取消验收")
                    cancel = page.get_by_role("button", name="取消压缩", exact=True)
                    await expect(cancel).to_be_enabled()
                    async with page.expect_response(lambda value: value.url.endswith(f"/runs/{rid}/cancel")) as cancelled:
                        await cancel.click()
                    cancel_response = await cancelled.value
                    assert cancel_response.status == 200
                    assert (await cancel_response.json())["accepted"]
                compact_response = await pending_compact.value
                assert compact_response.status == 200, await compact_response.text()
                result = await compact_response.json()
                assert result["cancelled"] and not result["changed"] and result["preserved_archive"]
                await expect(page.get_by_role("status").filter(has_text="上下文压缩已取消")).to_be_visible()
                await expect(page.get_by_role("button", name="取消压缩", exact=True)).to_have_count(0)
                run = await wait_run(api, rid)
                assert run["status"] == "cancelled"
                helpers = receipts(workbench_server)[original_calls:]
                assert helpers and all(row["helper"] and row["tool_count"] == 0 for row in helpers)
                assert run["budget"]["model_requests"] == len(helpers)
                assert (await api.get(f"/sessions/{sid}")).json()["messages"] == archive
                assert checkpoint(workbench_server, sid) == before_checkpoint
                assert (await api.get(f"/sessions/{other['id']}")).json()["runs"] == []
                await assert_basics(page)
                await screenshot(page, f"compaction-cancelled-{width}.png")
                assert_transport(page)
                assert not errors, errors
                await context.close()
            finally:
                await browser.close()


@pytest.mark.browser
async def test_pending_compaction_response_never_retargets_cancel_to_next_queued_run(workbench_server):
    async with client_for(workbench_server) as api:
        session = await create_session(api, workbench_server, "摘要取消竞态验收")
        sid = session["id"]
        for index in range(8):
            response = await api.post(f"/sessions/{sid}/runs", json={"text": f"测试长对话 {index} QA_BLOCK_SUMMARY_UNTIL_CANCEL"})
            assert response.status_code == 202, response.text
            assert (await wait_run(api, response.json()["id"]))["status"] == "completed"
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            release_response, backend_finished = asyncio.Event(), asyncio.Event()
            try:
                context, page, errors = await open_workbench(browser, workbench_server, width=1440, height=900)
                await page.get_by_role("button", name="摘要取消竞态验收", exact=True).click()
                await expect(page.get_by_test_id("message-user")).to_have_count(8)
                cancel_requests = []
                page.on("request", lambda request: cancel_requests.append(request.url)
                        if request.method == "POST" and request.url.endswith("/cancel") else None)

                async def delay_compact_response(route):
                    # Delay the genuine finished response at the transport
                    # boundary; the helper, queue and cancellation stay real.
                    result = await route.fetch()
                    backend_finished.set()
                    await release_response.wait()
                    await route.fulfill(response=result)

                await page.route(f"**/sessions/{sid}/compact", delay_compact_response)
                await page.get_by_role("button", name="更多输入选项", exact=True).click()
                await page.get_by_role("button", name=re.compile("模型摘要压缩")).click()
                compact_cancel = page.get_by_role("button", name="取消压缩", exact=True)
                await expect(compact_cancel).to_be_enabled(timeout=10000)
                detail = (await api.get(f"/sessions/{sid}")).json()
                helper_id = next(run["id"] for run in detail["runs"] if run["status"] == "running")
                assert (await api.get(f"/runs/{helper_id}")).json()["kind"] == "compaction"

                queued = await api.post(f"/sessions/{sid}/runs", json={"text": "测试取消：摘要结束后的普通排队任务"})
                assert queued.status_code == 202, queued.text
                next_id = queued.json()["id"]
                assert (await api.get(f"/runs/{next_id}")).json()["status"] == "queued"
                # Another client ends the helper. This UI has not issued cancel
                # and still awaits /compact when its poll observes the next run.
                ended = await api.post(f"/runs/{helper_id}/cancel")
                assert ended.status_code == 200 and ended.json()["accepted"]
                assert (await wait_run(api, helper_id))["status"] == "cancelled"
                assert (await wait_run(api, next_id, status="running"))["kind"] == "task"
                await asyncio.wait_for(backend_finished.wait(), 10)
                await expect(page.get_by_text("离线验收：任务正在等待，可随时停止。", exact=True)).to_be_visible(timeout=10000)
                await expect(compact_cancel).to_be_disabled()
                assert cancel_requests == []
                assert (await api.get(f"/runs/{next_id}")).json()["status"] == "running"
                await screenshot(page, "compaction-queued-run-cancel-isolation.png")

                async with page.expect_response(lambda value: value.url.endswith(f"/sessions/{sid}/compact")) as completed:
                    release_response.set()
                result = await (await completed.value).json()
                assert result["cancelled"] and not result["changed"]
                await expect(compact_cancel).to_have_count(0)
                ordinary_stop = page.get_by_role("button", name="停止", exact=True)
                await expect(ordinary_stop).to_be_enabled()
                async with page.expect_response(lambda value: value.url.endswith(f"/runs/{next_id}/cancel")) as stopped:
                    await ordinary_stop.click()
                assert (await (await stopped.value).json())["accepted"]
                assert (await wait_run(api, next_id))["status"] == "cancelled"
                assert cancel_requests == [workbench_server["base_url"] + f"/runs/{next_id}/cancel"]
                await assert_basics(page)
                assert_transport(page)
                assert not errors, errors
                await context.close()
            finally:
                release_response.set()
                await browser.close()
