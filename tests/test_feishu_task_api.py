"""Authenticated Feishu management and the normal Worker path with an offline event feed."""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import socket
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import uvicorn
import yaml
from playwright.async_api import async_playwright, expect
from test_web_workbench_browser import assert_basics, open_workbench, screenshot

from aster.api import create_app
from aster.customization.credentials import CredentialStore
from aster.domain import ModelTurn
from aster.engine import FakeModel
from aster.service import RunCoordinator

CHAT = "oc_" + "a" * 32
SENDER = "ou_" + "b" * 32
BOT = "ou_" + "c" * 32
APP = "cli_" + "d" * 16
CONTENT = "核对任务说明中的路径"
GROUP_NAME = "离线飞书测试群"


class OfflineFeishuFeed:
    def __init__(self):
        self.calls = 0
        self.opened = asyncio.Event()
        self.events_queue = asyncio.Queue()
        self.identity = {"app_id": APP, "bot_open_id": BOT, "generation": "a" * 64,
                         "bot_name": "离线应用机器人", "user_open_id": SENDER}

    def current_generation(self):
        return self.identity["generation"]

    async def discover(self):
        self.calls += 1
        return {"identity": dict(self.identity), "groups": [{"chat_id": CHAT, "name": GROUP_NAME}]}

    async def events(self, identity, status):
        assert identity["app_id"] == APP
        status("listening", None)
        self.opened.set()
        while True:
            yield await self.events_queue.get()

    async def submit(self, tag="e", *, sender=SENDER):
        await self.events_queue.put({
            "type": "im.message.receive_v1", "event_id": "event-" + tag,
            "message_id": "om_" + tag * 32, "chat_id": CHAT, "chat_type": "group",
            "sender_type": "user", "sender_id": sender, "message_type": "text",
            "mentions": [{"id": BOT, "key": "@_user_1", "name": "离线应用机器人"}],
            "content": "@_user_1 " + CONTENT, "create_time": str(int(time.time() * 1000)),
        })


@asynccontextmanager
async def feishu_runtime(root: Path):
    data, cwd = root / "state", root / "project"
    data.mkdir()
    cwd.mkdir()
    (data / "config.yaml").write_text(yaml.safe_dump({"cwd": str(cwd)}), encoding="utf-8")
    credentials = CredentialStore(data_dir=data)
    credentials.set("deepseek", "synthetic-feishu-triage-credential-never-used-on-network")
    feed = OfflineFeishuFeed()
    triage_inputs, workers = [], []

    def classify(messages, tools):
        assert tools == []
        triage_inputs.extend(messages)
        return ModelTurn(json.dumps({"decision": "process", "reason": "这是明确的工作请求"}))

    def worker_model(settings):
        model = FakeModel([ModelTurn("离线 Worker 已完成。")])
        workers.append(model)
        return model

    runtime = RunCoordinator(data, credentials=credentials, model_factory=worker_model,
                             browser_enabled=False, feishu_event_transport=feed)
    runtime.task_triage_model_factory = lambda settings, pin: FakeModel([classify])
    try:
        yield runtime, cwd, feed, triage_inputs, workers
    finally:
        if not runtime._closed:
            await runtime.close()


async def eventually(check, *, seconds=8):
    async with asyncio.timeout(seconds):
        while True:
            value = await check()
            if value:
                return value
            await asyncio.sleep(0.05)


async def test_feishu_management_and_worker_admission(tmp_path):
    async with feishu_runtime(tmp_path) as (runtime, cwd, feed, triage_inputs, workers):
        token = secrets.token_urlsafe(32)
        origin = "http://127.0.0.1:8765"
        app = create_app(runtime, token=token, allowed_origins={origin})
        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=origin, trust_env=False,
        ) as api:
            assert (await api.get("/feishu/sources")).status_code == 401
            assert (await api.post("/feishu/discover", json={})).status_code == 401
            assert feed.calls == 0 and not feed.opened.is_set()
            api.headers["Authorization"] = "Bearer " + token
            assert "feishu_task_sources_v1" in (await api.get("/capabilities")).json()["capabilities"]
            assert (await api.get("/feishu/sources")).json() == {"sources": []}
            assert feed.calls == 0
            discovered = (await api.post("/feishu/discover", json={})).json()
            assert discovered["groups"] == [{"chat_id": CHAT, "name": GROUP_NAME}]
            session = (await api.post("/sessions", json={"cwd": str(cwd)})).json()
            payload = {"source_session_id": session["id"], "chat_id": CHAT, "allowed_senders": [SENDER]}
            assert (await api.post("/feishu/sources", json={**payload, "enabled": True})).status_code == 422
            created = await api.post("/feishu/sources", json=payload)
            assert created.status_code == 201, created.text
            source = created.json()
            assert source["enabled"] is False
            assert not feed.opened.is_set() and not triage_inputs
            root = "/feishu/sources/" + source["id"]
            counts = feed.calls
            response = await api.get("/feishu/sources", params={"source_session_id": session["id"]})
            assert [row["id"] for row in response.json()["sources"]] == [source["id"]]
            assert (await api.get(root + "/events")).json()["events"] == []
            assert (await api.get(root + "/tickets")).json()["tickets"] == []
            assert feed.calls == counts

            enabled = await api.patch(root, json={"expected_revision": source["revision"], "enabled": True})
            assert enabled.status_code == 200, enabled.text
            assert (await api.post(f"/sessions/{session['id']}/archive")).status_code == 409
            await asyncio.wait_for(feed.opened.wait(), 5)
            await feed.submit()
            await feed.submit()

            async def completed_ticket():
                tickets = (await api.get(root + "/tickets")).json()["tickets"]
                return tickets[0] if len(tickets) == 1 and tickets[0]["state"] == "completed" else None

            ticket = await eventually(completed_ticket)
            events = (await api.get(root + "/events")).json()["events"]
            assert len(events) == 1 and events[0]["ticket_id"] == ticket["id"]
            assert events[0]["disposition"] == "accepted" and events[0]["triage"]["decision"] == "process"
            assert ticket["feishu"]["source_id"] == source["id"] and ticket["unattended"]
            assert len(triage_inputs) == 1
            assert len(workers) == 1 and len(workers[0].calls) == 1
            worker_call = workers[0].calls[0]
            assert CONTENT not in worker_call["system"]
            assert any(row.role == "user" and CONTENT in row.content for row in worker_call["messages"])
            stale_update = await api.patch(root, json={"expected_revision": source["revision"], "enabled": False})
            assert stale_update.status_code == 409


@asynccontextmanager
async def feishu_browser_server(tmp_path):
    async with feishu_runtime(tmp_path) as (runtime, cwd, feed, _triage, _workers):
        token = secrets.token_urlsafe(36)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(64)
            origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
            static = os.environ.get("ASTER_WEB_STATIC_ROOT")
            app = create_app(runtime, token=token, allowed_origins={origin},
                             static_root=Path(static) if static else None)
            server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False, proxy_headers=False))
            serving = asyncio.create_task(server.serve(sockets=[listener]))
            try:
                async with asyncio.timeout(12):
                    while not server.started:
                        if serving.done():
                            await serving
                            raise RuntimeError("Feishu browser fixture stopped before start")
                        await asyncio.sleep(0.02)
                yield {"base_url": origin, "token": token, "cwd": str(cwd)}, feed
            finally:
                server.should_exit = True
                await asyncio.wait_for(serving, 10)


@pytest.mark.browser
async def test_feishu_workbench_group_to_ticket(tmp_path):
    async with feishu_browser_server(tmp_path) as (server, feed), async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context, page, errors = await open_workbench(browser, server)
            try:
                await page.locator("nav[aria-label='主导航']").get_by_role("button", name="飞书", exact=True).click()
                await expect(page.get_by_role("heading", name="飞书", exact=True)).to_be_visible()
                await page.get_by_role("button", name="添加飞书来源", exact=True).click()
                dialog = page.get_by_role("dialog", name="添加飞书来源")
                await dialog.get_by_role("button", name="读取机器人所在群聊", exact=True).click()
                await dialog.get_by_role("combobox", name="飞书群聊").select_option(CHAT)
                await dialog.get_by_role("button", name="添加当前用户", exact=True).click()
                await dialog.get_by_role("button", name="保存来源", exact=True).click()
                await expect(dialog).not_to_be_visible()
                assert not feed.opened.is_set()
                await page.get_by_role("button", name="启用接收", exact=True).click()
                await asyncio.wait_for(feed.opened.wait(), 6)
                await feed.submit()
                await page.get_by_text("查看消息正文", exact=True).first.click(timeout=8000)
                await expect(page.get_by_text(CONTENT, exact=False).first).to_be_visible(timeout=8000)
                await expect(page.get_by_text("这是明确的工作请求", exact=False).first).to_be_visible(timeout=8000)
                await assert_basics(page)
                assert not errors
                await screenshot(page, "feishu-management.png")
                await page.get_by_role("button", name="查看关联任务").first.click()
                await expect(page.get_by_text("离线 Worker 已完成。", exact=False).first).to_be_visible(timeout=8000)
            finally:
                await context.close()
        finally:
            await browser.close()
