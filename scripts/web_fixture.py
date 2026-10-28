"""Offline workbench QA server; never imported or packaged by production Aster.

Every run uses the real coordinator, policy, journal, tools and HTTP API. Only
the model is scripted. State and credentials are temporary test data.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import socket
import tempfile
import time
import uuid
from functools import partial
from pathlib import Path

import httpx
import uvicorn
import yaml

from aster.api import create_app
from aster.domain import ModelTurn, ToolCall, Usage
from aster.engine import FakeModel
from aster.maintenance import resolve_artifact
from aster.providers.deepseek import ProviderError, encode_messages
from aster.service import RunCoordinator


class MemoryCredentials:
    """Synthetic keys stay in this test process; never reads an OS vault."""

    def __init__(self):
        self.values = {"deepseek": "offline-fixture-synthetic-key"}

    def get(self, name="deepseek", **kwargs):
        return self.values.get(name)

    def set(self, name, value):
        self.values[name] = value

    def delete(self, name):
        self.values.pop(name, None)

    def resolve(self, reference):
        raise ValueError("Offline workbench fixture has no provider credentials")


class WorkbenchFixtureModel:
    """Explicit Chinese test commands produce deterministic, real tool workflows."""

    model = "offline-workbench-fixture"

    def __init__(self, data_dir: Path, settings):
        self.data_dir = data_dir
        self.settings = settings
        self.model = settings["model"]
        self.delegate = None
        self.cancel = None
        self.reconnect = False

    def plan(self, messages):
        user = next(message for message in reversed(messages) if message.role == "user")
        prompt = user.content
        if "测试取消" in prompt:
            async def wait_for_cancel(_messages, _tools):
                await self.cancel.wait()
                raise asyncio.CancelledError
            return [wait_for_cancel]
        if "测试长对话" in prompt:
            return [ModelTurn("离线验收：长对话上下文样本。" + "保留原始记录，压缩只影响模型活动上下文。" * 160)]
        if "测试轨迹" in prompt:
            return [ModelTurn(tool_calls=[ToolCall("timeline-read", "file_read", {"path": "fixture-note.txt"}),
                                         ToolCall("timeline-list", "directory_list", {"path": "."})],
                              usage=Usage(input_tokens=100, output_tokens=20, cache_hit_tokens=40, cache_miss_tokens=60)),
                    ModelTurn("离线验收：真实只读工具批次已完成。", usage=Usage(input_tokens=200, output_tokens=30,
                                                                          cache_hit_tokens=100, cache_miss_tokens=100))]
        if "测试技能权限" in prompt:
            return [ModelTurn(tool_calls=[ToolCall("skill-discover", "tool_search", {"query": "select:file_write"})]),
                    ModelTurn(tool_calls=[ToolCall("skill-denied", "file_write", {"path": "skill-must-not-write.txt", "content": "blocked"})]),
                    ModelTurn("离线验收：已执行所选技能的权限检查。")]
        if "测试提问" in prompt:
            def answered(items, _tools):
                value = next(item for item in reversed(items) if item.role == "tool")
                # The live model projection carries structured fields in a text
                # prefix, not a second private metadata.data copy.
                answer = json.loads(value.content.rsplit("\n\n", 1)[-1])
                return ModelTurn("离线验收：收到选择 " + json.dumps(answer, ensure_ascii=False))
            return [ModelTurn(tool_calls=[ToolCall("fixture-question", "ask_user_question", {
                "question": "这份报告使用哪种语言？", "options": ["中文", "English"]})]), answered]
        if "测试审批" in prompt:
            name = "workbench-report-" + uuid.uuid4().hex[:8] + ".html"
            content = ('<!doctype html><meta charset="utf-8"><title>Aster 验收报告</title>'
                       '<style>body{font-family:system-ui;margin:48px;color:#172333}h1{color:#ff6900}</style>'
                       '<h1>ASTER WORKBENCH VERIFIED</h1><p>由已批准的真实 file_write 工具创建。</p>')

            def present(items, _tools):
                result = next(item for item in reversed(items) if item.role == "tool")
                if result.metadata.get("is_error"):
                    return ModelTurn("离线验收：写入没有获准，未创建交付物。")
                return ModelTurn(tool_calls=[ToolCall("fixture-present", "present_deliverable", {
                    "path": name, "title": "Aster 验收报告", "preview": True})])

            return [
                ModelTurn(tool_calls=[ToolCall("fixture-discover", "tool_search", {
                    "query": "select:file_write,present_deliverable"})]),
                ModelTurn("离线验收：准备创建本地报告，请确认文件写入。", tool_calls=[
                    ToolCall("fixture-write", "file_write", {"path": name, "content": content})]),
                present, ModelTurn("离线验收：报告已生成，可以下载或打开静态预览。"),
            ]
        if user.metadata.get("artifacts"):
            # Exercise the real image wire encoder without making any model call.
            encoded = encode_messages([user], self.data_dir / "artifacts", partial(resolve_artifact, self.data_dir))
            images = sum(part.get("type") == "input_image" for item in encoded
                         for part in item.get("content", []) if isinstance(part, dict))
            names = [Path(item["path"]).name for item in user.metadata["artifacts"]]
            return [ModelTurn("离线验收：已读取附件 " + "、".join(names) + f"；有效图片 {images} 张。")]
        self.reconnect = "测试重连" in prompt
        text = "离线验收：" + ("事件重连应保持这段文字完整且只出现一次。" * 8 if self.reconnect
                           else "你好，我是 Aster。聊天、事件和历史均来自当前临时 Runtime。")
        return [ModelTurn(text)]

    async def generate(self, messages, tools, *, system, emit=None, cancel=None):
        self.cancel = cancel
        with (self.data_dir / "fixture-model-calls.jsonl").open("a", encoding="utf-8") as receipt:
            receipt.write(json.dumps({"model": self.settings.get("model"), "reasoning_effort": self.settings.get("reasoning_effort"),
                                      "provider_id": self.settings.get("_provider", {}).get("id"),
                                      "tool_count": len(tools),
                                      "helper": system.startswith(("Compress the quoted conversation", "Extract durable knowledge")),
                                      "skill_instruction_seen": "QA_SELECTED_SKILL_INSTRUCTION" in system}, ensure_ascii=False) + "\n")
        if system.startswith("Extract durable knowledge"):
            return ModelTurn("NONE")
        if system.startswith(("Summarize the quoted archived conversation", "Compress the quoted conversation")):
            if any("QA_BLOCK_SUMMARY_UNTIL_CANCEL" in message.content for message in messages):
                if emit:
                    await emit("model.text.delta", {"text": "Offline summary waiting for explicit cancellation."})
                await cancel.wait()
                raise asyncio.CancelledError
            return ModelTurn("Goal: offline context fixture.\nConstraints: preserve archive.\nCompleted evidence: consult original records.\nOpen work: continue.\nReferences: original session history.")
        if self.delegate is None:
            self.delegate = FakeModel(self.plan(messages))
            if "测试取消" in next(item.content for item in reversed(messages) if item.role == "user") and emit:
                await emit("model.text.delta", {"text": "离线验收：任务正在等待，可随时停止。"})

        async def streamed(kind, payload):
            if kind == "model.text.delta":
                text = payload["text"]
                width = 512 if len(text) > 2000 else 3 if self.reconnect else 8
                for start in range(0, len(text), width):
                    if cancel and cancel.is_set():
                        raise asyncio.CancelledError
                    await emit(kind, {"text": text[start:start + width]})
                    await asyncio.sleep(0.003 if len(text) > 2000 else 0.06 if self.reconnect else 0.03)
            else:
                await emit(kind, payload)

        return await self.delegate.generate(messages, tools, system=system,
                                            emit=streamed if emit else None, cancel=cancel)


async def serve_fixture(ready_file: Path, stop_file: Path, *, port=0, lifetime=900):
    with tempfile.TemporaryDirectory(prefix="aster-web-fixture-") as directory:
        root = Path(directory)
        data, workspace = root / "state", root / "workspace"
        data.mkdir()
        workspace.mkdir()
        (workspace / "fixture-note.txt").write_text("Offline workbench fixture only.\n", encoding="utf-8")
        (data / "config.yaml").write_text(yaml.safe_dump({"cwd": str(workspace), "model": "deepseek-flash",
                                                       "runtime": {"deadline_seconds": 180}}), encoding="utf-8")
        skill = data / "skills" / "qa-selected-skill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: qa-selected-skill\ndescription: 离线验收的只读工作流\nallowed-tools: [file_read]\n---\nQA_SELECTED_SKILL_INSTRUCTION\n", encoding="utf-8")
        token = secrets.token_urlsafe(36)
        coordinator = RunCoordinator(data, model_factory=lambda settings: WorkbenchFixtureModel(data, settings),
                                     credentials=MemoryCredentials(), browser_enabled=os.environ.get("ASTER_WEB_AUDIT_BROWSER_ENABLED")=="1")

        catalog_fault_used = False
        def catalog(request):
            nonlocal catalog_fault_used
            assert request.method == "GET" and request.url.path.endswith("/models")
            if os.environ.get("ASTER_WEB_CATALOG_FAULT_ONCE") == "endpoint" and not catalog_fault_used:
                catalog_fault_used = True
                # Explicit offline injection reproduces the typed Runtime error,
                # not a mocked UI response; no real DNS, host or key is accessed.
                raise ProviderError("Synthetic catalog endpoint rejection", category="endpoint")
            return httpx.Response(200, json={"data": [{"id": "qa-catalog-model", "name": "离线目录模型",
                                                       "effort_levels": ["low", "high"], "input_modalities": ["text", "image"]}]})

        coordinator.providers.client_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(catalog), trust_env=False)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", port))
        listener.listen(128)
        origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
        static_root = Path(os.environ["ASTER_WEB_STATIC_ROOT"]) if os.environ.get("ASTER_WEB_STATIC_ROOT") else None
        app = create_app(coordinator, token=token, allowed_origins={origin}, static_root=static_root)
        @app.post("/fixture/automations/{job_id}/tick")
        async def audit_tick(job_id: str):
            # A synthetic clock advances the real scheduler; no business response
            # or execution receipt is fabricated by the browser test fixture.
            job = coordinator.automation.get_job(job_id)
            await coordinator.automation.tick(job["next_fire"])
            return {"triggers": coordinator.automation.receipts(job_id)}
        disconnected = set()

        @app.middleware("http")
        async def reconnect_fault(request, call_next):
            response = await call_next(request)
            pieces = request.url.path.strip("/").split("/")
            if response.status_code == 200 and len(pieces) == 3 and pieces[0] == "runs" and pieces[2] == "events":
                rid = pieces[1]
                payload = coordinator.store.run_request(rid)
                if "测试重连" in payload.get("text", "") and rid not in disconnected:
                    disconnected.add(rid)
                    original = response.body_iterator

                    async def early_eof():
                        # Fault injection truncates a real server stream; it does
                        # not fabricate, replace or reorder any runtime events.
                        try:
                            async for chunk in original:
                                yield chunk
                                if b"event: model.text.delta" in chunk:
                                    break
                        finally:
                            await original.aclose()

                    response.body_iterator = early_eof()
            return response

        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False,
                                              proxy_headers=False, timeout_graceful_shutdown=2))
        task = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError("Fixture server stopped before startup")
                await asyncio.sleep(0.01)
            ready_file.parent.mkdir(parents=True, exist_ok=True)
            ready_file.write_text(json.dumps({"fixture": True, "base_url": origin, "data_dir": str(data),
                                             "cwd": str(workspace), "token": token}), encoding="utf-8")
            print("Offline Aster QA fixture ready; no provider credentials or paid calls.", flush=True)
            deadline = time.monotonic() + lifetime
            while not stop_file.exists() and time.monotonic() < deadline and not task.done():
                await asyncio.sleep(0.1)
        finally:
            server.should_exit = True
            try:
                await asyncio.wait_for(task, 10)
            finally:
                await coordinator.close()
                listener.close()
                ready_file.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ready-file", required=True, type=Path)
    parser.add_argument("--stop-file", required=True, type=Path)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--lifetime", type=float, default=900)
    args = parser.parse_args()
    asyncio.run(serve_fixture(args.ready_file, args.stop_file, port=args.port, lifetime=args.lifetime))


if __name__ == "__main__":
    main()
