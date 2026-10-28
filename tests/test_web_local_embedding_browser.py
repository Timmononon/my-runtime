"""Local embedding settings/queries through Chromium and a real temporary API.

Only the model worker is synthetic. The backend's configuration, model-file
identity, owned subprocess, memory cache and dispatcher remain real.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from playwright.async_api import async_playwright, expect
from test_web_workbench import client_for, create_session
from test_web_workbench_browser import assert_basics, open_settings, open_workbench, screenshot


@pytest.fixture
def local_embedding_server(tmp_path):
    root = Path(__file__).resolve().parents[1]
    ready, stop, log = (tmp_path / name for name in ("ready.json", "stop", "fixture.log"))
    worker = tmp_path / "embedding_worker.py"
    worker.write_text('''import json, sys, time
from pathlib import Path
maximum = 1024
for line in sys.stdin:
    request = json.loads(line)
    with Path(__file__).with_suffix(".calls").open("a", encoding="utf-8") as receipt:
        receipt.write(json.dumps({"op": request["op"], "count": len(request.get("texts", []))}) + "\\n")
    if request["op"] == "load":
        maximum = request["pin"]["local_max_tokens"]
        time.sleep(3)
        response = {"state": "ready", "device": "cpu", "dtype": "float32"}
    else:
        texts = request["texts"]
        counts = [len(text) for text in texts]
        response = {"vectors": [[1.0, 0.0, 0.0] if "Mina" in text or "负责人" in text else [0.0, 1.0, 0.0] for text in texts],
                    "token_counts": counts, "truncated_indices": [i for i, n in enumerate(counts) if n > maximum],
                    "model_max_tokens": maximum}
    print(json.dumps({"id": request["id"], **response}), flush=True)
''', encoding="utf-8")
    runner = tmp_path / "fixture_runner.py"
    runner.write_text(f'''import sys
sys.path.insert(0, {str(root / "scripts")!r})
import web_fixture
from aster.memory import local_embedding
local_embedding._dependencies = lambda: {{name: "offline-fixture" for name in local_embedding._PACKAGES}}
class LocalFixtureCoordinator(web_fixture.RunCoordinator):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs, local_embedding_factory=lambda: local_embedding.LocalEmbeddingManager(worker_script={str(worker)!r}))
web_fixture.RunCoordinator = LocalFixtureCoordinator
web_fixture.main()
''', encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if key not in {"DEEPSEEK_API_KEY", "ASTER_API_TOKEN"}}
    env["PYTHONIOENCODING"] = "utf-8"
    with log.open("w", encoding="utf-8") as output:
        process = subprocess.Popen([sys.executable, str(runner), "--ready-file", str(ready),
                                    "--stop-file", str(stop), "--lifetime", "180"],
                                   cwd=root, env=env, stdout=output, stderr=subprocess.STDOUT,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            deadline = time.monotonic() + 20
            while not ready.exists():
                assert process.poll() is None, log.read_text(encoding="utf-8")
                assert time.monotonic() < deadline, "Temporary embedding fixture did not start"
                time.sleep(0.05)
            server = json.loads(ready.read_text(encoding="utf-8"))
            model = Path(server["cwd"]) / "models" / "offline-bge-fixture"
            (model / "1_Pooling").mkdir(parents=True)
            (model / "modules.json").write_text(json.dumps([
                {"idx": 0, "name": "0", "path": "", "type": "sentence_transformers.models.Transformer"},
                {"idx": 1, "name": "1", "path": "1_Pooling", "type": "sentence_transformers.models.Pooling"},
            ]), encoding="utf-8")
            (model / "config.json").write_text('{"model_type":"bert","hidden_size":3}', encoding="utf-8")
            (model / "tokenizer.json").write_text("{}", encoding="utf-8")
            (model / "1_Pooling/config.json").write_text("{}", encoding="utf-8")
            (model / "model.safetensors").write_bytes(b"synthetic-fixture" * 2)
            server.update(model_path=str(model), worker_receipts=str(worker.with_suffix(".calls")))
            yield server
        finally:
            stop.touch()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=5)
            assert process.returncode == 0, log.read_text(encoding="utf-8")


async def extensions(page, width):
    if width < 700 and not await page.locator(".sidebar.open").count():
        await page.get_by_role("button", name="打开导航", exact=True).click()
    dialog = await open_settings(page)
    await dialog.get_by_role("tab", name="搜索与记忆", exact=True).click()
    await expect(dialog.get_by_role("heading", name="语义记忆", exact=True)).to_be_visible()
    return dialog


async def save_local(dialog, server):
    await dialog.get_by_label("Embedding 运行方式", exact=True).select_option("local")
    await dialog.get_by_label("启用语义检索", exact=True).check()
    await dialog.get_by_label("本地模型目录", exact=True).fill(server["model_path"])
    await dialog.get_by_label("推理设备", exact=True).select_option("cpu")
    await dialog.get_by_label("每条文本 token 上限", exact=True).fill("64")
    await dialog.get_by_role("button", name="保存搜索、记忆与浏览器", exact=True).click()
    await expect(dialog.locator(".runtime-settings > .soft-notice")).to_be_visible()


@pytest.mark.browser
@pytest.mark.parametrize("width", [1440, 390])
async def test_local_settings_persist_without_model_load_and_show_real_worker_state(local_embedding_server, width):
    server = local_embedding_server
    async with client_for(server) as api, async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            context, page, errors = await open_workbench(browser, server, width=width, height=950)
            dialog = await extensions(page, width)
            await save_local(dialog, server)
            await expect(dialog.get_by_label("Embedding 提供商", exact=True)).to_have_count(0)
            await expect(dialog.get_by_label("Embedding 模型 ID", exact=True)).to_have_count(0)
            await expect(dialog.locator(".embedding-saved-status")).to_contain_text("配置就绪")
            await expect(dialog.locator(".embedding-saved-status")).to_contain_text("尚未加载")
            assert not Path(server["worker_receipts"]).exists()
            current = (await api.get("/management")).json()
            config = current["extensions"]["embedding"]
            assert config["backend"] == "local" and config["model_path"] == server["model_path"]
            assert config["device"] == "cpu" and config["local_max_tokens"] == 64
            assert config["batch_size"] == 4 and config["timeout_seconds"] == 180
            assert config["provider_id"] is None and config["model"] is None
            await dialog.get_by_label("本地模型目录", exact=True).fill(server["model_path"] + "-unsaved")
            await dialog.get_by_role("button", name="刷新模型状态", exact=True).click()
            await expect(dialog.get_by_label("本地模型目录", exact=True)).to_have_value(server["model_path"] + "-unsaved")
            assert not Path(server["worker_receipts"]).exists()
            await dialog.get_by_role("button", name="关闭", exact=True).click()
            dialog = await extensions(page, width)
            await expect(dialog.get_by_label("本地模型目录", exact=True)).to_have_value(server["model_path"])
            session = await create_session(api, server, "本地向量状态验收")
            recorded = await api.post(f"/customization/sessions/{session['id']}/memory_record", json={
                "approved": True, "arguments": {"entity": "项目 A", "relation": "负责人", "value": "Mina",
                "sources": [{"kind": "user_supplied", "label": "合成测试", "quote": "项目 A 的负责人是 Mina。"}]}})
            assert recorded.status_code == 200 and not recorded.json()["is_error"]
            pending = asyncio.create_task(api.post(f"/customization/sessions/{session['id']}/memory_semantic_recall",
                json={"arguments": {"query": "负责人", "scopes": ["agent"]}}))
            async with asyncio.timeout(8):
                while not Path(server["worker_receipts"]).exists():
                    await asyncio.sleep(0.02)
            await dialog.get_by_role("button", name="刷新模型状态", exact=True).click()
            await expect(dialog.locator(".embedding-saved-status")).to_contain_text("正在加载模型")
            response = await pending
            assert response.status_code == 200 and not response.json()["is_error"], response.text
            await dialog.get_by_role("button", name="刷新模型状态", exact=True).click()
            await expect(dialog.locator(".embedding-saved-status")).to_contain_text("模型已加载 · cpu")
            await assert_basics(page)
            await screenshot(page, f"local-embedding-settings-{width}.png")
            assert not errors, errors
            await context.close()
        finally:
            await browser.close()


@pytest.mark.browser
@pytest.mark.parametrize("width", [1440, 390])
async def test_local_memory_query_uses_read_permission_and_remote_switch_still_requires_approval(local_embedding_server, width):
    server = local_embedding_server
    async with client_for(server) as api, async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            session = await create_session(api, server, "本地语义查询验收")
            response = await api.post(f"/customization/sessions/{session['id']}/memory_record", json={
                "approved": True, "arguments": {"entity": "项目 A", "relation": "负责人", "value": "Mina",
                "sources": [{"kind": "user_supplied", "label": "合成测试", "quote": "项目 A 的负责人是 Mina。"}]}})
            assert response.status_code == 200 and not response.json()["is_error"]
            context, page, errors = await open_workbench(browser, server, width=width, height=950)
            dialog = await extensions(page, width)
            await save_local(dialog, server)
            await dialog.get_by_role("button", name="关闭", exact=True).click()
            if width < 700 and not await page.locator(".sidebar.open").count():
                await page.get_by_role("button", name="打开导航", exact=True).click()
            await page.get_by_role("button", name="本地语义查询验收", exact=True).click()
            if width < 700 and not await page.locator(".sidebar.open").count():
                await page.get_by_role("button", name="打开导航", exact=True).click()
            await page.get_by_role("navigation", name="主导航").get_by_role("button", name="记忆", exact=True).click()
            query = page.locator(".memory-query")
            await query.locator("summary").first.click()
            await query.get_by_label("记忆查询模式", exact=True).select_option("semantic")
            await query.get_by_label("语义查询文本", exact=True).fill("负责人")
            async with page.expect_request(lambda request: request.method == "POST" and request.url.endswith("/memory_semantic_recall")) as sent:
                await query.get_by_role("button", name="查询记忆", exact=True).click()
            assert (await sent.value).post_data_json["approved"] is False
            await expect(page.get_by_role("dialog", name="授权语义记忆查询", exact=True)).to_have_count(0)
            await expect(query.locator(".memory-query-results")).to_contain_text("本地语义检索")
            await expect(query.locator(".memory-query-results")).to_contain_text("Mina")
            await expect(query.locator(".memory-query-results")).to_contain_text("已截断后检索")
            await screenshot(page, f"local-embedding-memory-{width}.png")
            calls_before = Path(server["worker_receipts"]).read_text(encoding="utf-8")

            async def switch_to_remote(route):
                assert route.request.post_data_json["approved"] is False
                saved = (await api.get("/management")).json()
                updated = await api.patch("/management", json={"expected_revision": saved["revision"],
                    "embedding": {**saved["extensions"]["embedding"], "backend": "remote", "enabled": False}})
                assert updated.status_code == 200, updated.text
                await route.continue_()
            await page.route("**/customization/sessions/*/memory_semantic_recall", switch_to_remote)
            async with page.expect_response(lambda response: response.url.endswith("/memory_semantic_recall")) as rejected:
                await query.get_by_role("button", name="查询记忆", exact=True).click()
            assert (await rejected.value).status == 403
            await page.unroute("**/customization/sessions/*/memory_semantic_recall", switch_to_remote)
            await expect(query.locator(".memory-query-results")).to_have_count(0)
            assert Path(server["worker_receipts"]).read_text(encoding="utf-8") == calls_before
            await page.get_by_role("button", name="关闭错误提示", exact=True).click()
            await query.get_by_role("button", name="查询记忆", exact=True).click()
            authorize = page.get_by_role("dialog", name="授权语义记忆查询", exact=True)
            await expect(authorize).to_be_visible()
            await expect(authorize).to_contain_text("可能产生供应商费用")
            await authorize.get_by_role("button", name="取消", exact=True).click()
            await assert_basics(page)
            assert not errors, errors
            await context.close()
        finally:
            await browser.close()
