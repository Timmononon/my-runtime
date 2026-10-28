"""Audit fixes through real UI, authenticated HTTP and temporary offline state."""
from __future__ import annotations

import json
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml
from playwright.async_api import async_playwright, expect
from test_web_workbench import client_for, wait_run
from test_web_workbench import workbench_server as workbench_server  # noqa: PLC0414 - fixture re-export
from test_web_workbench_browser import open_settings, open_workbench, screenshot, send


@pytest.fixture
def audit_browser_server(monkeypatch, request):
    monkeypatch.setenv("ASTER_WEB_AUDIT_BROWSER_ENABLED", "1")
    return request.getfixturevalue("workbench_server")


@pytest.fixture
def catalog_fault_server(monkeypatch, request):
    monkeypatch.setenv("ASTER_WEB_CATALOG_FAULT_ONCE", "endpoint")
    return request.getfixturevalue("workbench_server")


@pytest.fixture
def owned_browser_page():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body=b"<!doctype html><title>Owned browser fixture</title><h1>Owned temporary page</h1>"
            self.send_response(200)
            self.send_header("Content-Type","text/html; charset=utf-8")
            self.send_header("Content-Length",str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass
    server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/owned"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def sidebar(page):
    if await page.get_by_role("button", name="打开导航", exact=True).is_visible() and not await page.locator(".sidebar.open").count():
        await page.get_by_role("button", name="打开导航", exact=True).click()


async def nav(page, name):
    await sidebar(page)
    await page.get_by_role("navigation", name="主导航").get_by_role("button", name=name, exact=True).click()


async def tab(dialog, name):
    await dialog.get_by_role("tab", name=name, exact=True).click()
    return dialog.get_by_role("tabpanel", name=name, exact=True)


async def geometry(page, dialog=None):
    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    if dialog:
        bounds = await dialog.evaluate("el=>({w:el.clientWidth,sw:el.scrollWidth,h:el.clientHeight,sh:el.scrollHeight})")
        assert bounds["sw"] <= bounds["w"] + 1 and bounds["sh"] <= bounds["h"] + 1, bounds
        panel = dialog.locator(".settings-content")
        assert await panel.evaluate("el=>el.scrollWidth <= el.clientWidth + 1")


@pytest.mark.browser
@pytest.mark.parametrize("width", [1440, 390])
async def test_default_assistant_catalog_error_and_existing_deepseek_search(catalog_fault_server, width):
    server = catalog_fault_server
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context, page, errors = await open_workbench(browser, server, width=width, height=900)
            agent = page.get_by_label("新对话 Agent", exact=True)
            await expect(agent.locator("option:checked")).to_have_text("默认助手")
            await expect(agent).to_have_value("default")
            await sidebar(page)
            dialog = await open_settings(page)
            async with page.expect_response(lambda response: response.url.endswith("/deepseek/models/fetch")) as failed:
                await dialog.get_by_role("button", name="获取模型", exact=True).click()
            response = await failed.value
            assert response.status == 502 and (await response.json())["category"] == "endpoint"
            catalog = dialog.get_by_role("region", name="模型目录", exact=True)
            await expect(catalog.get_by_role("alert")).to_contain_text("DNS")
            await expect(catalog).to_contain_text("手动添加")
            await expect(catalog.get_by_label("手动添加模型 ID", exact=True)).to_be_enabled()
            await expect(catalog.get_by_role("button", name="移除模型 deepseek-flash", exact=True)).to_be_visible()
            # The saved model and key remain usable after the failed catalog read.
            panel = await tab(dialog, "搜索与记忆")
            await expect(panel).to_contain_text("未配置，网页搜索不可用")
            await panel.get_by_label("搜索后端", exact=True).select_option("deepseek")
            await expect(panel.get_by_label("DeepSeek 搜索连接", exact=True)).to_have_value("deepseek")
            await expect(panel.get_by_label("搜索模型", exact=True)).to_have_value("deepseek-flash")
            await expect(panel).to_contain_text("每次搜索计入 DeepSeek API 用量")
            await expect(panel.get_by_label("凭证引用", exact=True)).to_have_count(0)
            await expect(panel.get_by_label("服务地址", exact=True)).to_have_count(0)
            await panel.get_by_label("结果数量", exact=True).fill("3")
            await panel.get_by_label("搜索输出上限（tokens）", exact=True).fill("2048")
            await panel.get_by_label("每次搜索的联网次数上限", exact=True).fill("2")
            async with (
                page.expect_request(lambda request: request.method == "PATCH" and request.url.endswith("/management")) as submitted,
                page.expect_response(lambda response: response.request.method == "PATCH" and response.url.endswith("/management")) as saved,
            ):
                await panel.get_by_role("button", name="保存搜索、记忆与浏览器", exact=True).click()
            configuration = (await submitted.value).post_data_json["web_search"]
            assert configuration == {"backend": "deepseek", "provider_id": "deepseek", "model": "deepseek-flash",
                "timeout_seconds": 30, "result_limit": 3, "max_tokens": 2048, "max_uses": 2}
            assert (await saved.value).status == 200
            await expect(panel).to_contain_text("已配置（尚未在线验证）")
            await geometry(page, dialog)
            await dialog.get_by_role("button", name="关闭", exact=True).click()
            await sidebar(page)
            dialog = await open_settings(page)
            panel = await tab(dialog, "搜索与记忆")
            await expect(panel.get_by_label("搜索模型", exact=True)).to_have_value("deepseek-flash")
            await expect(panel.get_by_label("搜索输出上限（tokens）", exact=True)).to_have_value("2048")
            await expect(panel.get_by_label("每次搜索的联网次数上限", exact=True)).to_have_value("2")
            await tab(dialog, "模型提供商")
            async with page.expect_response(lambda response: response.url.endswith("/deepseek/models/fetch")) as retried:
                await dialog.get_by_role("button", name="获取模型", exact=True).click()
            assert (await retried.value).status == 200
            await expect(dialog.get_by_role("region", name="模型目录", exact=True)).to_contain_text("离线目录模型")
            await expect(dialog.locator(".catalog-issue")).to_have_count(0)
            await dialog.get_by_role("button", name="关闭", exact=True).click()
            # A user-defined name on the same default ID must remain intact.
            async with client_for(server) as api:
                management = (await api.get("/management")).json()
                default = next(item for item in management["agents"] if item["id"] == "default")
                renamed = await api.put("/management/agents/default", json={"expected_revision": default["revision"], "name": "我的自定义助手"})
                assert renamed.status_code == 200, renamed.text
                events = (await api.get("/events?limit=1000")).json()["events"]
                assert not [event for event in events if event["type"] == "model.request"]
                assert (await api.get("/sessions")).json()["sessions"] == []
                assert management["extensions"]["web_search"] == configuration
            await page.reload()
            await expect(agent.locator("option:checked")).to_have_text("我的自定义助手")
            await expect(agent).to_have_value("default")
            assert errors == []
            await context.close()
        finally:
            await browser.close()


@pytest.mark.browser
async def test_settings_load_after_disabling_search_without_a_credential(workbench_server):
    # Reproduce the real save path that used to leave {credential_ref: null}.
    # No external search is dispatched and all config belongs to this fixture.
    async with client_for(workbench_server) as api:
        initial = await api.get("/management")
        assert initial.status_code == 200
        enabled = await api.patch("/management", json={"expected_revision": initial.json()["revision"],
            "web_search": {"backend": "searxng", "base_url": "https://search.example", "credential_ref": None,
                           "timeout_seconds": 15, "result_limit": 10}})
        assert enabled.status_code == 200, enabled.text
        disabled = await api.patch("/management", json={"expected_revision": enabled.json()["revision"], "web_search": {}})
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context, page, errors = await open_workbench(browser, workbench_server)
            async def loaded_settings():
                dialog = await open_settings(page)
                async with page.expect_response(lambda response: response.request.method == "GET" and response.url.endswith("/management")) as loaded:
                    panel = await tab(dialog, "Agents")
                response = await loaded.value
                assert response.status == 200, await response.text()
                await expect(panel.get_by_role("button", name="创建 Agent", exact=True)).to_be_visible()
                panel = await tab(dialog, "MCP 服务")
                await expect(panel.get_by_role("button", name="添加 MCP 服务", exact=True)).to_be_visible()
                panel = await tab(dialog, "搜索与记忆")
                await expect(panel.get_by_label("搜索后端", exact=True)).to_have_value("")
                await expect(panel.get_by_label("启用语义检索", exact=True)).not_to_be_checked()
                await expect(panel.get_by_label("Embedding 运行方式", exact=True)).to_have_value("remote")
                await expect(panel.get_by_text("语义查询会将当前作用域的记忆文本发送到所选提供商；保存设置不发请求，每次查询另行授权。", exact=True)).to_be_visible()
                await expect(panel.get_by_label("Embedding 模型 ID", exact=True)).to_have_value("")
                await expect(panel.get_by_label("浏览器模式", exact=True)).to_have_value("isolated")
                await expect(dialog.get_by_role("alert")).to_have_count(0)
                await expect(page.get_by_text("Management fields are invalid", exact=False)).to_have_count(0)
                return dialog, panel
            dialog, panel = await loaded_settings()
            assert disabled.status_code == 200, disabled.text
            assert disabled.json()["extensions"]["web_search"] == {}
            async with page.expect_response(lambda response: response.request.method == "GET" and response.url.endswith("/management")) as refreshed:
                await panel.get_by_role("button", name="重新读取配置", exact=True).click()
            assert (await refreshed.value).status == 200
            await expect(panel.get_by_label("搜索后端", exact=True)).to_have_value("")
            await dialog.get_by_role("button", name="关闭", exact=True).click()
            dialog, _panel = await loaded_settings()
            await expect(page.get_by_role("alert")).to_have_count(0)
            saved = yaml.safe_load((Path(workbench_server["data_dir"]) / "config.yaml").read_text(encoding="utf-8"))
            assert saved["web_search"] == {}
            assert errors == []
            # A new independent API connection verifies that loading settings did
            # not execute search, embeddings, MCP, or a model request.
            async with client_for(workbench_server) as inspection:
                events = (await inspection.get("/events?limit=1000")).json()["events"]
                assert not [event for event in events if event["type"] == "model.request"]
                assert (await inspection.get("/sessions")).json()["sessions"] == []
            await context.close()
        finally:
            await browser.close()


@pytest.mark.browser
@pytest.mark.parametrize("width", [1440, 390])
async def test_audit_management_forms_agent_defaults_and_optional_mcp_failure(workbench_server, width):
    async with client_for(workbench_server) as api:
        created = await api.post("/providers", json={"name": "Offline custom", "base_url": "https://fixture.example/v1",
            "protocol": "openai_chat", "models": [{"id": "audit-agent", "effort_levels": ["low", "high"]}],
            "api_key": "synthetic-audit-provider-key"})
        assert created.status_code == 201, created.text
        pid = created.json()["id"]
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context, page, errors = await open_workbench(browser, workbench_server, width=width, height=900)
            await sidebar(page)
            dialog = await open_settings(page)
            panel = await tab(dialog, "Agents")
            await panel.get_by_role("button", name="创建 Agent", exact=True).click()
            await panel.get_by_label("Agent ID", exact=True).fill("audit")
            await panel.get_by_label("显示名称", exact=True).fill("审查助手")
            await panel.get_by_label("Agent 指令", exact=True).fill("OFFLINE_AUDIT_AGENT_INSTRUCTIONS")
            await panel.get_by_label("提供商", exact=True).select_option(pid)
            await panel.get_by_label("模型", exact=True).select_option("audit-agent")
            await panel.get_by_label("推理强度", exact=True).select_option("low")
            await panel.get_by_label("固定 Skill", exact=True).select_option("qa-selected-skill")
            await panel.get_by_label("禁止工具", exact=True).fill("shell_exec")
            async with page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/management/agents")) as saved:
                await panel.get_by_role("button", name="保存 Agent", exact=True).click()
            assert (await saved.value).status == 201
            await expect(panel.get_by_role("button", name="审查助手", exact=True)).to_be_visible()
            await panel.get_by_label("显示名称", exact=True).fill("审查助手已编辑")
            async with page.expect_response(lambda response: response.request.method == "PUT" and response.url.endswith("/management/agents/audit")) as saved:
                await panel.get_by_role("button", name="保存 Agent", exact=True).click()
            assert (await saved.value).status == 200
            await geometry(page, dialog)
            await dialog.locator(".settings-content").evaluate("el=>el.scrollTop=0")
            await screenshot(page, f"audit-settings-agents-{width}.png")
            panel = await tab(dialog, "MCP 服务")
            await panel.get_by_role("button", name="添加 MCP 服务", exact=True).click()
            await panel.get_by_label("服务名称", exact=True).fill("unavailable")
            await panel.get_by_label("可执行文件", exact=True).fill(str(Path(workbench_server["cwd"]) / "missing-mcp.exe"))
            await panel.get_by_label("发现预算（秒）", exact=True).fill("0.1")
            async with page.expect_response(lambda response: response.request.method == "PATCH" and response.url.endswith("/management")) as saved:
                await panel.get_by_role("button", name="保存MCP 服务", exact=True).click()
            assert (await saved.value).status == 200
            async with page.expect_response(lambda response: response.url.endswith("/unavailable/diagnose")) as diagnosed:
                await panel.get_by_role("button", name="连接诊断", exact=True).click()
            assert (await diagnosed.value).status == 200
            await expect(panel.locator(".mcp-health")).to_contain_text(re.compile("unavailable|retrying|exhausted"))
            async with page.expect_response(lambda response: response.url.endswith("/unavailable/reconnect")) as reconnect:
                await panel.get_by_role("button", name="重新连接", exact=True).click()
            assert (await reconnect.value).status == 200
            panel = await tab(dialog, "Hooks")
            await panel.get_by_role("button", name="添加 Hook", exact=True).click()
            # The saved post-tool hook will not execute in the greeting run.
            await panel.get_by_label("事件", exact=True).select_option("PostToolUse")
            await panel.get_by_label("程序路径", exact=True).fill(str(Path(workbench_server["cwd"]) / "audit-hook.py"))
            await panel.get_by_label("指定工具（每行一个，留空匹配所有）", exact=True).fill("unused_fixture_tool")
            async with page.expect_response(lambda response: response.request.method == "PATCH" and response.url.endswith("/management")) as saved:
                await panel.get_by_role("button", name="保存Hooks", exact=True).click()
            assert (await saved.value).status == 200
            panel = await tab(dialog, "工具权限")
            await panel.get_by_label("每次询问", exact=True).fill("file_write")
            async with page.expect_response(lambda response: response.request.method == "PATCH" and response.url.endswith("/management")) as saved:
                await panel.get_by_role("button", name="保存工具权限", exact=True).click()
            assert (await saved.value).status == 200
            panel = await tab(dialog, "搜索与记忆")
            await expect(panel.get_by_label("Embedding 运行方式", exact=True)).to_have_value("remote")
            await panel.get_by_label("启用语义检索",exact=True).check()
            await expect(panel.get_by_label("本地模型目录", exact=True)).to_have_count(0)
            await panel.get_by_label("Embedding 提供商",exact=True).select_option(pid)
            await panel.get_by_label("Embedding 模型 ID",exact=True).fill("audit-embedding")
            await panel.get_by_label("凭证名称", exact=True).fill("audit_search")
            await panel.get_by_label("密钥（仅写入）", exact=True).fill("synthetic-audit-search-key")
            async with page.expect_response(lambda response: response.request.method=="POST" and response.url.endswith("/management/credentials")) as credential:
                await panel.get_by_role("button", name="保存服务凭证", exact=True).click()
            assert (await credential.value).status==201
            await expect(panel.get_by_label("密钥（仅写入）",exact=True)).to_have_value("")
            await panel.get_by_label("搜索后端", exact=True).select_option("searxng")
            await panel.get_by_label("服务地址", exact=True).fill("https://search.fixture.example")
            await panel.get_by_label("凭证引用",exact=True).fill("keyring:aster/audit_search")
            await panel.get_by_label("浏览器模式", exact=True).select_option("bridge")
            await panel.get_by_label("本机 CDP 地址", exact=True).fill("http://127.0.0.1:9222")
            await panel.get_by_label("允许的页面 URL（每行一个，精确匹配）", exact=True).fill("https://fixture.example/")
            async with page.expect_response(lambda response: response.request.method == "PATCH" and response.url.endswith("/management")) as saved:
                await panel.get_by_role("button", name="保存搜索、记忆与浏览器", exact=True).click()
            assert (await saved.value).status == 200
            await expect(panel).to_contain_text("配置就绪（尚未在线验证）")
            await geometry(page, dialog)
            await screenshot(page, f"audit-settings-services-{width}.png")
            await dialog.get_by_role("button", name="关闭", exact=True).click()
            # Close the narrow sidebar before interacting with the composer.
            if width < 700:
                await page.get_by_role("navigation", name="主导航").get_by_role("button", name="对话", exact=True).click()
            await page.get_by_label("新对话 Agent", exact=True).select_option("audit")
            await send(page, "你好，检查所选 Agent 默认")
            await expect(page.locator(".assistant-group .markdown").last).to_contain_text("离线验收")
            async with client_for(workbench_server) as api:
                management = (await api.get("/management")).json()
                agent = next(item for item in management["agents"] if item["id"] == "audit")
                assert agent["name"] == "审查助手已编辑" and agent["selection"]["provider_id"] == pid
                assert agent["effective_permissions"]["denied_tools"] == ["shell_exec"]
                assert management["hooks"][0]["tool_names"] == ["unused_fixture_tool"]
                assert management["permissions"]["ask_tools"] == ["file_write"]
                assert management["extensions"]["web_search"]["backend"] == "searxng"
                assert management["extensions"]["web_search"]["credential_ref"] == "keyring:aster/audit_search"
                assert "synthetic-audit-search-key" not in json.dumps(management)
                assert management["extensions"]["browser"]["mode"] == "bridge"
                assert management["extensions"]["embedding"]["model"] == "audit-embedding"
                assert management["extensions"]["embedding"]["backend"] == "remote"
                assert management["extensions"]["embedding_status"]["status"] == "ready"
                session = (await api.get("/sessions")).json()["sessions"][0]
                assert session["agent_id"] == "audit"
                denied_embedding = await api.post(f"/customization/sessions/{session['id']}/memory_semantic_recall",
                    json={"arguments": {"query": "未授权的远程查询"}, "approved": False})
                assert denied_embedding.status_code == 403
                runs = (await api.get(f"/sessions/{session['id']}")).json()["runs"]
                assert (await wait_run(api, runs[0]["id"]))["status"] == "completed"
                calls = [json.loads(line) for line in (Path(workbench_server["data_dir"]) / "fixture-model-calls.jsonl").read_text(encoding="utf-8").splitlines()]
                assert calls[0]["model"] == "audit-agent" and calls[0]["provider_id"] == pid
                assert calls[0]["reasoning_effort"] == "low" and calls[0]["skill_instruction_seen"] is True
            await sidebar(page)
            await page.get_by_role("button",name="创建项目",exact=True).click()
            project_form=page.get_by_role("dialog",name="创建项目",exact=True)
            await project_form.get_by_label("项目名称",exact=True).fill("Agent 范围验收")
            await project_form.get_by_role("textbox",name=re.compile("^本地文件夹")).fill(workbench_server["cwd"])
            await project_form.get_by_role("button",name="保存项目",exact=True).click()
            await expect(project_form).not_to_be_visible()
            async with client_for(workbench_server) as api:
                project=(await api.get("/workbench/projects")).json()["projects"][0]
                default=await api.post("/sessions",json={"project_id":project["id"],"agent_id":"default"})
                assert default.status_code==201
            async def plus():
                await sidebar(page)
                row=page.locator(".project-row-wrap").filter(has=page.get_by_role("button",name="Agent 范围验收",exact=True))
                await row.hover()
                await row.get_by_role("button",name="在项目 Agent 范围验收 中新建对话",exact=True).click()
                await expect(page.get_by_role("textbox",name="输入消息",exact=True)).to_be_enabled()
            await plus()
            await expect(page.get_by_label("新对话 Agent",exact=True)).to_have_value("audit")
            async with client_for(workbench_server) as api:
                sessions=(await api.get("/sessions")).json()["sessions"]
                project_sessions=[item for item in sessions if item["project_id"]==project["id"]]
                assert len(project_sessions)==1 and project_sessions[0]["agent_id"]=="default"
            await plus()
            async with client_for(workbench_server) as api:
                assert len([item for item in (await api.get("/sessions")).json()["sessions"] if item["project_id"]==project["id"]])==1
            await page.get_by_role("textbox",name="输入消息",exact=True).fill("保留这个 Agent 项目的草稿")
            await plus()
            async with client_for(workbench_server) as api:
                project_sessions=[item for item in (await api.get("/sessions")).json()["sessions"] if item["project_id"]==project["id"]]
                assert len(project_sessions)==2 and len([item for item in project_sessions if item["agent_id"]=="audit"])==1
                parked=next(item for item in project_sessions if item["agent_id"]=="audit")
                assert (await api.get(f"/sessions/{parked['id']}/selection")).json()["model"]=="audit-agent"
            await expect(page.get_by_label("新对话 Agent",exact=True)).to_be_enabled()
            await send(page,"开始另一个 Agent 项目任务")
            await expect(page.locator(".assistant-group .markdown").last).to_contain_text("离线验收")
            async with client_for(workbench_server) as api:
                project_sessions=[item for item in (await api.get("/sessions")).json()["sessions"] if item["project_id"]==project["id"]]
                assert len(project_sessions)==3 and len([item for item in project_sessions if item["agent_id"]=="audit"])==2
                assert (await api.get(f"/sessions/{parked['id']}")).json()["messages"]==[]
            assert errors == []
            await context.close()
        finally:
            await browser.close()


@pytest.mark.browser
@pytest.mark.parametrize("width", [1440, 390])
async def test_audit_automation_memory_queries_and_real_timeline(workbench_server, width):
    prices = {"version": "offline-synthetic-only", "currency": "USD", "models": {"deepseek-flash": {
        "per_million": {"input_uncached": 1, "input_cached": 0.1, "output": 2}}}}
    (Path(workbench_server["data_dir"]) / "prices.yaml").write_text(yaml.safe_dump(prices), encoding="utf-8")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context, page, errors = await open_workbench(browser, workbench_server, width=width, height=900)
            await send(page, "测试轨迹")
            await expect(page.get_by_text("离线验收：真实只读工具批次已完成。", exact=True)).to_be_visible()
            await page.get_by_role("button", name="执行状态与性能", exact=True).click()
            panel = page.get_by_role("region", name="执行时间线与性能", exact=True)
            await expect(panel.get_by_role("heading", name="执行时间线", exact=True)).to_be_visible()
            await expect(panel.get_by_text("model.finished", exact=True)).to_have_count(2)
            await panel.locator(".timeline-batch summary").first.click()
            await expect(panel.locator(".timeline-tool code").filter(has_text=re.compile("^file_read$"))).to_be_visible()
            await expect(panel).to_contain_text("46.7%")
            await expect(panel).to_contain_text("USD")
            await geometry(page)
            await screenshot(page, f"audit-timeline-{width}.png")
            await page.get_by_role("button", name="关闭详情面板", exact=True).click()
            async with client_for(workbench_server) as api:
                sid = (await api.get("/sessions")).json()["sessions"][0]["id"]
                for entity, relation, value, entity_type, target_type in [("Mina", "所在部门", "研发部", "人员", "部门")]:
                    response = await api.post(f"/customization/sessions/{sid}/memory_record", json={"approved": True, "arguments": {
                        "entity": entity, "relation": relation, "value": value, "entity_type": entity_type, "target_type": target_type,
                        "sources": [{"kind": "user_supplied", "label":"离线操作员明确提供", "quote": f"离线操作员提供：{entity_type}{entity}的{relation}是{target_type}{value}。"}]}})
                    assert response.status_code == 200 and not response.json()["is_error"], response.text
            await nav(page, "记忆")
            await page.get_by_role("button",name="记录事实",exact=True).click()
            form=page.get_by_role("dialog",name="记录一条事实",exact=True)
            await form.get_by_label("实体",exact=True).fill("项目A")
            await form.get_by_label("关系",exact=True).fill("负责人")
            await form.get_by_label("内容",exact=True).fill("Mina")
            await form.get_by_label("来源陈述",exact=True).fill("离线操作员提供：项目项目A的负责人是人员Mina。")
            await form.locator("summary").filter(has_text="明确记录的实体类型").click()
            await form.get_by_label("起始实体类型",exact=True).fill("项目")
            await form.get_by_label("目标实体类型",exact=True).fill("人员")
            await form.get_by_role("button",name="确认记录",exact=True).click()
            await expect(form).not_to_be_visible()
            query = page.locator(".memory-query")
            await query.locator("summary").first.click()
            await query.get_by_label("记忆查询模式", exact=True).select_option("path_query")
            await query.get_by_label("起始实体", exact=True).fill("项目A")
            await query.get_by_label("关系路径（每行一跳）", exact=True).fill("负责人\n所在部门")
            await query.get_by_label("有效时间（留空为现在）",exact=True).fill("2030-01-01T00:00")
            async with page.expect_request(lambda req:req.method=="POST" and req.url.endswith("/memory_query")) as sent:
                await query.get_by_role("button", name="查询记忆", exact=True).click()
            arguments=(await sent.value).post_data_json["arguments"]
            assert arguments["scopes"]==["agent"] and arguments["path"]==["负责人","所在部门"]
            assert arguments["at"].startswith("2030-") or arguments["at"].startswith("2029-")
            await expect(query.locator(".memory-query-results")).to_contain_text("研发部")
            await query.get_by_label("记忆查询模式", exact=True).select_option("typed_neighborhood")
            await query.get_by_label("关系", exact=True).fill("负责人")
            await query.get_by_label("目标实体类型", exact=True).fill("人员")
            await query.get_by_role("button", name="查询记忆", exact=True).click()
            await expect(query.locator(".memory-query-results")).to_contain_text("Mina")
            await query.get_by_label("记忆查询模式", exact=True).select_option("aggregate")
            await query.get_by_role("button", name="查询记忆", exact=True).click()
            await expect(query.locator(".memory-query-results")).to_contain_text("聚合结果：1")
            async with page.expect_response(lambda response:"/memory?" in response.url and "scope=project" in response.url) as scoped:
                await page.get_by_label("记忆作用域",exact=True).select_option("project")
            assert (await scoped.value).status==422
            await expect(query.locator(".memory-query-results")).to_have_count(0)
            await page.get_by_role("button",name="关闭错误提示",exact=True).click()
            await page.get_by_label("记忆作用域",exact=True).select_option("agent")
            await query.get_by_label("记忆查询模式", exact=True).select_option("semantic")
            await query.get_by_label("语义查询文本", exact=True).fill("项目负责人的部门")
            semantic_requests = []
            page.on("request", lambda request: semantic_requests.append(request.post_data_json)
                    if request.method == "POST" and request.url.endswith("/memory_semantic_recall") else None)
            await query.get_by_role("button", name="查询记忆", exact=True).click()
            authorize = page.get_by_role("dialog", name="授权语义记忆查询", exact=True)
            await expect(authorize).to_be_visible()
            await expect(authorize).to_contain_text("可能产生供应商费用")
            assert semantic_requests == []
            await authorize.get_by_role("button", name="授权并查询", exact=True).click()
            await expect(page.get_by_role("alert")).to_contain_text("disabled")
            assert len(semantic_requests) == 1 and semantic_requests[0]["approved"] is True
            await expect(query.locator(".memory-query-results")).to_have_count(0)
            await page.get_by_role("button", name="关闭错误提示", exact=True).click()
            await screenshot(page, f"audit-memory-query-{width}.png")
            await nav(page, "自动化")
            await page.get_by_role("button", name="创建自动化", exact=True).click()
            form = page.get_by_role("dialog", name="创建自动化", exact=True)
            await form.get_by_label("希望 Aster 做什么", exact=True).fill("离线固定模型触发")
            await form.get_by_label("Cron 表达式").fill("0 0 1 1 *")
            await form.get_by_label("自动化模型选择", exact=True).select_option("fixed")
            await form.get_by_label("推理强度", exact=True).select_option("low")
            await form.get_by_label("固定 Skill", exact=True).select_option("qa-selected-skill")
            await form.get_by_role("button", name="创建并启用", exact=True).click()
            await expect(form).not_to_be_visible()
            async with client_for(workbench_server) as api:
                job = (await api.get("/automations")).json()["jobs"][0]
                assert job["config"]["selection_mode"] == "fixed"
                assert job["config"]["selection"]["reasoning_effort"] == "low"
                fired = await api.post(f"/fixture/automations/{job['id']}/tick")
                assert fired.status_code == 200, fired.text
                trigger = fired.json()["triggers"][0]
                assert (await wait_run(api, trigger["run_id"]))["status"] == "completed"
            await page.get_by_role("button", name="查看触发记录", exact=True).click()
            records = page.get_by_role("dialog", name="自动化触发记录", exact=True)
            await expect(records).to_contain_text("deepseek-flash")
            await expect(records).to_contain_text("qa-selected-skill")
            await expect(records).to_contain_text("low")
            await records.get_by_role("button", name="关闭", exact=True).click()
            await geometry(page)
            assert errors == []
            await context.close()
        finally:
            await browser.close()


@pytest.mark.browser
async def test_audit_browser_operator_controls_owned_cdp_without_closing_user_page(audit_browser_server, owned_browser_page, tmp_path):
    server=audit_browser_server
    with socket.socket() as probe:
        probe.bind(("127.0.0.1",0))
        port=probe.getsockname()[1]
    async with async_playwright() as playwright:
        owner=await playwright.chromium.launch_persistent_context(tmp_path/"owned-profile",headless=True,
            args=[f"--remote-debugging-port={port}","--remote-debugging-address=127.0.0.1"])
        browser=await playwright.chromium.launch(headless=True)
        try:
            owned=owner.pages[0]
            await owned.goto(owned_browser_page)
            context,page,errors=await open_workbench(browser,server)
            await send(page,"你好，准备独立的浏览器授权")
            await expect(page.locator(".assistant-group .markdown").last).to_contain_text("离线验收")
            async with client_for(server) as api:
                sid=(await api.get("/sessions")).json()["sessions"][0]["id"]
                data=(await api.get("/management")).json()
                saved=await api.patch("/management",json={"expected_revision":data["revision"],"browser":{
                    "mode":"bridge","headless":True,"cdp_endpoint":f"http://127.0.0.1:{port}","allowed_pages":[owned_browser_page]}})
                assert saved.status_code==200,saved.text
                before=(await api.get(f"/browser-control/sessions/{sid}")).json()
                assert before["attached"] is False
            dialog=await open_settings(page)
            panel=await tab(dialog,"搜索与记忆")
            controls=panel.locator(".browser-control")
            await expect(controls).to_contain_text("尚未连接")
            await controls.get_by_label("连接的已授权页面",exact=True).select_option(owned_browser_page)
            async with page.expect_request(lambda req:req.method=="POST" and req.url.endswith("/attach")) as attached:
                await controls.get_by_role("button",name="连接此页面",exact=True).click()
            body=(await attached.value).post_data_json
            assert body["approved"] is True and body["request_id"] and body["arguments"]=={"url":owned_browser_page}
            await expect(controls).to_contain_text("已连接")
            await controls.get_by_role("button",name="暂停浏览器自动化",exact=True).click()
            await expect(controls).to_contain_text("已暂停，等待你操作")
            async with client_for(server) as api:
                assert (await api.get(f"/browser-control/sessions/{sid}")).json()["paused"] is True
            await controls.get_by_role("button",name="恢复浏览器自动化",exact=True).click()
            await expect(controls).to_contain_text("自动化可继续")
            await controls.get_by_role("button",name="断开连接",exact=True).click()
            await expect(controls).to_contain_text("尚未连接")
            await expect(owned.get_by_role("heading",name="Owned temporary page",exact=True)).to_be_visible()
            assert owned.url==owned_browser_page and not owned.is_closed()
            async with client_for(server) as api:
                events=(await api.get("/events?limit=1000")).json()["events"]
                intents=[item for item in events if item["type"]=="browser.control" and item["payload"]["state"]=="prepared"]
                assert [item["payload"]["action"] for item in intents]==["attach","pause","resume","detach"]
            await screenshot(page,"audit-browser-operator.png")
            assert errors==[]
            await context.close()
        finally:
            await browser.close()
            await owner.close()
