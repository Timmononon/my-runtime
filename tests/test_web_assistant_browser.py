"""Assistant identity through real Chromium and temporary offline Runtime state."""
from __future__ import annotations

from urllib.parse import urlsplit

import pytest
from playwright.async_api import async_playwright, expect
from test_web_audit_browser import geometry, sidebar
from test_web_workbench import client_for, wait_run
from test_web_workbench import workbench_server as workbench_server  # noqa: PLC0414 - fixture re-export
from test_web_workbench_browser import open_workbench, project_action, screenshot, send


@pytest.mark.browser
@pytest.mark.parametrize("width", [1440, 390])
async def test_assistant_selection_then_readonly_project_and_independent_sessions(workbench_server, width):
    server = workbench_server
    async with client_for(server) as api:
        management = (await api.get("/management")).json()
        created = await api.post("/management/agents", json={
            "expected_revision": management["revision"], "id": "reader", "name": "论文助手",
            "instructions": "Offline assistant identity fixture only.",
        })
        assert created.status_code == 201, created.text
        response = await api.post("/workbench/projects", json={"name": "助手显示验收", "cwd": server["cwd"]})
        assert response.status_code == 201, response.text
        project = response.json()
        response = await api.post("/sessions", json={"project_id": project["id"], "title": "既有项目对话"})
        assert response.status_code == 201, response.text
        project_session = response.json()
        response = await api.post("/sessions", json={"cwd": server["cwd"], "agent_id": "reader", "title": "既有独立对话"})
        assert response.status_code == 201, response.text
        independent = response.json()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context, page, errors = await open_workbench(browser, server, width=width, height=900)
            picker = page.get_by_label("新对话 Agent", exact=True)
            current = page.get_by_label("当前助手", exact=True)
            await expect(picker.locator("option:checked")).to_have_text("默认助手")
            await expect(current).to_have_count(0)
            await picker.select_option("reader")

            async def open_session(title, name):
                await sidebar(page)
                await page.locator(".sidebar").get_by_role("button", name=title, exact=True).click()
                await expect(page.get_by_role("textbox", name="输入消息", exact=True)).to_be_enabled()
                await expect(current).to_have_text(name)
                await expect(picker).to_have_count(0)
                await expect(page.locator(".composer select")).to_have_count(0)
                assert await current.evaluate("el => el.tagName") == "OUTPUT"
                await geometry(page)

            # Saved identity wins over the last choice for a new conversation.
            await open_session("既有项目对话", "默认助手")
            await screenshot(page, f"assistant-existing-project-{width}.png")
            await open_session("既有独立对话", "论文助手")

            # Project New chat is a creation screen too. It must not persist a
            # session or lock the assistant just by opening that screen.
            await sidebar(page)
            await project_action(page, project["name"], f"在项目 {project['name']} 中新建对话")
            await expect(picker).to_be_enabled()
            await expect(current).to_have_count(0)
            await picker.select_option("default")
            await picker.select_option("reader")
            async with client_for(server) as api:
                assert len((await api.get("/sessions")).json()["sessions"]) == 2
            await screenshot(page, f"assistant-project-before-creation-{width}.png")
            async with page.expect_response(lambda response: response.request.method == "POST"
                    and urlsplit(response.url).path == "/sessions") as created:
                await send(page, "项目加号选择助手验收")
            response = await created.value
            assert response.status == 201
            plus_session = await response.json()
            assert plus_session["project_id"] == project["id"] and plus_session["agent_id"] == "reader"
            await expect(current).to_have_text("论文助手")
            await expect(picker).to_have_count(0)
            await expect(page.locator(".assistant-group .markdown").last).to_contain_text("离线验收")
            async with client_for(server) as api:
                detail = (await api.get(f"/sessions/{plus_session['id']}")).json()
                assert (await wait_run(api, detail["runs"][0]["id"]))["status"] == "completed"

            async def new_and_send(agent_id, name, text, project_id):
                await sidebar(page)
                await page.locator("button.new-chat").click()
                await expect(picker).to_be_enabled()
                await expect(current).to_have_count(0)
                await picker.select_option(agent_id)
                async with page.expect_response(lambda response: response.request.method == "POST"
                        and urlsplit(response.url).path == "/sessions") as created:
                    await send(page, text)
                response = await created.value
                assert response.status == 201
                session = await response.json()
                assert session["agent_id"] == agent_id and session["project_id"] == project_id
                await expect(current).to_have_text(name)
                await expect(picker).to_have_count(0)
                await expect(page.locator(".assistant-group .markdown").last).to_contain_text("离线验收")
                async with client_for(server) as api:
                    detail = (await api.get(f"/sessions/{session['id']}")).json()
                    assert (await wait_run(api, detail["runs"][0]["id"]))["status"] == "completed"
                    assert detail["agent_id"] == agent_id
                await geometry(page)
                return session

            # The global New chat preserves project context and permits selection
            # until the session is created by the first send (or upload).
            project_new = await new_and_send("default", "默认助手", "新项目助手验收", project["id"])
            await screenshot(page, f"assistant-new-project-{width}.png")
            await open_session("既有独立对话", "论文助手")
            standalone_new = await new_and_send("reader", "论文助手", "新独立助手验收", None)
            await screenshot(page, f"assistant-new-independent-{width}.png")
            await open_session("既有项目对话", "默认助手")
            await open_session("新独立助手验收", "论文助手")

            # A renamed default keeps its real name in old and new conversations.
            async with client_for(server) as api:
                management = (await api.get("/management")).json()
                default = next(agent for agent in management["agents"] if agent["id"] == "default")
                renamed = await api.put("/management/agents/default", json={
                    "expected_revision": default["revision"], "name": "我的默认助手",
                })
                assert renamed.status_code == 200, renamed.text
            await page.reload()
            await expect(picker.locator('option[value="default"]')).to_have_text("我的默认助手")
            await open_session("既有项目对话", "我的默认助手")
            await open_session("新项目助手验收", "我的默认助手")
            await open_session("既有独立对话", "论文助手")
            async with client_for(server) as api:
                for session in (project_session, independent, plus_session, project_new, standalone_new):
                    saved = (await api.get(f"/sessions/{session['id']}")).json()
                    assert saved["agent_id"] == session["agent_id"]
                    assert saved["project_id"] == session["project_id"]
            assert errors == []
            await context.close()
        finally:
            await browser.close()


@pytest.mark.browser
@pytest.mark.parametrize("width", [1440, 390])
async def test_project_draft_reuses_chosen_assistant_only_and_upload_locks_identity(workbench_server, width):
    server = workbench_server
    async with client_for(server) as api:
        state = (await api.get("/management")).json()
        created = await api.post("/management/agents", json={
            "expected_revision": state["revision"], "id": "reader", "name": "论文助手",
            "instructions": "Offline chosen-assistant reuse fixture only.",
        })
        assert created.status_code == 201, created.text
        project = (await api.post("/workbench/projects", json={"name": "助手复用验收", "cwd": server["cwd"]})).json()
        original = {}
        for agent_id in ("default", "reader"):
            response = await api.post("/sessions", json={"project_id": project["id"], "agent_id": agent_id})
            assert response.status_code == 201, response.text
            original[agent_id] = response.json()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context, page, errors = await open_workbench(browser, server, width=width, height=900)
            posts = []
            page.on("request", lambda request: posts.append(request.post_data_json)
                if request.method == "POST" and urlsplit(request.url).path == "/sessions" else None)
            picker = page.get_by_label("新对话 Agent", exact=True)
            current = page.get_by_label("当前助手", exact=True)
            for _ in range(2):
                await sidebar(page)
                await project_action(page, project["name"], f"在项目 {project['name']} 中新建对话")
                await expect(picker).to_be_enabled()
                assert posts == []
            await picker.select_option("reader")
            text = "复用所选助手的空白会话"
            async with page.expect_response(lambda response: response.request.method == "POST"
                    and urlsplit(response.url).path == f"/sessions/{original['reader']['id']}/runs") as started:
                await send(page, text)
            assert (await started.value).status == 202
            accepted = await (await started.value).json()
            await expect(current).to_have_text("论文助手")
            await expect(picker).to_have_count(0)
            await expect(page.locator(".assistant-group .markdown").last).to_contain_text("离线验收")
            async with client_for(server) as api:
                run = await wait_run(api, accepted["id"])
                assert run["status"] == "completed"
                assert (await api.get(f"/sessions/{original['default']['id']}")).json()["messages"] == []
                assert len((await api.get("/sessions")).json()["sessions"]) == 2
            await sidebar(page)
            await project_action(page, project["name"], f"在项目 {project['name']} 中新建对话")
            await picker.select_option("default")
            async with page.expect_response(lambda response: response.request.method == "POST"
                    and urlsplit(response.url).path == f"/sessions/{original['default']['id']}/attachments") as uploaded:
                await page.locator('input[type="file"]').set_input_files({
                    "name": "assistant-lock.txt", "mimeType": "text/plain", "buffer": b"Offline assistant lock probe."})
            assert (await uploaded.value).status == 201
            await expect(current).to_have_text("默认助手")
            await expect(picker).to_have_count(0)
            await expect(page.locator(".draft-attachments")).to_contain_text("assistant-lock.txt")
            assert posts == []
            async with client_for(server) as api:
                sessions = (await api.get("/sessions")).json()["sessions"]
                assert {item["id"] for item in sessions} == {item["id"] for item in original.values()}
                for agent_id, session in original.items():
                    saved = (await api.get(f"/sessions/{session['id']}")).json()
                    assert saved["agent_id"] == agent_id and saved["project_id"] == project["id"]
            await geometry(page)
            await screenshot(page, f"assistant-project-upload-locked-{width}.png")
            assert errors == []
            await context.close()
        finally:
            await browser.close()
