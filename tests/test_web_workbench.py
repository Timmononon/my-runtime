"""Workbench black-box tests use a real HTTP server and temporary Runtime state."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest


@pytest.fixture
def workbench_server(tmp_path):
    ready, stop, log = (tmp_path / name for name in ("ready.json", "stop", "fixture.log"))
    root = Path(__file__).resolve().parents[1]
    env = {key: value for key, value in os.environ.items() if key not in {"DEEPSEEK_API_KEY", "ASTER_API_TOKEN"}}
    env["PYTHONIOENCODING"] = "utf-8"
    with log.open("w", encoding="utf-8") as output:
        process = subprocess.Popen([sys.executable, str(root / "scripts/web_fixture.py"),
                                    "--ready-file", str(ready), "--stop-file", str(stop), "--lifetime", "180"],
                                   cwd=root, env=env, stdout=output, stderr=subprocess.STDOUT,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            deadline = time.monotonic() + 15
            while not ready.exists():
                assert process.poll() is None, log.read_text(encoding="utf-8")
                assert time.monotonic() < deadline, "Offline fixture did not start"
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
            assert not Path(value["data_dir"]).exists() if "value" in locals() else True


def client_for(server):
    return httpx.AsyncClient(base_url=server["base_url"], headers={"Authorization": "Bearer " + server["token"]},
                             trust_env=False, timeout=10)


async def create_session(client, server, title="离线浏览器验收"):
    result = await client.post("/sessions", json={"cwd": server["cwd"], "title": title})
    assert result.status_code == 201, result.text
    return result.json()


async def wait_run(client, run_id, *, status=None):
    async with asyncio.timeout(12):
        while True:
            response = await client.get(f"/runs/{run_id}")
            response.raise_for_status()
            run = response.json()
            if status and run["status"] == status:
                return run
            if not status and run["status"] in {"completed", "failed", "cancelled", "blocked"}:
                return run
            await asyncio.sleep(0.03)


async def test_workbench_fixture_stream_history_and_cursor_replay(workbench_server):
    async with client_for(workbench_server) as client:
        assert (await client.get("/sessions")).json() == {"sessions": []}
        session = await create_session(client, workbench_server)
        run = (await client.post(f"/sessions/{session['id']}/runs", json={"text": "你好"})).json()
        records = []
        async with client.stream("GET", f"/runs/{run['id']}/events") as response:
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    records.append(json.loads(line[6:]))
        assert records[-1]["type"] == "run.finished"
        assert (await wait_run(client, run["id"]))["status"] == "completed"
        history = (await client.get(f"/sessions/{session['id']}")).json()["messages"]
        final = history[-1]["message"]
        assert final["role"] == "assistant" and "离线验收" in final["content"]
        deltas = [item["payload"]["text"] for item in records if item["type"] == "model.text.delta"]
        assert len(deltas) > 1 and "".join(deltas) == final["content"]
        cursor = records[len(records) // 2]["event_id"]
        replay = await client.get(f"/runs/{run['id']}/events?follow=false&after={cursor}")
        remaining = [json.loads(line[6:]) for line in replay.text.splitlines() if line.startswith("data: ")]
        assert remaining == [item for item in records if item["event_id"] > cursor]


async def test_workbench_fixture_approval_rejection_and_late_decision(workbench_server):
    async with client_for(workbench_server) as client:
        session = await create_session(client, workbench_server)
        run = (await client.post(f"/sessions/{session['id']}/runs", json={"text": "测试审批"})).json()
        pending = await wait_run(client, run["id"], status="waiting_approval")
        request = pending["pending_requests"][0]
        assert request["kind"] == "approval" and request["payload"]["tool_name"] == "file_write"
        target = Path(workbench_server["cwd"]) / request["payload"]["arguments"]["path"]
        assert not target.exists()
        assert (await client.post(f"/approvals/{request['id']}/decision", json={"approved": False})).json()["accepted"]
        assert (await wait_run(client, run["id"]))["status"] == "completed"
        assert not target.exists()
        assert (await client.post(f"/approvals/{request['id']}/decision", json={"approved": True})).json() == {"accepted": False}
        history = (await client.get(f"/sessions/{session['id']}")).json()["messages"]
        assert "未创建交付物" in history[-1]["message"]["content"]
        assert any(row["message"]["tool_calls"] and not row["message"]["content"] for row in history)


async def test_workbench_fixture_approved_download_preview_and_raw_branch_boundary(workbench_server):
    async with client_for(workbench_server) as client:
        session = await create_session(client, workbench_server)
        sid = session["id"]
        run = (await client.post(f"/sessions/{sid}/runs", json={"text": "测试审批"})).json()
        pending = await wait_run(client, run["id"], status="waiting_approval")
        request = pending["pending_requests"][0]
        await client.post(f"/approvals/{request['id']}/decision", json={"approved": True})
        assert (await wait_run(client, run["id"]))["status"] == "completed"
        deliverable = (await client.get(f"/sessions/{sid}/deliverables")).json()["deliverables"][0]
        download = await client.get(deliverable["download_path"])
        assert download.status_code == 200 and "ASTER WORKBENCH VERIFIED" in download.text
        preview = (await client.post(f"/sessions/{sid}/previews", json={"artifact_id": deliverable["artifact_id"]})).json()
        async with httpx.AsyncClient(trust_env=False) as public:
            page = await public.get(preview["preview_url"])
        assert page.status_code == 200 and 'sandbox=""' in page.text
        history = (await client.get(f"/sessions/{sid}")).json()["messages"]
        # Includes tool-only assistant and tool records, not just visible prose.
        boundary = len(history) - 1
        child = (await client.post(f"/sessions/{sid}/fork", json={"message_index": boundary})).json()
        copied = (await client.get(f"/sessions/{child['id']}")).json()["messages"]
        assert len(copied) == boundary
        for new, original in zip(copied, history[:boundary], strict=True):
            message = new["message"]
            metadata = message["metadata"]
            assert metadata["inherited_from_session"] == sid
            assert metadata["source_seq"] == original["seq"]
            assert metadata["source_message_id"] == original["id"]
            assert {key: value for key, value in message.items() if key != "metadata"} == {
                key: value for key, value in original["message"].items() if key != "metadata"}
            assert all(metadata[key] == value for key, value in original["message"]["metadata"].items())
        assert len(copied) > 4


async def test_workbench_fixture_question_cancel_and_attachment_roundtrip(workbench_server):
    async with client_for(workbench_server) as client:
        session = await create_session(client, workbench_server)
        sid = session["id"]
        first = (await client.post(f"/sessions/{sid}/runs", json={"text": "测试提问"})).json()
        pending = await wait_run(client, first["id"], status="waiting_input")
        question = pending["pending_requests"][0]
        assert question["payload"]["options"] == ["中文", "English"]
        await client.post(f"/questions/{question['id']}/answer", json={"answer": "中文"})
        assert (await wait_run(client, first["id"]))["status"] == "completed"
        second = (await client.post(f"/sessions/{sid}/runs", json={"text": "测试取消"})).json()
        await wait_run(client, second["id"], status="running")
        assert (await client.post(f"/runs/{second['id']}/cancel")).json()["accepted"]
        assert (await wait_run(client, second["id"]))["status"] == "cancelled"
        attachment = await client.post(f"/sessions/{sid}/attachments?filename=sample.txt", content=b"Source text only",
                                       headers={"Content-Type": "text/plain"})
        assert attachment.status_code == 201
        third = (await client.post(f"/sessions/{sid}/runs", json={"text": "测试附件", "attachments": [attachment.json()["attachment_id"]]})).json()
        assert (await wait_run(client, third["id"]))["status"] == "completed"
        history = (await client.get(f"/sessions/{sid}")).json()["messages"]
        assert "sample.txt" in history[-1]["message"]["content"]


@pytest.mark.parametrize("cwd_form", ["canonical", "windows-short-path"])
async def test_workbench_real_http_ticket_cookie_csrf_and_logout(workbench_server, cwd_form):
    cwd = str(Path(workbench_server["cwd"]).resolve(strict=True))
    if cwd_form == "windows-short-path":
        if os.name != "nt":
            pytest.skip("Windows 8.3 directory aliases require Windows")
        import ctypes
        from ctypes import wintypes

        short_path = ctypes.WinDLL("kernel32", use_last_error=True).GetShortPathNameW
        short_path.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        short_path.restype = wintypes.DWORD
        size = short_path(cwd, None, 0)
        if not size:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_unicode_buffer(size)
        if not short_path(cwd, buffer, size):
            raise ctypes.WinError(ctypes.get_last_error())
        if buffer.value.casefold() == cwd.casefold():
            pytest.skip("Temporary directory has no Windows 8.3 alias")
        cwd = buffer.value
    origin = workbench_server["base_url"]
    async with client_for(workbench_server) as launcher, httpx.AsyncClient(base_url=origin, trust_env=False) as browser:
        issued = await launcher.post("/ui/tickets", json={"cwd": cwd})
        assert issued.status_code == 201
        ticket = issued.json()["ticket"]
        body = {"ticket": ticket}
        assert (await browser.post("/ui/auth", json=body)).status_code == 403
        assert (await browser.post("/ui/auth", json=body, headers={"X-Aster-UI": "1", "Origin": "null"})).status_code == 403
        headers = {"X-Aster-UI": "1", "Origin": origin}
        exchange = await browser.post("/ui/auth", json=body, headers=headers)
        assert exchange.status_code == 200 and exchange.json()["authenticated"]
        assert "httponly" in exchange.headers["set-cookie"].lower()
        assert "samesite=strict" in exchange.headers["set-cookie"].lower()
        assert (await browser.post("/ui/auth", json=body, headers=headers)).status_code == 401
        assert (await browser.get("/sessions")).status_code == 403
        bootstrap = await browser.get("/ui/bootstrap", headers={"X-Aster-UI": "1"})
        assert bootstrap.status_code == 200
        assert Path(bootstrap.json()["cwd"]).samefile(cwd)
        assert bootstrap.json()["model"] == "deepseek-flash"
        for origin_value in ("http://attacker.invalid", origin + "/", "null"):
            denied = await browser.post("/sessions", json={}, headers={"X-Aster-UI": "1", "Origin": origin_value})
            assert denied.status_code == 403
        assert (await browser.post("/sessions", json={}, headers={"X-Aster-UI": "1"})).status_code == 403
        created = await browser.post("/sessions", json={"cwd": cwd}, headers=headers)
        assert created.status_code == 201
        assert (await browser.post("/ui/tickets", json={}, headers=headers)).status_code == 403
        assert (await browser.post("/ui/logout", headers=headers)).status_code == 200
        assert (await browser.get("/sessions", headers={"X-Aster-UI": "1"})).status_code == 401
