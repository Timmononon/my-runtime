"""Real Chromium history positioning, reading intent and navigation isolation."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright, expect
from test_web_session_lifecycle_browser import lifecycle_server as lifecycle_server  # noqa: PLC0414 - fixture re-export
from test_web_workbench import client_for, create_session, wait_run
from test_web_workbench_browser import assert_basics, assert_transport, open_workbench, screenshot, send


async def long_history(api, server, title):
    session = await create_session(api, server, title)
    for text in (f"测试长对话 {title}", f"{title} final saved message"):
        response = await api.post(f"/sessions/{session['id']}/runs", json={"text": text})
        assert response.status_code == 202, response.text
        assert (await wait_run(api, response.json()["id"]))["status"] == "completed"
    response = await api.get(f"/sessions/{session['id']}")
    assert response.status_code == 200, response.text
    detail = response.json()
    assert len(detail["messages"]) == 4 and len(detail["runs"]) == 2
    return session, detail


async def navigation_action(page, name):
    toggle = page.get_by_role("button", name="打开导航", exact=True)
    if await toggle.count() and "open" not in (await page.locator(".sidebar").get_attribute("class")).split():
        await toggle.click()
    await page.locator(".sidebar").get_by_role("button", name=name, exact=True).click()


async def open_history(page, session):
    await navigation_action(page, session["title"])
    await expect(page.locator(".page-name")).to_have_text(session["title"])
    await expect(page.locator(".loading-history")).to_have_count(0)
    await expect(page.get_by_test_id("message-user")).to_have_count(2)
    await expect(page.get_by_test_id("message-assistant")).to_have_count(2)


async def geometry(page):
    return await page.locator(".conversation-scroll").evaluate("""el => {
        const last = el.querySelectorAll('[data-testid="message-assistant"]');
        const rect = el.getBoundingClientRect();
        return {scrollTop:el.scrollTop,scrollHeight:el.scrollHeight,clientHeight:el.clientHeight,
            gap:el.scrollHeight-el.clientHeight-el.scrollTop,
            viewport:rect.toJSON(),last:last.length ? last[last.length-1].getBoundingClientRect().toJSON() : null,
            userCount:el.querySelectorAll('[data-testid="message-user"]').length,
            assistantCount:last.length,selected:document.querySelector('.page-name')?.textContent,
            documentTop:document.scrollingElement.scrollTop,
            documentHeight:document.documentElement.scrollHeight,windowHeight:innerHeight,
            documentWidth:document.documentElement.scrollWidth,windowWidth:innerWidth};
    }""")


def evidence(name, value):
    if directory := os.environ.get("ASTER_WEB_EVIDENCE"):
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        (root / f"{name}.json").write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


async def at_bottom(page, name):
    try:
        await page.wait_for_function("""() => {
            const el = document.querySelector('.conversation-scroll');
            return el && !el.querySelector('.loading-history') &&
                el.scrollHeight-el.clientHeight > 400 && Math.abs(el.scrollHeight-el.clientHeight-el.scrollTop) <= 2;
        }""", timeout=5000)
    except PlaywrightTimeoutError:
        measured = await geometry(page)
        evidence(name + "-failed", measured)
        await screenshot(page, name + "-failed.png")
        raise AssertionError(f"History should open at its saved end: {measured}") from None
    measured = await geometry(page)
    evidence(name, measured)
    assert measured["scrollHeight"] - measured["clientHeight"] > 400
    assert abs(measured["gap"]) <= 2
    assert measured["last"]["top"] >= measured["viewport"]["top"] - 1
    assert measured["last"]["bottom"] <= measured["viewport"]["bottom"] + 1
    assert measured["documentTop"] == 0
    assert measured["documentHeight"] <= measured["windowHeight"] + 1
    assert measured["documentWidth"] <= measured["windowWidth"] + 1
    await expect(page.get_by_test_id("message-assistant").last).to_be_in_viewport(ratio=1)
    await expect(page.get_by_role("button", name="回到最新消息", exact=True)).to_have_count(0)
    return measured


async def read_from_top(page):
    scroller = page.locator(".conversation-scroll")
    await scroller.hover()
    await page.mouse.wheel(0, -100000)
    await page.wait_for_function("() => document.querySelector('.conversation-scroll').scrollTop <= 1")
    await expect(page.get_by_role("button", name="回到最新消息", exact=True)).to_be_visible()
    measured = await geometry(page)
    assert measured["gap"] > 400
    return measured


@pytest.mark.browser
@pytest.mark.parametrize("width", [1440, 390])
async def test_open_reopen_switch_and_return_to_long_history_follow_saved_end(lifecycle_server, width):
    async with client_for(lifecycle_server) as api, async_playwright() as pw:
        alpha, alpha_before = await long_history(api, lifecycle_server, "Scroll Alpha")
        beta, beta_before = await long_history(api, lifecycle_server, "Scroll Beta")
        assert len(alpha_before["messages"]) == len(beta_before["messages"])
        browser = await pw.chromium.launch()
        release, received = asyncio.Event(), asyncio.Event()
        try:
            context, page, errors = await open_workbench(browser, lifecycle_server, width=width, height=844 if width == 390 else 1000)

            async def delayed_assets(route):
                response = await route.fetch()
                assert response.status == 200
                received.set()
                await release.wait()
                await route.fulfill(response=response)

            # The actual history is already committed. Hold one real ancillary
            # response so its detail renders into the loading-placeholder phase.
            await page.route(f"**/sessions/{alpha['id']}/attachments", delayed_assets)
            await navigation_action(page, alpha["title"])
            await asyncio.wait_for(received.wait(), 5)
            await expect(page.locator(".loading-history")).to_be_visible()
            await expect(page.get_by_test_id("message-user")).to_have_count(0)
            await page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
            evidence(f"open-scroll-{width}-loading-placeholder", await geometry(page))
            release.set()
            await expect(page.locator(".loading-history")).to_have_count(0)
            await expect(page.get_by_test_id("message-user")).to_have_count(2)
            await at_bottom(page, f"open-scroll-{width}-initial")
            await screenshot(page, f"open-scroll-{width}-initial.png")
            await page.unroute(f"**/sessions/{alpha['id']}/attachments", delayed_assets)

            await read_from_top(page)
            await open_history(page, beta)
            await at_bottom(page, f"open-scroll-{width}-same-count-switch")
            await read_from_top(page)
            await open_history(page, beta)
            await at_bottom(page, f"open-scroll-{width}-same-session-reopen")
            await read_from_top(page)
            await open_history(page, alpha)
            await at_bottom(page, f"open-scroll-{width}-return-alpha")
            await read_from_top(page)
            await navigation_action(page, "Skills")
            await expect(page.locator(".conversation-scroll")).to_have_count(0)
            await navigation_action(page, "对话")
            await expect(page.locator(".page-name")).to_have_text(alpha["title"])
            await at_bottom(page, f"open-scroll-{width}-management-return")
            assert (await api.get(f"/sessions/{beta['id']}")).json() == beta_before

            # An explicit send follows new output; a subsequent user wheel scroll
            # must retain reading intent through real SSE replay and HTTP refresh.
            async with page.expect_response(lambda response: response.request.method == "POST" and urlsplit(response.url).path == f"/sessions/{alpha['id']}/runs") as started:
                await send(page, "测试重连 while reading history")
            run = await (await started.value).json()
            await expect(page.locator(".streaming .markdown")).to_contain_text("离线验收")
            await read_from_top(page)
            await page.locator(".conversation-scroll").evaluate("""el => {
                window.readingScrollQA = {maxTop:el.scrollTop};
                el.addEventListener('scroll', () => window.readingScrollQA.maxTop = Math.max(window.readingScrollQA.maxTop, el.scrollTop));
            }""")
            assert (await wait_run(api, run["id"]))["status"] == "completed"
            expected = "离线验收：" + "事件重连应保持这段文字完整且只出现一次。" * 8
            await expect(page.get_by_test_id("message-assistant").last).to_contain_text(expected)
            await expect(page.locator(".streaming")).to_have_count(0)
            await expect(page.get_by_role("button", name="回到最新消息", exact=True)).to_be_visible()
            measured = await geometry(page)
            measured["maximumTopDuringUpdates"] = await page.evaluate("window.readingScrollQA.maxTop")
            evidence(f"open-scroll-{width}-reading-after-live-refresh", measured)
            assert measured["scrollTop"] <= 2 and measured["maximumTopDuringUpdates"] <= 2
            assert measured["gap"] > 400 and measured["last"]["top"] >= measured["viewport"]["bottom"]
            await page.get_by_role("button", name="回到最新消息", exact=True).click()
            await at_bottom(page, f"open-scroll-{width}-jump-bottom")
            after = (await api.get(f"/sessions/{alpha['id']}")).json()
            assert after["messages"][:4] == alpha_before["messages"]
            assert len(after["messages"]) == 6 and len(after["runs"]) == 3
            await screenshot(page, f"open-scroll-{width}-latest-message.png")
            await assert_basics(page)
            assert not errors, errors
            assert_transport(page, connection_fault=True)
            await context.close()
        finally:
            release.set()
            await browser.close()


@pytest.mark.browser
async def test_late_history_cannot_steal_selection_reading_or_resized_bottom(lifecycle_server):
    async with client_for(lifecycle_server) as api, async_playwright() as pw:
        alpha, alpha_before = await long_history(api, lifecycle_server, "Delayed Scroll Alpha")
        beta, beta_before = await long_history(api, lifecycle_server, "Selected Scroll Beta")
        browser = await pw.chromium.launch()
        release, received, delivered = asyncio.Event(), asyncio.Event(), asyncio.Event()
        try:
            context, page, errors = await open_workbench(browser, lifecycle_server)
            await open_history(page, beta)
            await at_bottom(page, "open-scroll-resize-before")
            await page.set_viewport_size({"width": 1000, "height": 740})
            await at_bottom(page, "open-scroll-resize-following")

            async def delayed_history(route):
                response = await route.fetch()
                assert response.status == 200
                assert (await response.json())["id"] == alpha["id"]
                received.set()
                await release.wait()
                await route.fulfill(response=response)
                delivered.set()

            await page.route(f"**/sessions/{alpha['id']}", delayed_history)
            await navigation_action(page, alpha["title"])
            await asyncio.wait_for(received.wait(), 5)
            await expect(page.locator(".loading-history")).to_be_visible()
            await open_history(page, beta)
            await at_bottom(page, "open-scroll-late-switch-beta")
            draft = page.get_by_role("textbox", name="输入消息", exact=True)
            await draft.fill("Keep Beta draft while old history is late")
            await read_from_top(page)
            release.set()
            await asyncio.wait_for(delivered.wait(), 5)
            # Await rendered frames after the stale HTTP response, rather than
            # passing on the selected title while it is still in transit.
            await page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
            await expect(page.locator(".page-name")).to_have_text(beta["title"])
            await expect(draft).to_have_value("Keep Beta draft while old history is late")
            await expect(page.get_by_test_id("message-user").last).to_contain_text(beta["title"])
            measured = await geometry(page)
            assert measured["scrollTop"] <= 2 and measured["gap"] > 400
            evidence("open-scroll-late-history-reading", measured)
            await page.set_viewport_size({"width": 960, "height": 700})
            await page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
            measured = await geometry(page)
            assert measured["scrollTop"] <= 2 and measured["gap"] > 400
            await expect(page.get_by_role("button", name="回到最新消息", exact=True)).to_be_visible()
            await page.get_by_role("button", name="回到最新消息", exact=True).click()
            await at_bottom(page, "open-scroll-late-history-jump-bottom")
            assert (await api.get(f"/sessions/{alpha['id']}")).json() == alpha_before
            assert (await api.get(f"/sessions/{beta['id']}")).json() == beta_before
            await screenshot(page, "open-scroll-late-history-beta-preserved.png")
            await assert_basics(page)
            assert not errors, errors
            assert_transport(page)
            await context.close()
        finally:
            release.set()
            await browser.close()
