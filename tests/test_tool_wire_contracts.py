"""Real tool results must retain continuation fields and obey wire text budgets."""
import asyncio
import base64
import io
import json
import re
from types import SimpleNamespace

import pytest
from PIL import Image

from aster.domain import Message, ToolCall, ToolContext
from aster.engine import Engine
from aster.mcp.client import MCPServerConfig, MCPTool
from aster.providers.deepseek import encode_messages
from aster.tools.browser import BrowserManager, build_browser_tools
from aster.tools.files import build_file_tools


def png_fixture():
    output = io.BytesIO()
    Image.new("RGB", (2, 2), "white").save(output, "PNG")
    return output.getvalue()


def context(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    return ToolContext("session", "run", tmp_path, artifacts, asyncio.Event())


def output(spec, result, ctx, *, limit=16000):
    raw = Engine._tool_message("call", result, ctx.run_id)
    projected = Engine._budget_tool_message(raw, ctx, limit)
    wire = encode_messages([Message("assistant", tool_calls=[ToolCall("call", spec.name, {})]), projected], ctx.artifact_dir)
    return raw, projected, wire[-1]["output"]


@pytest.mark.asyncio
async def test_real_file_hash_visible_on_wire_can_drive_guarded_edit(tmp_path):
    ctx = context(tmp_path)
    file = tmp_path / "settings.ini"
    file.write_text("mode=draft\n", encoding="utf-8")
    tools = {tool.spec.name: tool for tool in build_file_tools()}
    result = await tools["file_read"].execute({"path": "settings.ini"}, ctx)
    _, _, wire = output(tools["file_read"].spec, result, ctx)
    model_visible_hash = re.search(r'"sha256"\s*:\s*"([0-9a-f]{64})"', wire).group(1)
    changed = await tools["file_edit"].execute({"path": "settings.ini", "old_text": "draft", "new_text": "ready", "expected_hash": model_visible_hash}, ctx)
    assert not changed.is_error
    assert file.read_text() == "mode=ready\n"


@pytest.mark.asyncio
async def test_real_browser_context_and_page_ids_are_visible_on_wire(tmp_path):
    ctx = context(tmp_path)
    manager = BrowserManager(tmp_path / "profiles")
    tools = {tool.spec.name: tool for tool in build_browser_tools(manager)}
    try:
        result = await tools["browser_open"].execute({}, ctx)
        assert not result.is_error, result.content
        _, _, wire = output(tools["browser_open"].spec, result, ctx)
        assert result.data["context_id"] in wire
        assert result.data["pages"][0]["page_id"] in wire
        assert '"context_id"' in wire
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_mcp_mixed_content_text_spills_on_wire_but_image_and_raw_survive(tmp_path):
    ctx = context(tmp_path)
    png = png_fixture()
    text = "long-mcp-evidence:" + "x" * 20000

    async def respond(*args, **kwargs):
        return {"content": [{"type": "text", "text": text}, {"type": "image", "mimeType": "image/png", "data": base64.b64encode(png).decode()}], "structuredContent": {"marker": "mcp-reference-71"}}

    connection = SimpleNamespace(config=MCPServerConfig("fixture", command="unused"), request=respond)
    tool = MCPTool(connection, {"name": "mixed", "description": "fixture", "inputSchema": {"type": "object"}})
    result = await tool.execute({}, ctx)
    raw, projected, wire = output(tool.spec, result, ctx, limit=1000)
    assert len(raw.content) > 20000
    assert len(result.data["content_parts"][0]["text"]) > 20000
    visible = "\n".join(part.get("text", "") for part in wire)
    assert len(visible) < 1500
    assert "truncated" in visible.lower() and "output" in visible.lower()
    image = next(part for part in wire if part["type"] == "input_image")
    assert base64.b64decode(image["image_url"].partition(",")[2]) == png
    assert projected.metadata["output_artifact"]["path"] in visible
    assert "data:image" not in json.dumps(raw.metadata)


@pytest.mark.asyncio
async def test_identical_mcp_images_across_runs_have_unique_registered_identity(tmp_path):
    from aster.maintenance import resolve_artifact
    from aster.state import Store

    png = png_fixture()

    async def respond(*args, **kwargs):
        return {"content": [{"type": "image", "mimeType": "image/png", "data": base64.b64encode(png).decode()}]}

    tool = MCPTool(SimpleNamespace(config=MCPServerConfig("fixture", command="unused"), request=respond),
                   {"name": "image", "description": "fixture", "inputSchema": {"type": "object"}})
    store = Store(tmp_path / "runtime.db")
    session = store.create_session(cwd=tmp_path)
    registered = []
    try:
        for index in range(2):
            run, _ = store.create_run(session["id"])
            ctx = ToolContext(session["id"], run["id"], tmp_path, tmp_path / "artifacts" / run["id"], asyncio.Event())
            result = await tool.execute({}, ctx)
            store.append_message(session["id"], run["id"], Engine._tool_message(f"call-{index}", result, run["id"]))
            registered.append(result.artifacts[0])
            store.set_run_status(run["id"], "completed")
        assert registered[0]["artifact_id"] != registered[1]["artifact_id"]
        assert registered[0]["sha256"] == registered[1]["sha256"]
        for item in registered:
            assert resolve_artifact(tmp_path, artifact_id=item["artifact_id"]).read_bytes() == png
    finally:
        store.close()
