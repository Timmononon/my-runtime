"""Lifecycle acceptance in Chromium, with staged code and temporary offline state."""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.async_api import async_playwright, expect
from test_web_workbench import client_for, create_session, wait_run
from test_web_workbench_browser import (
    assert_basics,
    assert_transport,
    attachment_image,
    open_settings,
    open_workbench,
    project_action,
    screenshot,
    send,
)


@pytest.fixture
def lifecycle_server(tmp_path):
    """Pin imports and staged static assets; use only temporary offline state."""
    root = Path(__file__).resolve().parents[1]
    source = root / "src"
    static = Path(os.environ.get("ASTER_WEB_STATIC_ROOT", root / "artifacts/validation/open-session-scroll-static")).resolve()
    assert (source / "aster/__init__.py").is_file()
    assert static.is_relative_to(root), "Lifecycle browser tests require static assets in the staged checkout"
    assert (static / "index.html").is_file(), f"Build staged UI before browser tests: {static}"
    ready, stop, log = (tmp_path / name for name in ("ready.json", "stop", "fixture.log"))
    env = {key: value for key, value in os.environ.items() if key not in {"DEEPSEEK_API_KEY", "ASTER_API_TOKEN"}}
    env.update(PYTHONPATH=str(source), PYTHONIOENCODING="utf-8", ASTER_WEB_STATIC_ROOT=str(static))
    # Checking the imported package in the child prevents an editable install from
    # silently exercising the original checkout instead of the staged implementation.
    bootstrap = (
        "import aster, pathlib, runpy, sys; "
        "assert pathlib.Path(aster.__file__).resolve().parent == pathlib.Path(sys.argv[1]); "
        "sys.argv = sys.argv[2:]; "
        "runpy.run_path(sys.argv[0], run_name='__main__')"
    )
    value = None
    with log.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(
            [sys.executable, "-c", bootstrap, str(source / "aster"), str(root / "scripts/web_fixture.py"),
             "--ready-file", str(ready), "--stop-file", str(stop), "--lifetime", "180"],
            cwd=root, env=env, stdout=output, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            deadline = time.monotonic() + 15
            while not ready.exists():
                assert process.poll() is None, log.read_text(encoding="utf-8")
                assert time.monotonic() < deadline, "Offline lifecycle fixture did not start"
                time.sleep(0.05)
            value = json.loads(ready.read_text(encoding="utf-8"))
            yield value
        finally:
            stop.touch()
            try:
                process.wait(timeout=12)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=5)
            assert process.returncode == 0, log.read_text(encoding="utf-8")
            assert value is None or not Path(value["data_dir"]).exists()


async def recorded_session(api, server, title, *, project_id=None):
    if project_id:
        response = await api.post("/sessions", json={"title": title, "project_id": project_id})
        assert response.status_code == 201, response.text
        session = response.json()
    else:
        session = await create_session(api, server, title)
    response = await api.post(f"/sessions/{session['id']}/runs", json={"text": title + " original message"})
    assert response.status_code == 202, response.text
    assert (await wait_run(api, response.json()["id"]))["status"] == "completed"
    detail = (await api.get(f"/sessions/{session['id']}")).json()
    assert len(detail["messages"]) >= 2
    return session, detail["messages"]


async def session_action(page, label):
    await page.get_by_role("button", name="管理当前对话", exact=True).click()
    await page.get_by_role("button", name=label, exact=True).click()


async def sidebar_action(page, title, label):
    manage = page.locator(".sidebar").get_by_role("button", name=f"管理对话 {title}", exact=True)
    await expect(manage).to_be_visible()
    # A visible hit target is insufficient if opacity:0 still hides the only
    # discovery affordance. Inspect it before hover or selection.
    appearance = await manage.evaluate("el => ({opacity: getComputedStyle(el).opacity, rect: el.getBoundingClientRect().toJSON()})")
    assert float(appearance["opacity"]) == 1
    assert appearance["rect"]["width"] >= 24 and appearance["rect"]["height"] >= 24
    await manage.click()
    await page.get_by_role("button", name=label, exact=True).click()


async def open_archives(page):
    dialog = await open_settings(page)
    await dialog.get_by_role("tab", name="已归档", exact=True).click()
    archive = dialog.get_by_role("region", name="已归档对话", exact=True)
    await expect(archive).to_be_visible()
    return dialog, archive


async def assert_no_recycle_navigation(page):
    sidebar = page.locator(".sidebar")
    for label in ("已归档", "已删除", "已删除项目"):
        await expect(sidebar.get_by_role("button", name=re.compile(rf"^{label}(?: \(\d+\))?$"))).to_have_count(0)


def session_row(page, title):
    # The topbar rename button carries the same name as the selected session.
    return page.locator(".sidebar").get_by_role("button", name=title, exact=True)


def project_group(page, project):
    return page.locator(".project-group").filter(
        has=page.get_by_role("button", name=project["name"], exact=True),
    )


async def new_project_conversation(page, project, *, existing=None):
    """Persist a park-only draft, reopen and clear it to test empty reuse.

    New chat now remains uncreated until send/upload or parking a real draft.
    Parking exercises registration without submitting an offline model request.
    """
    await project_action(page, project["name"], f"在项目 {project['name']} 中新建对话")
    await expect(page.get_by_label("新对话 Agent", exact=True)).to_be_enabled()
    draft = page.get_by_role("textbox", name="输入消息", exact=True)
    await draft.fill("Offline park-only draft for empty-session reuse")
    if existing:
        async with page.expect_response(lambda response: response.request.method == "GET" and urlsplit(response.url).path == f"/sessions/{existing['id']}") as loaded:
            await project_action(page, project["name"], f"在项目 {project['name']} 中新建对话")
        response = await loaded.value
        assert response.status == 200
        session = await response.json()
        assert session["id"] == existing["id"]
    else:
        async with page.expect_response(lambda response: response.request.method == "POST" and urlsplit(response.url).path == "/sessions") as created:
            await project_action(page, project["name"], f"在项目 {project['name']} 中新建对话")
        response = await created.value
        assert response.status == 201
        session = await response.json()
    assert session["project_id"] == project["id"] and session["cwd"] == project["cwd"]
    await expect(page.get_by_role("button", name=f"在项目 {project['name']} 中新建对话", exact=True)).to_be_enabled()
    # The freshly parked/reused record is first in this project's local list.
    async with page.expect_response(lambda response: response.request.method == "GET"
            and urlsplit(response.url).path == f"/sessions/{session['id']}"):
        await project_group(page, project).locator("button.recent-item").first.click()
    await expect(page.get_by_role("textbox", name="输入消息", exact=True)).to_be_enabled()
    await expect(draft).to_have_value("Offline park-only draft for empty-session reuse")
    await draft.fill("")
    await expect(project_group(page, project).locator(".recent-row.selected")).to_have_count(1)
    return session


async def assert_readonly_composer(page):
    composer = page.get_by_role("textbox", name="输入消息", exact=True)
    if await composer.count():
        await expect(composer).to_be_disabled()


@pytest.mark.browser
async def test_sidebar_archive_settings_history_unarchive_and_permanent_delete(lifecycle_server):
    async with client_for(lifecycle_server) as api:
        session, original = await recorded_session(api, lifecycle_server, "Lifecycle history")
        sid = session["id"]
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                context, page, errors = await open_workbench(browser, lifecycle_server)
                await assert_no_recycle_navigation(page)
                await screenshot(page, "permanent-lifecycle-visible-sidebar-actions.png")
                await sidebar_action(page, session["title"], "归档对话")
                await expect(session_row(page, session["title"])).to_have_count(0)
                dialog, archive = await open_archives(page)
                await expect(archive.get_by_role("button", name=f"查看归档对话 {session['title']}", exact=True)).to_be_visible()
                await screenshot(page, "permanent-lifecycle-settings-archives.png")
                await archive.get_by_role("button", name=f"查看归档对话 {session['title']}", exact=True).click()
                await expect(dialog).to_have_count(0)
                await expect(page.get_by_text("已归档的对话仅供查看", exact=False)).to_be_visible()
                await assert_readonly_composer(page)
                await expect(page.get_by_test_id("message-user")).to_contain_text("original message")
                assert (await api.get(f"/sessions/{sid}")).json()["messages"] == original
                dialog, archive = await open_archives(page)
                await archive.get_by_role("button", name=f"取消归档对话 {session['title']}", exact=True).click()
                await expect(archive.get_by_role("button", name=f"查看归档对话 {session['title']}", exact=True)).to_have_count(0)
                await dialog.get_by_role("button", name="关闭", exact=True).click()
                await expect(page.get_by_role("textbox", name="输入消息", exact=True)).to_be_enabled()
                await session_row(page, session["title"]).click()

                await sidebar_action(page, session["title"], "永久删除对话")
                dialog = page.get_by_role("dialog")
                await expect(dialog.get_by_role("heading", name="永久删除对话", exact=True)).to_be_visible()
                await dialog.get_by_role("button", name="取消", exact=True).click()
                retained = (await api.get(f"/sessions/{sid}")).json()
                assert retained["messages"] == original and retained["archived_at"] is None
                await expect(page.get_by_test_id("message-user")).to_contain_text("original message")
                await sidebar_action(page, session["title"], "永久删除对话")
                await screenshot(page, "permanent-lifecycle-delete-confirmation.png")
                async with page.expect_response(lambda response: response.request.method == "DELETE" and urlsplit(response.url).path == f"/sessions/{sid}") as deleted:
                    await dialog.get_by_role("button", name="永久删除", exact=True).click()
                assert parse_qs(urlsplit((await deleted.value).url).query) == {"permanent": ["true"]}
                assert (await deleted.value).status == 200
                assert await (await deleted.value).json() == {"deleted": True, "id": sid}
                await expect(dialog).to_have_count(0)
                await expect(session_row(page, session["title"])).to_have_count(0)
                await expect(page.get_by_role("textbox", name="输入消息", exact=True)).to_be_enabled()
                await expect(page.get_by_test_id("message-user")).to_have_count(0)
                assert (await api.get(f"/sessions/{sid}")).status_code == 404
                assert (await api.get("/sessions?state=all")).json()["sessions"] == []
                await assert_no_recycle_navigation(page)
                assert not errors, errors
                await assert_basics(page)
                assert_transport(page)
                await context.close()
            finally:
                await browser.close()


@pytest.mark.browser
async def test_touch_project_menu_creates_conversation_and_deletes_registration(lifecycle_server):
    folder = Path(lifecycle_server["cwd"]) / "touch-project"
    folder.mkdir()
    protected = folder / "user-owned.txt"
    protected.write_text("Touch project files stay unchanged.", encoding="utf-8")
    async with client_for(lifecycle_server) as api, async_playwright() as pw:
        name = "Touch project with a long name " + "项目" * 12
        response = await api.post("/workbench/projects", json={"name": name, "cwd": str(folder)})
        assert response.status_code == 201, response.text
        project = response.json()
        old, history = await recorded_session(api, lifecycle_server, "Touch retained conversation", project_id=project["id"])
        receipts = Path(lifecycle_server["data_dir"]) / "fixture-model-calls.jsonl"
        model_calls_before = receipts.read_text(encoding="utf-8")
        browser = await pw.chromium.launch()
        try:
            context, page, errors = await open_workbench(browser, lifecycle_server, width=390, height=844, has_touch=True)
            assert await page.evaluate("matchMedia('(hover: none)').matches")
            await page.get_by_role("button", name="打开导航", exact=True).tap()
            manage = page.get_by_role("button", name=f"管理项目 {name}", exact=True)
            plus = page.get_by_role("button", name=f"在项目 {name} 中新建对话", exact=True)
            delete = page.get_by_role("button", name=f"删除项目 {name}", exact=True)
            for action in (plus, delete):
                await expect(action).to_have_css("opacity", "0")
                await expect(action).to_have_css("pointer-events", "none")
            await expect(manage).to_be_in_viewport()
            await screenshot(page, "workspace-settings-hover-touch-project-default.png")
            await manage.tap()
            menu = page.get_by_role("dialog", name=f"项目 {name} 操作", exact=True)
            await expect(menu.get_by_role("button", name="新建对话", exact=True)).to_be_in_viewport()
            await expect(menu.get_by_role("button", name="删除项目", exact=True)).to_be_in_viewport()
            for action in (plus, delete):
                await expect(action).to_have_css("opacity", "0")
            await screenshot(page, "workspace-settings-hover-touch-project-menu.png")
            await menu.get_by_role("button", name="新建对话", exact=True).tap()
            await expect(page.get_by_label("新对话 Agent", exact=True)).to_be_enabled()
            assert len((await api.get("/sessions")).json()["sessions"]) == 1
            async with page.expect_response(lambda response: response.request.method == "POST" and urlsplit(response.url).path == "/sessions") as created:
                await page.locator('input[type="file"]').set_input_files({
                    "name": "touch-probe.txt", "mimeType": "text/plain", "buffer": b"Offline attachment locks the chosen assistant."})
            response = await created.value
            assert response.status == 201
            new = await response.json()
            assert new["project_id"] == project["id"] and new["cwd"] == str(folder.resolve())
            await expect(page.locator(".page-name")).to_be_enabled()
            await expect(menu).to_have_count(0)
            new_detail = (await api.get(f"/sessions/{new['id']}")).json()
            assert new_detail["messages"] == [] and new_detail["runs"] == []
            assert receipts.read_text(encoding="utf-8") == model_calls_before
            await page.get_by_role("button", name="打开导航", exact=True).tap()
            await manage.tap()
            await menu.get_by_role("button", name="删除项目", exact=True).tap()
            dialog = page.get_by_role("dialog", name="删除项目", exact=True)
            await expect(dialog.get_by_role("button", name="取消", exact=True)).to_be_in_viewport()
            await dialog.get_by_role("button", name="取消", exact=True).tap()
            assert (await api.get("/workbench/projects")).json()["projects"][0]["id"] == project["id"]
            await manage.tap()
            await menu.get_by_role("button", name="删除项目", exact=True).tap()
            async with page.expect_response(lambda response: response.request.method == "DELETE" and urlsplit(response.url).path == f"/workbench/projects/{project['id']}") as deleted:
                await dialog.get_by_role("button", name="确认删除", exact=True).tap()
            response = await deleted.value
            assert response.status == 200
            assert parse_qs(urlsplit(response.url).query) == {"permanent": ["true"]}
            result = await response.json()
            assert result["removed"] and result["permanent"] and result["preserved_sessions"] == 2
            assert result["files_preserved"] and result["history_preserved"]
            await expect(dialog).to_have_count(0)
            await expect(manage).to_have_count(0)
            assert (await api.get("/workbench/projects?include_removed=true")).json()["projects"] == []
            retained = (await api.get(f"/sessions/{old['id']}")).json()
            assert retained["project_id"] == project["id"] and retained["cwd"] == str(folder.resolve())
            assert retained["messages"] == history
            assert protected.read_text(encoding="utf-8") == "Touch project files stay unchanged."
            await session_row(page, old["title"]).tap()
            await expect(page.get_by_test_id("message-user")).to_contain_text("original message")
            await screenshot(page, "workspace-settings-hover-touch-retained-conversation.png")
            await assert_basics(page)
            assert not errors, errors
            assert_transport(page)
            await context.close()
        finally:
            await browser.close()


@pytest.mark.browser
async def test_busy_session_actions_disabled_project_registration_can_be_deleted(lifecycle_server):
    async with client_for(lifecycle_server) as api:
        response = await api.post("/workbench/projects", json={
            "name": "Busy project", "cwd": lifecycle_server["cwd"],
        })
        assert response.status_code == 201, response.text
        project = response.json()
        session, original = await recorded_session(api, lifecycle_server, "Busy conversation", project_id=project["id"])
        response = await api.post(f"/sessions/{session['id']}/runs", json={"text": "测试取消"})
        assert response.status_code == 202, response.text
        run = response.json()
        await wait_run(api, run["id"], status="running")
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                context, page, errors = await open_workbench(browser, lifecycle_server)
                await session_row(page, session["title"]).click()
                await expect(page.get_by_role("button", name="停止", exact=True)).to_be_visible()
                await page.get_by_role("button", name="管理当前对话", exact=True).click()
                await expect(page.get_by_role("button", name="归档对话", exact=True)).to_be_disabled()
                await expect(page.get_by_role("button", name="永久删除对话", exact=True)).to_be_disabled()
                await page.get_by_role("button", name="管理当前对话", exact=True).click()
                await project_action(page, "Busy project", "删除项目 Busy project")
                assert "会阻止删除" not in await page.get_by_role("dialog").inner_text()
                async with page.expect_response(
                    lambda response: response.request.method == "DELETE"
                    and urlsplit(response.url).path == f"/workbench/projects/{project['id']}"
                ) as deleted:
                    await page.get_by_role("dialog").get_by_role("button", name="确认删除", exact=True).click()
                assert (await deleted.value).status == 200
                assert parse_qs(urlsplit((await deleted.value).url).query) == {"permanent": ["true"]}
                result = await (await deleted.value).json()
                assert result["removed"] and result["permanent"] and result["preserved_sessions"] == 1
                await expect(page.get_by_role("dialog")).to_have_count(0)
                assert (await api.get("/workbench/projects?include_removed=true")).json()["projects"] == []
                detail = (await api.get(f"/sessions/{session['id']}")).json()
                assert detail["archived_at"] is None and detail["deleted_at"] is None
                assert detail["project_id"] == project["id"] and detail["cwd"] == project["cwd"]
                assert detail["messages"][:len(original)] == original
                assert (await api.get(f"/runs/{run['id']}")).json()["status"] == "running"
                assert (await api.post(f"/sessions/{session['id']}/archive")).status_code == 409
                assert (await api.delete(f"/sessions/{session['id']}?permanent=true")).status_code == 409
                assert not errors, errors
                assert_transport(page)
                await context.close()
            finally:
                await browser.close()
                await api.post(f"/runs/{run['id']}/cancel")
                assert (await wait_run(api, run["id"]))["status"] == "cancelled"


@pytest.mark.browser
async def test_delayed_archive_response_preserves_other_session_selection_and_draft(lifecycle_server):
    async with client_for(lifecycle_server) as api:
        first, first_history = await recorded_session(api, lifecycle_server, "Archive target")
        second, second_history = await recorded_session(api, lifecycle_server, "Still selected")
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            release, received, delivered = asyncio.Event(), asyncio.Event(), asyncio.Event()
            try:
                context, page, errors = await open_workbench(browser, lifecycle_server)
                await session_row(page, first["title"]).click()
                await expect(page.get_by_test_id("message-user")).to_contain_text(first["title"])

                async def delay_archive(route):
                    response = await route.fetch()
                    assert response.status == 200
                    received.set()
                    await release.wait()
                    await route.fulfill(response=response)
                    delivered.set()

                await page.route(f"**/sessions/{first['id']}/archive", delay_archive)
                await sidebar_action(page, first["title"], "归档对话")
                await asyncio.wait_for(received.wait(), 5)
                await session_row(page, second["title"]).click()
                await expect(page.locator(".page-name")).to_have_text(second["title"])
                draft = page.get_by_role("textbox", name="输入消息", exact=True)
                await draft.fill("Keep this unsent draft")
                release.set()
                await asyncio.wait_for(delivered.wait(), 5)
                # The refreshed active sidebar is observable proof the delayed
                # mutation continuation finished; no arbitrary sleep is needed.
                await expect(session_row(page, first["title"])).to_have_count(0)
                await expect(page.locator(".page-name")).to_have_text(second["title"])
                await expect(draft).to_have_value("Keep this unsent draft")
                await expect(page.get_by_test_id("message-user")).to_contain_text(second["title"])
                assert (await api.get(f"/sessions/{first['id']}")).json()["messages"] == first_history
                assert (await api.get(f"/sessions/{second['id']}")).json()["messages"] == second_history
                assert not errors, errors
                assert_transport(page)
                await context.close()
            finally:
                release.set()
                await browser.close()


@pytest.mark.browser
@pytest.mark.parametrize(("width", "height"), [(1440, 1000), (390, 844)])
async def test_project_direct_delete_cancel_preserves_conversation_and_files(lifecycle_server, width, height):
    folder = Path(lifecycle_server["cwd"]) / "lifecycle-project"
    folder.mkdir()
    protected = folder / "user-owned.txt"
    protected.write_text("Preserve project files exactly.", encoding="utf-8")
    async with client_for(lifecycle_server) as api:
        name = "Permanent project with a long name to keep every action reachable " + "项目名称" * 8
        response = await api.post("/workbench/projects", json={"name": name, "cwd": str(folder)})
        assert response.status_code == 201, response.text
        project = response.json()
        session, history = await recorded_session(api, lifecycle_server, "Project conversation", project_id=project["id"])
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                context, page, errors = await open_workbench(browser, lifecycle_server, width=width, height=height)
                if width < 700:
                    await page.get_by_role("button", name="打开导航", exact=True).click()
                delete = page.get_by_role("button", name=f"删除项目 {name}", exact=True)
                plus = page.get_by_role("button", name=f"在项目 {name} 中新建对话", exact=True)
                manage = page.get_by_role("button", name=f"管理项目 {name}", exact=True)
                row_button = page.get_by_role("button", name=name, exact=True)
                row = page.locator(".project-row-wrap").filter(has=row_button)
                outside = page.get_by_role("button", name="文件与成果", exact=True)
                await outside.focus()
                await page.mouse.move(width - 2, 2)
                for action in (plus, delete):
                    await expect(action).to_have_css("opacity", "0")
                    await expect(action).to_have_css("pointer-events", "none")
                await expect(manage).to_have_css("opacity", "1")
                await expect(manage).to_be_in_viewport()
                await screenshot(page, f"workspace-settings-hover-project-default-{width}.png")
                await row.hover()
                for action in (plus, delete):
                    await expect(action).to_have_css("opacity", "1")
                    await expect(action).to_have_css("pointer-events", "auto")
                await expect(delete).to_be_in_viewport()
                appearance = await delete.evaluate("""el => {
                    const rect = el.getBoundingClientRect();
                    return {opacity: getComputedStyle(el).opacity, rect: rect.toJSON(),
                            hittable: el.contains(document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2))};
                }""")
                assert float(appearance["opacity"]) == 1
                assert appearance["rect"]["width"] >= 24 and appearance["rect"]["height"] >= 24
                assert appearance["hittable"], appearance
                await screenshot(page, f"workspace-settings-hover-project-revealed-{width}.png")
                await page.mouse.move(width - 2, 2)
                for action in (plus, delete):
                    await expect(action).to_have_css("opacity", "0")
                    await expect(action).to_have_css("pointer-events", "none")
                # Ordinary mouse selection must not leave the row actions
                # visible after the pointer moves away.
                await row_button.click()
                if width < 700:
                    await page.get_by_role("button", name="打开导航", exact=True).click()
                await page.mouse.move(width - 2, 2)
                for action in (plus, delete):
                    await expect(action).to_have_css("opacity", "0")
                    await expect(action).to_have_css("pointer-events", "none")
                # Start at the always-visible menu, then use a real keyboard
                # transition to the preceding action. Programmatic focus after
                # a mouse click would not exercise focus-visible correctly.
                await manage.focus()
                await manage.press("Shift+Tab")
                await expect(delete).to_be_focused()
                assert await delete.evaluate("el => el.matches(':focus-visible')")
                for action in (plus, delete):
                    await expect(action).to_have_css("opacity", "1")
                await delete.press("Shift+Tab")
                await expect(plus).to_be_focused()
                await screenshot(page, f"workspace-settings-hover-project-keyboard-{width}.png")
                await outside.focus()
                for action in (plus, delete):
                    await expect(action).to_have_css("opacity", "0")
                    await expect(action).to_have_css("pointer-events", "none")
                await row.hover()
                await expect(delete).to_have_css("opacity", "1")
                await delete.click()
                await expect(page.get_by_role("dialog", name=f"项目 {name} 操作", exact=True)).to_have_count(0)
                dialog = page.get_by_role("dialog")
                await expect(dialog.get_by_role("heading", name="删除项目", exact=True)).to_be_in_viewport()
                assert "会阻止删除" not in await dialog.inner_text()
                await expect(dialog.get_by_role("button", name="取消", exact=True)).to_be_in_viewport()
                await dialog.get_by_role("button", name="取消", exact=True).click()
                assert (await api.get("/workbench/projects")).json()["projects"][0]["id"] == project["id"]
                await row.hover()
                await delete.click()
                await screenshot(page, f"workspace-settings-hover-project-delete-confirmation-{width}.png")
                async with page.expect_response(lambda response: response.request.method == "DELETE" and urlsplit(response.url).path == f"/workbench/projects/{project['id']}") as deleted:
                    await dialog.get_by_role("button", name="确认删除", exact=True).click()
                assert (await deleted.value).status == 200
                assert parse_qs(urlsplit((await deleted.value).url).query) == {"permanent": ["true"]}
                result = await (await deleted.value).json()
                assert result["permanent"] and result["removed"] and result["preserved_sessions"] == 1
                assert result["history_preserved"] and result["files_preserved"]
                await expect(dialog).to_have_count(0)
                await expect(delete).to_have_count(0)
                assert (await api.get("/workbench/projects")).json()["projects"] == []
                assert (await api.get("/workbench/projects?include_removed=true")).json()["projects"] == []
                retained = (await api.get(f"/sessions/{session['id']}")).json()
                assert retained["project_id"] == project["id"] and retained["cwd"] == str(folder.resolve())
                assert retained["messages"] == history and retained["deleted_at"] is None
                assert protected.read_text(encoding="utf-8") == "Preserve project files exactly."

                await assert_no_recycle_navigation(page)
                await session_row(page, session["title"]).click()
                await expect(page.get_by_test_id("message-user")).to_contain_text("original message")
                assert (await api.get(f"/sessions/{session['id']}")).json()["messages"] == history
                assert protected.read_text(encoding="utf-8") == "Preserve project files exactly."
                assert (await api.post(f"/workbench/projects/{project['id']}/restore")).status_code == 410
                await expect(page.get_by_role("textbox", name="输入消息", exact=True)).to_be_enabled()
                await screenshot(page, f"workspace-settings-hover-retained-conversation-{width}.png")
                await assert_basics(page)
                assert not errors, errors
                assert_transport(page)
                await context.close()
            finally:
                await browser.close()


@pytest.mark.browser
async def test_narrow_settings_permanently_deletes_archived_conversation(lifecycle_server):
    async with client_for(lifecycle_server) as api:
        archived, history = await recorded_session(api, lifecycle_server, "Narrow archived history")
        active = await create_session(api, lifecycle_server, "Keep active conversation")
        response = await api.post(f"/sessions/{archived['id']}/archive")
        assert response.status_code == 200, response.text
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                context, page, errors = await open_workbench(browser, lifecycle_server, width=390, height=844)
                await page.get_by_role("button", name="打开导航", exact=True).click()
                await session_row(page, active["title"]).click()
                draft = page.get_by_role("textbox", name="输入消息", exact=True)
                await draft.fill("Keep active narrow draft")
                await page.get_by_role("button", name="打开导航", exact=True).click()
                dialog, archive = await open_archives(page)
                await expect(dialog.get_by_role("heading", name="设置", exact=True)).to_be_in_viewport()
                await expect(dialog.get_by_role("button", name="关闭", exact=True)).to_be_in_viewport()
                delete = archive.get_by_role("button", name=f"永久删除归档对话 {archived['title']}", exact=True)
                await expect(delete).to_be_in_viewport()
                await screenshot(page, "permanent-lifecycle-narrow-settings-archive.png")
                await delete.click()
                confirmation = page.get_by_role("dialog").filter(has=page.get_by_role("heading", name="永久删除对话", exact=True))
                await expect(confirmation.get_by_role("button", name="永久删除", exact=True)).to_be_in_viewport()
                await confirmation.get_by_role("button", name="取消", exact=True).click()
                retained = (await api.get(f"/sessions/{archived['id']}")).json()
                assert retained["messages"] == history and retained["archived_at"] is not None
                await delete.click()
                async with page.expect_response(lambda response: response.request.method == "DELETE" and urlsplit(response.url).path == f"/sessions/{archived['id']}") as deleted:
                    await confirmation.get_by_role("button", name="永久删除", exact=True).click()
                assert await (await deleted.value).json() == {"deleted": True, "id": archived["id"]}
                await expect(delete).to_have_count(0)
                assert (await api.get(f"/sessions/{archived['id']}")).status_code == 404
                assert (await api.get("/sessions?state=archived")).json()["sessions"] == []
                await dialog.get_by_role("button", name="关闭", exact=True).click()
                await expect(page.locator(".page-name")).to_have_text(active["title"])
                await expect(draft).to_have_value("Keep active narrow draft")
                assert (await api.get(f"/sessions/{active['id']}")).json()["messages"] == []
                await assert_no_recycle_navigation(page)
                await assert_basics(page)
                assert not errors, errors
                assert_transport(page)
                await context.close()
            finally:
                await browser.close()


@pytest.mark.browser
async def test_multiple_explicit_project_conversations_keep_project_and_cwd_after_switching(lifecycle_server):
    folders = [Path(lifecycle_server["cwd"]) / name for name in ("alpha-folder", "beta-folder")]
    for folder in folders:
        folder.mkdir()
    async with client_for(lifecycle_server) as api:
        projects = []
        for name, folder in zip(("Alpha project", "Beta project"), folders, strict=True):
            response = await api.post("/workbench/projects", json={"name": name, "cwd": str(folder)})
            assert response.status_code == 201, response.text
            projects.append(response.json())
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                context, page, errors = await open_workbench(browser, lifecycle_server)
                posts = []
                page.on("request", lambda request: posts.append(request.post_data_json) if request.method == "POST" and urlsplit(request.url).path == "/sessions" else None)
                alpha = await new_project_conversation(page, projects[0])
                for _ in range(3):
                    await new_project_conversation(page, projects[0], existing=alpha)
                beta = await new_project_conversation(page, projects[1])
                await new_project_conversation(page, projects[1], existing=beta)
                await new_project_conversation(page, projects[0], existing=alpha)
                assert len(posts) == 2
                assert {row["id"] for row in (await api.get("/sessions")).json()["sessions"]} == {alpha["id"], beta["id"]}
                await page.reload()
                await expect(page.get_by_role("textbox", name="输入消息", exact=True)).to_be_enabled()
                await new_project_conversation(page, projects[1], existing=beta)
                await new_project_conversation(page, projects[0], existing=alpha)
                assert len(posts) == 2
                for session in (alpha, beta):
                    detail = (await api.get(f"/sessions/{session['id']}")).json()
                    assert detail["project_id"] == session["project_id"] and detail["cwd"] == session["cwd"]
                    assert detail["messages"] == [] and detail["runs"] == [] and detail["plan"] is None
                assert not (Path(lifecycle_server["data_dir"]) / "fixture-model-calls.jsonl").exists()

                # Empty reuse ends after the first real message, without losing
                # that conversation's history or changing its project scope.
                async with page.expect_response(lambda response: response.request.method == "POST" and urlsplit(response.url).path == f"/sessions/{alpha['id']}/runs") as started:
                    await send(page, "项目第一次对话")
                run = await (await started.value).json()
                assert (await wait_run(api, run["id"]))["status"] == "completed"
                await expect(page.get_by_role("button", name="停止", exact=True)).to_have_count(0)
                await expect(page.get_by_test_id("message-assistant")).to_contain_text("离线验收：你好，我是 Aster。")
                history = (await api.get(f"/sessions/{alpha['id']}")).json()["messages"]
                assert any(row["message"]["role"] == "user" for row in history)
                following = await new_project_conversation(page, projects[0])
                assert following["id"] != alpha["id"]
                await new_project_conversation(page, projects[0], existing=following)
                await new_project_conversation(page, projects[1], existing=beta)
                await new_project_conversation(page, projects[0], existing=following)
                assert len(posts) == 3
                all_sessions = (await api.get("/sessions")).json()["sessions"]
                assert {row["id"] for row in all_sessions} == {alpha["id"], beta["id"], following["id"]}
                assert (await api.get(f"/sessions/{alpha['id']}")).json()["messages"] == history
                assert (await api.get(f"/sessions/{following['id']}")).json()["messages"] == []
                sidebar = page.locator(".sidebar")
                await expect(sidebar.get_by_role("button", name="Skills", exact=True)).to_be_visible()
                await expect(sidebar.get_by_text("能力", exact=True)).to_have_count(0)
                assert (await sidebar.get_by_role("button", name="Skills", exact=True).locator("svg path").get_attribute("d")).startswith("m14 6 4 4")
                assert (await sidebar.get_by_role("button", name="记忆", exact=True).locator("svg path").get_attribute("d")).startswith("M6 3h14v18H6")
                await screenshot(page, "project-empty-reuse-projects-and-sidebar-icons.png")
                await assert_basics(page)
                assert not errors, errors
                assert_transport(page)
                await context.close()
            finally:
                await browser.close()


@pytest.mark.browser
async def test_project_empty_reuse_keeps_text_draft_and_attachment_on_original_session(lifecycle_server):
    folder = Path(lifecycle_server["cwd"]) / "draft-project"
    folder.mkdir()
    async with client_for(lifecycle_server) as api, async_playwright() as pw:
        response = await api.post("/workbench/projects", json={"name": "Draft project", "cwd": str(folder)})
        assert response.status_code == 201, response.text
        project = response.json()
        browser = await pw.chromium.launch()
        try:
            context, page, errors = await open_workbench(browser, lifecycle_server)
            original = await new_project_conversation(page, project)
            draft = page.get_by_role("textbox", name="输入消息", exact=True)
            await draft.fill("Keep the original unsent project draft")
            async with page.expect_response(lambda response: response.request.method == "POST" and urlsplit(response.url).path == f"/sessions/{original['id']}/attachments") as uploaded:
                await page.locator('input[type="file"]').set_input_files({"name": "project-draft.png", "mimeType": "image/png", "buffer": attachment_image()})
            assert (await uploaded.value).status == 201
            attachment = await (await uploaded.value).json()
            await expect(page.locator(".draft-attachments")).to_contain_text("project-draft.png")
            await expect(draft).to_be_enabled()
            blank = await new_project_conversation(page, project)
            assert blank["id"] != original["id"]
            await expect(draft).to_have_value("")
            await expect(page.locator(".draft-attachments")).to_have_count(0)
            assert (await api.get(f"/sessions/{blank['id']}/attachments")).json()["attachments"] == []

            async def return_to_original():
                rows = project_group(page, project).get_by_role("button", name="新对话", exact=True)
                await expect(rows).to_have_count(2)
                async with page.expect_response(lambda response: response.request.method == "GET" and urlsplit(response.url).path == f"/sessions/{original['id']}"):
                    await rows.nth(1).click()
                await expect(draft).to_have_value("Keep the original unsent project draft")
                await expect(page.locator(".draft-attachments")).to_contain_text("project-draft.png")

            await return_to_original()
            await new_project_conversation(page, project, existing=blank)
            await return_to_original()
            files = (await api.get(f"/sessions/{original['id']}/attachments")).json()["attachments"]
            assert len(files) == 1 and files[0]["attachment_id"] == attachment["attachment_id"]
            sessions = (await api.get("/sessions")).json()["sessions"]
            assert {row["id"] for row in sessions} == {original["id"], blank["id"]}
            for session in sessions:
                detail = (await api.get(f"/sessions/{session['id']}")).json()
                assert detail["project_id"] == project["id"] and detail["cwd"] == project["cwd"]
                assert detail["messages"] == [] and detail["runs"] == []
            assert not (Path(lifecycle_server["data_dir"]) / "fixture-model-calls.jsonl").exists()
            await screenshot(page, "project-empty-reuse-original-draft-and-attachment.png")
            await assert_basics(page)
            assert not errors, errors
            assert_transport(page)
            await context.close()
        finally:
            await browser.close()


@pytest.mark.browser
async def test_project_empty_reuse_pagination_fault_and_late_lookup_preserve_other_draft(lifecycle_server):
    folder = Path(lifecycle_server["cwd"]) / "paged-project"
    folder.mkdir()
    async with client_for(lifecycle_server) as api, async_playwright() as pw:
        response = await api.post("/workbench/projects", json={"name": "Paged project", "cwd": str(folder)})
        assert response.status_code == 201, response.text
        project = response.json()
        response = await api.post("/sessions", json={"project_id": project["id"]})
        assert response.status_code == 201, response.text
        candidate = response.json()
        for index in range(101):
            await create_session(api, lifecycle_server, f"Other project distractor {index:03d}")
        active, history = await recorded_session(api, lifecycle_server, "Keep selected during lookup")
        initial = {row["id"] for row in (await api.get("/sessions?limit=100&offset=0")).json()["sessions"]}
        assert candidate["id"] not in initial
        receipts = Path(lifecycle_server["data_dir"]) / "fixture-model-calls.jsonl"
        calls_before = receipts.read_text(encoding="utf-8")
        browser = await pw.chromium.launch()
        release, received, delivered = asyncio.Event(), asyncio.Event(), asyncio.Event()
        try:
            context, page, errors = await open_workbench(browser, lifecycle_server)
            await session_row(page, active["title"]).click()
            draft = page.get_by_role("textbox", name="输入消息", exact=True)
            await draft.fill("Keep the selected conversation draft through lookup")
            posts = []
            page.on("request", lambda request: posts.append(request.post_data_json) if request.method == "POST" and urlsplit(request.url).path == "/sessions" else None)
            mode = "fault"
            offsets = []

            async def control_lookup(route):
                nonlocal mode
                query = parse_qs(urlsplit(route.request.url).query)
                if query.get("state") != ["active"]:
                    await route.continue_()
                    return
                offsets.append(int(query.get("offset", ["0"])[0]))
                if query.get("offset") != ["100"]:
                    await route.continue_()
                    return
                if mode == "fault":
                    mode = "delay"
                    await route.abort("connectionfailed")
                    return
                if mode == "delay":
                    response = await route.fetch()
                    assert response.status == 200
                    received.set()
                    await release.wait()
                    await route.fulfill(response=response)
                    delivered.set()
                    return
                await route.continue_()

            await page.route("**/sessions?**", control_lookup)
            await project_action(page, project["name"], f"在项目 {project['name']} 中新建对话")
            await draft.fill("Keep this new project draft through failed lookup")
            await project_action(page, project["name"], f"在项目 {project['name']} 中新建对话")
            await expect(page.get_by_role("alert")).to_contain_text("Failed to fetch")
            await expect(draft).to_have_value("Keep this new project draft through failed lookup")
            assert posts == []
            await project_action(page, project["name"], f"在项目 {project['name']} 中新建对话")
            await asyncio.wait_for(received.wait(), 5)
            await session_row(page, active["title"]).click()
            await expect(draft).to_have_value("Keep the selected conversation draft through lookup")
            release.set()
            await asyncio.wait_for(delivered.wait(), 5)
            await expect(page.get_by_role("button", name=f"在项目 {project['name']} 中新建对话", exact=True)).to_be_enabled()
            await expect(page.locator(".page-name")).to_have_text(active["title"])
            await expect(draft).to_have_value("Keep the selected conversation draft through lookup")
            assert posts == []
            mode = "pass"
            await new_project_conversation(page, project, existing=candidate)
            await expect(draft).to_have_value("")
            assert posts == [] and offsets.count(100) == 3 and offsets.count(0) == 3
            assert (await api.get("/sessions?limit=100&offset=100")).json()["sessions"][-1]["id"] == candidate["id"]
            await session_row(page, active["title"]).click()
            await expect(draft).to_have_value("Keep the selected conversation draft through lookup")
            assert (await api.get(f"/sessions/{active['id']}")).json()["messages"] == history
            assert receipts.read_text(encoding="utf-8") == calls_before
            failed = page.qa_network["failed"]
            assert len(failed) == 1 and failed[0][0] == "net::ERR_CONNECTION_FAILED"
            assert parse_qs(urlsplit(failed[0][1]).query).get("offset") == ["100"]
            await screenshot(page, "project-empty-reuse-paged-lookup-draft-preserved.png")
            await assert_basics(page)
            assert not errors, errors
            assert_transport(page, connection_fault=True)
            await context.close()
        finally:
            release.set()
            await browser.close()


@pytest.mark.browser
async def test_project_empty_reuse_double_click_and_late_creation_do_not_steal_selection(lifecycle_server):
    folder = Path(lifecycle_server["cwd"]) / "late-project"
    folder.mkdir()
    async with client_for(lifecycle_server) as api, async_playwright() as pw:
        response = await api.post("/workbench/projects", json={"name": "Late project", "cwd": str(folder)})
        assert response.status_code == 201, response.text
        project = response.json()
        active, history = await recorded_session(api, lifecycle_server, "Keep selected during creation")
        receipts = Path(lifecycle_server["data_dir"]) / "fixture-model-calls.jsonl"
        calls_before = receipts.read_text(encoding="utf-8")
        browser = await pw.chromium.launch()
        release, received, delivered = asyncio.Event(), asyncio.Event(), asyncio.Event()
        created = []
        try:
            context, page, errors = await open_workbench(browser, lifecycle_server)
            await session_row(page, active["title"]).click()
            draft = page.get_by_role("textbox", name="输入消息", exact=True)
            await draft.fill("Original draft before project creation")

            async def delay_creation(route):
                if route.request.method != "POST":
                    await route.continue_()
                    return
                response = await route.fetch()
                assert response.status == 201
                created.append(await response.json())
                received.set()
                await release.wait()
                await route.fulfill(response=response)
                delivered.set()

            await page.route("**/sessions", delay_creation)
            plus = page.get_by_role("button", name=f"在项目 {project['name']} 中新建对话", exact=True)
            await project_group(page, project).locator(".project-row-wrap").hover()
            await expect(plus).to_have_css("opacity", "1")
            await plus.dblclick()
            await expect(page.get_by_label("新对话 Agent", exact=True)).to_be_enabled()
            assert created == []
            await draft.fill("Deferred project draft before late registration")
            await project_group(page, project).locator(".project-row-wrap").hover()
            await plus.dblclick()
            await asyncio.wait_for(received.wait(), 5)
            await expect(plus).to_be_disabled()
            await session_row(page, active["title"]).click()
            await expect(draft).to_have_value("Original draft before project creation")
            await draft.fill("Keep this later draft after navigation")
            release.set()
            await asyncio.wait_for(delivered.wait(), 5)
            await expect(plus).to_be_enabled()
            await expect(page.locator(".page-name")).to_have_text(active["title"])
            await expect(draft).to_have_value("Keep this later draft after navigation")
            assert len(created) == 1
            blank = created[0]
            assert blank["project_id"] == project["id"] and blank["cwd"] == project["cwd"]
            await new_project_conversation(page, project, existing=blank)
            assert len(created) == 1
            await expect(draft).to_have_value("")
            await session_row(page, active["title"]).click()
            await expect(draft).to_have_value("Keep this later draft after navigation")
            assert {row["id"] for row in (await api.get("/sessions")).json()["sessions"]} == {active["id"], blank["id"]}
            detail = (await api.get(f"/sessions/{blank['id']}")).json()
            assert detail["messages"] == [] and detail["runs"] == []
            assert (await api.get(f"/sessions/{active['id']}")).json()["messages"] == history
            assert receipts.read_text(encoding="utf-8") == calls_before
            await screenshot(page, "project-empty-reuse-late-creation-selection-preserved.png")
            await assert_basics(page)
            assert not errors, errors
            assert_transport(page)
            await context.close()
        finally:
            release.set()
            await browser.close()


@pytest.mark.browser
async def test_delayed_permanent_delete_preserves_other_selection_and_draft(lifecycle_server):
    async with client_for(lifecycle_server) as api:
        target, _ = await recorded_session(api, lifecycle_server, "Delayed delete target")
        active, original = await recorded_session(api, lifecycle_server, "Selected while deleting")
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            release, received, delivered = asyncio.Event(), asyncio.Event(), asyncio.Event()
            try:
                context, page, errors = await open_workbench(browser, lifecycle_server)
                await session_row(page, active["title"]).click()
                await expect(page.get_by_test_id("message-user")).to_contain_text(active["title"])
                draft = page.get_by_role("textbox", name="输入消息", exact=True)
                await draft.fill("Keep draft through delayed deletion")

                async def delay_delete(route):
                    if route.request.method != "DELETE":
                        await route.continue_()
                        return
                    response = await route.fetch()
                    assert response.status == 200
                    received.set()
                    await release.wait()
                    await route.fulfill(response=response)
                    delivered.set()

                await page.route(f"**/sessions/{target['id']}?**", delay_delete)
                await sidebar_action(page, target["title"], "永久删除对话")
                await page.get_by_role("dialog").get_by_role("button", name="永久删除", exact=True).click()
                await asyncio.wait_for(received.wait(), 5)
                await expect(page.locator(".page-name")).to_have_text(active["title"])
                await expect(draft).to_have_value("Keep draft through delayed deletion")
                release.set()
                await asyncio.wait_for(delivered.wait(), 5)
                await expect(session_row(page, target["title"])).to_have_count(0)
                await expect(page.locator(".page-name")).to_have_text(active["title"])
                await expect(draft).to_have_value("Keep draft through delayed deletion")
                await expect(page.get_by_test_id("message-user")).to_contain_text(active["title"])
                assert (await api.get(f"/sessions/{target['id']}")).status_code == 404
                assert (await api.get(f"/sessions/{active['id']}")).json()["messages"] == original
                assert not errors, errors
                assert_transport(page)
                await context.close()
            finally:
                release.set()
                await browser.close()


@pytest.mark.browser
async def test_settings_archive_pagination_unarchives_oldest_and_ignores_late_page_after_tab_switch(lifecycle_server):
    async with client_for(lifecycle_server) as api:
        active = await create_session(api, lifecycle_server, "Older active conversation")
        archived = []
        for index in range(101):
            session = await create_session(api, lifecycle_server, f"Archived page {index:03d}")
            response = await api.post(f"/sessions/{session['id']}/archive")
            assert response.status_code == 200, response.text
            archived.append(session)
        oldest = archived[0]
        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            release, received = asyncio.Event(), asyncio.Event()
            try:
                context, page, errors = await open_workbench(browser, lifecycle_server)
                await expect(session_row(page, active["title"])).to_be_visible()
                await session_row(page, active["title"]).click()
                draft = page.get_by_role("textbox", name="输入消息", exact=True)
                await draft.fill("Keep this pagination draft")
                dialog, archive = await open_archives(page)
                archived_rows = archive.get_by_role("button", name=re.compile(r"^查看归档对话 Archived page \d{3}$"))
                await expect(archived_rows).to_have_count(100)
                await expect(archive.get_by_role("button", name=f"查看归档对话 {oldest['title']}", exact=True)).to_have_count(0)

                def is_second_page(url):
                    query = parse_qs(urlsplit(url).query)
                    return query.get("state") == ["archived"] and query.get("offset") == ["100"]

                async def delay_page(route):
                    if not is_second_page(route.request.url):
                        await route.continue_()
                        return
                    response = await route.fetch()
                    assert response.status == 200
                    received.set()
                    await release.wait()
                    await route.fulfill(response=response)

                await page.route("**/sessions?**", delay_page)
                async with page.expect_response(lambda response: is_second_page(response.url)) as late_response:
                    await archive.get_by_role("button", name="加载更多归档", exact=True).click()
                    await asyncio.wait_for(received.wait(), 5)
                    await dialog.get_by_role("tab", name="模型提供商", exact=True).click()
                    await expect(session_row(page, active["title"])).to_be_visible()
                    release.set()
                await (await late_response.value).finished()
                # Drain rendering after the real delayed response. The old page
                # must never replace the newer active-state list or its draft.
                await page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
                await expect(archived_rows).to_have_count(0)
                await expect(session_row(page, active["title"])).to_be_visible()
                await expect(draft).to_have_value("Keep this pagination draft")
                await page.unroute("**/sessions?**", delay_page)

                await dialog.get_by_role("tab", name="已归档", exact=True).click()
                await expect(archived_rows).to_have_count(100)
                await archive.get_by_role("button", name="加载更多归档", exact=True).click()
                await expect(archived_rows).to_have_count(101)
                await screenshot(page, "permanent-lifecycle-archive-pagination.png")
                await archive.get_by_role("button", name=f"取消归档对话 {oldest['title']}", exact=True).click()
                await expect(archive.get_by_role("button", name=f"查看归档对话 {oldest['title']}", exact=True)).to_have_count(0)
                await dialog.get_by_role("button", name="关闭", exact=True).click()
                await expect(page.get_by_role("textbox", name="输入消息", exact=True)).to_be_enabled()
                await expect(session_row(page, oldest["title"])).to_be_visible()
                await expect(session_row(page, active["title"])).to_be_visible()
                await expect(page.locator(".page-name")).to_have_text(active["title"])
                await expect(draft).to_have_value("Keep this pagination draft")
                restored = (await api.get(f"/sessions/{oldest['id']}")).json()
                assert restored["archived_at"] is None and restored["deleted_at"] is None
                assert restored["messages"] == [] and restored["runs"] == []
                assert not (Path(lifecycle_server["data_dir"]) / "fixture-model-calls.jsonl").exists()
                assert not errors, errors
                assert_transport(page)
                await context.close()
            finally:
                release.set()
                await browser.close()
