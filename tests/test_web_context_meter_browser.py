"""Context meter against temporary offline Runtime counts and staged frontend."""
import asyncio
import contextlib
import copy
import json
import re
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml
from playwright.async_api import async_playwright, expect
from test_web_workbench import client_for, wait_run
from test_web_workbench import workbench_server as workbench_server  # noqa: PLC0414 - fixture re-export
from test_web_workbench_browser import open_workbench, screenshot


async def sidebar(page):
    if await page.get_by_role("button", name="打开导航", exact=True).is_visible() and not await page.locator(".sidebar.open").count():
        await page.get_by_role("button", name="打开导航", exact=True).click()


async def open_saved(page, title):
    await sidebar(page)
    await page.locator(".sidebar").get_by_role("button", name=title, exact=True).click()
    await expect(page.get_by_role("textbox", name="输入消息", exact=True)).to_be_enabled()


async def inside_viewport(page, panel):
    bounds = await panel.bounding_box()
    assert bounds is not None
    viewport = page.viewport_size
    assert bounds["x"] >= 7 and bounds["y"] >= 7
    assert bounds["x"] + bounds["width"] <= viewport["width"] - 7
    assert bounds["y"] + bounds["height"] <= viewport["height"] - 7
    assert await page.evaluate("() => document.documentElement.scrollWidth <= innerWidth + 1")


@pytest.mark.browser
@pytest.mark.parametrize("width", [1440, 390])
async def test_context_meter_real_counts_compaction_and_late_session_response(workbench_server, width):
    server = workbench_server
    config = Path(server["data_dir"]) / "config.yaml"
    settings = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    settings["runtime"] = {**settings.get("runtime", {}), "context_tokens": 1_000_000,
        "max_total_tokens": 2_000_000, "reserve_tokens": 8192, "retained_context_rounds": 2,
        "semantic_compaction": False, "compaction_persist_learnings": False, "file_restore_max_files": 0}
    config.write_text(yaml.safe_dump(settings, allow_unicode=True), encoding="utf-8")
    async with client_for(server) as api:
        first = (await api.post("/sessions", json={"cwd": server["cwd"], "title": "长上下文读数"})).json()
        for index in range(4):
            response = await api.post(f"/sessions/{first['id']}/runs", json={
                "text": f"离线上下文样本 {index}: " + "offline context observation " * 5500})
            assert response.status_code == 202, response.text
            assert (await wait_run(api, response.json()["id"]))["status"] == "completed"
        second = (await api.post("/sessions", json={"cwd": server["cwd"], "title": "短上下文读数"})).json()
        run = (await api.post(f"/sessions/{second['id']}/runs", json={"text": "短上下文离线样本"})).json()
        assert (await wait_run(api, run["id"]))["status"] == "completed"
        compressible = (await api.post("/sessions", json={"cwd": server["cwd"], "title": "可压缩上下文读数"})).json()
        for index in range(8):
            run = (await api.post(f"/sessions/{compressible['id']}/runs", json={"text": f"测试长对话 {index}"})).json()
            assert (await wait_run(api, run["id"]))["status"] == "completed"
        first_value = (await api.get(f"/sessions/{first['id']}/context-usage")).json()
        second_value = (await api.get(f"/sessions/{second['id']}/context-usage")).json()
        compact_before = (await api.get(f"/sessions/{compressible['id']}/context-usage")).json()
        assert first_value["context_window"] == 1_000_000
        assert first_value["percent"] > second_value["percent"]
        original_history = copy.deepcopy((await api.get(f"/sessions/{compressible['id']}")).json()["messages"])

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        release, received, delivered = asyncio.Event(), asyncio.Event(), asyncio.Event()
        try:
            context, page, errors = await open_workbench(browser, server, width=width, height=900)
            await expect(page.get_by_role("button", name=re.compile(r"^上下文已用 \d+%$"))).to_have_count(0)
            await open_saved(page, first["title"])
            meter = page.get_by_role("button", name=f"上下文已用 {first_value['percent']}%", exact=True)
            await expect(meter).to_be_visible()
            model_button = page.get_by_role("button", name="选择模型", exact=True)
            meter_bounds, model_bounds = await meter.bounding_box(), await model_button.bounding_box()
            assert meter_bounds["x"] + meter_bounds["width"] <= model_bounds["x"] + 1
            assert abs((meter_bounds["y"] + meter_bounds["height"] / 2)
                       - (model_bounds["y"] + model_bounds["height"] / 2)) <= 1
            await expect(meter.locator("svg circle")).to_have_count(2)
            await meter.click()
            panel = page.get_by_role("dialog", name="上下文已用", exact=True)
            await expect(panel).to_be_visible()
            for label in ("系统提示词", "工具定义", "对话消息"):
                await expect(panel.get_by_text(label, exact=True)).to_be_visible()
            await expect(panel).to_contain_text("1M")
            await expect(panel).to_contain_text("当前活动上下文估算")
            await inside_viewport(page, panel)
            await screenshot(page, f"context-ring-details-{width}.png")
            await meter.press("Escape")
            await expect(panel).to_have_count(0)
            await expect(meter).to_be_focused()

            # Compression changes the model-live count, while the raw transcript
            # and the read-only view retain their own separate sources.
            await open_saved(page, compressible["title"])
            await page.get_by_role("button", name="更多输入选项", exact=True).click()
            async with page.expect_response(lambda response: response.request.method == "POST"
                    and urlsplit(response.url).path == f"/sessions/{compressible['id']}/compact") as compacted:
                await page.get_by_role("button", name=re.compile(r"^压缩对话")).click()
            assert (await compacted.value).status == 200
            async with client_for(server) as api:
                after = (await api.get(f"/sessions/{compressible['id']}/context-usage")).json()
                assert after["used_tokens"] < compact_before["used_tokens"]
                assert (await api.get(f"/sessions/{compressible['id']}")).json()["messages"] == original_history
            await expect(page.get_by_role("button", name=f"上下文已用 {after['percent']}%", exact=True)).to_be_visible()

            await open_saved(page, second["title"])
            short_meter = page.get_by_role("button", name=f"上下文已用 {second_value['percent']}%", exact=True)
            await expect(short_meter).to_be_visible()

            async def delayed(route):
                response = await route.fetch()
                received.set()
                await release.wait()
                await route.fulfill(response=response)
                delivered.set()

            await page.route(f"**/sessions/{first['id']}/context-usage", delayed)
            await open_saved(page, first["title"])
            await asyncio.wait_for(received.wait(), 5)
            await open_saved(page, second["title"])
            await expect(short_meter).to_be_visible()
            release.set()
            await asyncio.wait_for(delivered.wait(), 5)
            await expect(short_meter).to_be_visible()
            await expect(page.get_by_role("button", name=f"上下文已用 {first_value['percent']}%", exact=True)).to_have_count(0)
            await short_meter.click()
            await inside_viewport(page, panel)
            await page.mouse.click(width / 2, 80)
            await expect(panel).to_have_count(0)
            assert errors == []
            await context.close()
        finally:
            release.set()
            await browser.close()


@pytest.mark.browser
@pytest.mark.parametrize("width", [1440, 390])
async def test_context_meter_keeps_legacy_independent_and_empty_chats_visible(workbench_server, width):
    server = workbench_server
    async with client_for(server) as api:
        project = (await api.post("/workbench/projects", json={
            "name": "上下文未知读数验收", "cwd": server["cwd"]})).json()
        project_session = (await api.post("/sessions", json={
            "project_id": project["id"], "title": "空白项目上下文"})).json()
        independent = (await api.post("/sessions", json={
            "cwd": server["cwd"], "title": "既有独立上下文"})).json()
        empty = (await api.post("/sessions", json={
            "cwd": server["cwd"], "title": "空白独立上下文"})).json()
        run = (await api.post(f"/sessions/{independent['id']}/runs", json={"text": "旧对话离线样本"})).json()
        assert (await wait_run(api, run["id"]))["status"] == "completed"
        original = copy.deepcopy((await api.get(f"/sessions/{independent['id']}")).json()["messages"])

    # Simulate a pre-feature checkpoint only in this fixture's temporary DB.
    with contextlib.closing(sqlite3.connect(Path(server["data_dir"]) / "runtime.db")) as db, db:
        row = db.execute("SELECT metadata_json FROM checkpoints WHERE session_id=?", (independent["id"],)).fetchone()
        metadata = json.loads(row[0])
        assert metadata.pop("context_usage")
        db.execute("UPDATE checkpoints SET metadata_json=? WHERE session_id=?",
                   (json.dumps(metadata), independent["id"]))
    receipts = Path(server["data_dir"]) / "fixture-model-calls.jsonl"
    calls_before = receipts.read_bytes()
    async with client_for(server) as api:
        unknown = (await api.get(f"/sessions/{independent['id']}/context-usage")).json()
        assert unknown["status"] == "unavailable" and unknown["percent"] is None
        assert unknown["breakdown"]["message_tokens"] > 0

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context, page, errors = await open_workbench(browser, server, width=width, height=900)
            meter = page.get_by_role("button", name="上下文使用情况，暂无完整统计", exact=True)
            panel = page.get_by_role("dialog", name="上下文已用", exact=True)
            await expect(meter).to_be_visible()
            await expect(meter).to_have_text("—")
            await meter.click()
            await expect(panel).to_contain_text("暂无完整统计")
            await expect(panel.get_by_text("未知", exact=True)).to_have_count(4)
            await meter.press("Escape")
            await expect(panel).to_have_count(0)

            for session in (project_session, empty, independent):
                await open_saved(page, session["title"])
                await expect(meter).to_be_visible()
                await expect(meter.locator("svg circle")).to_have_count(1)
                model = page.get_by_role("button", name="选择模型", exact=True)
                meter_bounds, model_bounds = await meter.bounding_box(), await model.bounding_box()
                assert meter_bounds["x"] + meter_bounds["width"] <= model_bounds["x"] + 1
                assert abs((meter_bounds["y"] + meter_bounds["height"] / 2)
                           - (model_bounds["y"] + model_bounds["height"] / 2)) <= 1
                await meter.click()
                await expect(panel).to_be_visible()
                await expect(panel).to_contain_text("后续运行")
                await inside_viewport(page, panel)
                await expect(page.get_by_role("button", name="上下文已用 0%", exact=True)).to_have_count(0)
                if session is independent:
                    messages = panel.locator("dl > div").filter(has=page.get_by_text("对话消息", exact=True))
                    await expect(messages.locator("dd")).to_have_text(re.compile(r"^~\d"))
                    await screenshot(page, f"context-ring-legacy-independent-{width}.png")
                await meter.press("Escape")

            # Opening/clicking the meter must neither manufacture a run nor
            # migrate a checkpoint. Normal user execution supplies new counts.
            assert receipts.read_bytes() == calls_before
            async with client_for(server) as api:
                saved = (await api.get(f"/sessions/{independent['id']}")).json()
                assert saved["messages"] == original and len(saved["runs"]) == 1
                assert len((await api.get("/sessions")).json()["sessions"]) == 3
                assert (await api.get(f"/sessions/{independent['id']}/context-usage")).json()["status"] == "unavailable"

            async with page.expect_response(lambda response: response.request.method == "POST"
                    and urlsplit(response.url).path == f"/sessions/{independent['id']}/runs") as submitted:
                await page.get_by_role("textbox", name="输入消息", exact=True).fill("旧对话后续离线运行")
                await page.get_by_role("button", name="发送消息", exact=True).click()
            response = await submitted.value
            assert response.status == 202
            async with client_for(server) as api:
                assert (await wait_run(api, (await response.json())["id"]))["status"] == "completed"
                updated = (await api.get(f"/sessions/{independent['id']}/context-usage")).json()
                assert updated["status"] == "estimated" and updated["percent"] is not None
                saved = (await api.get(f"/sessions/{independent['id']}")).json()
                assert saved["messages"][:len(original)] == original
            await expect(page.get_by_role("button", name=f"上下文已用 {updated['percent']}%", exact=True)).to_be_visible()
            await expect(meter).to_have_count(0)
            assert errors == []
            await context.close()
        finally:
            await browser.close()
