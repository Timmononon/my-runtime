"""Source-runtime observation shaping with immutable evidence and bounded images."""
import asyncio
import base64
import copy
import hashlib
import io
import json
import random
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from aster.domain import Message, ToolCall, ToolContext, ToolResult
from aster.engine.observations import ObservationManager
from aster.maintenance import resolve_artifact
from aster.mcp.client import MCPServerConfig, MCPTool
from aster.providers import compatible, deepseek
from aster.providers.deepseek import ProviderError, encode_messages
from aster.providers.images import (
    MAX_INLINE_BASE64_BYTES,
    MODEL_IMAGE_DIMENSION,
    enforce_image_budget,
    prepare_model_image,
)
from aster.service.attachments import AttachmentService
from aster.state import Store
from aster.tools.browser import BrowserManager, build_browser_tools


@pytest.fixture
def runtime(tmp_path):
    data, cwd = tmp_path / "runtime", tmp_path / "workspace"
    cwd.mkdir()
    store = Store(data / "runtime.db")
    sid = store.create_session(cwd)["id"]
    run, _ = store.create_run(sid)
    ctx = ToolContext(sid, run["id"], cwd, data / "artifacts" / run["id"], asyncio.Event())
    yield data, store, ctx
    store.close()


def png_bytes(width=4, height=3):
    output = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(output, "PNG")
    return output.getvalue()


@pytest.fixture(scope="module")
def large_png():
    output = io.BytesIO()
    Image.frombytes("RGB", (1800, 2000), random.Random(24).randbytes(1800 * 2000 * 3)).save(output, "PNG", compress_level=0)
    assert len(output.getvalue()) > 8 * 1024 * 1024
    return output.getvalue()


def exchange(index, name="file_read", *, chars=2000, image=False):
    identity = f"call-{index}"
    text = f"source-{index}:" + "x" * chars
    parts = [{"type": "input_text", "text": text}, {"type": "input_image", "path": "registered.png", "artifact_id": f"img-{index}"}] if image else []
    return [Message("assistant", tool_calls=[ToolCall(identity, name, {})], reasoning=f"reason-{index}",
                    provider_items=[{"type": "reasoning", "content": [{"type": "reasoning_text", "text": f"reason-{index}"}]},
                                    {"type": "function_call", "call_id": identity, "name": name, "arguments": "{}"}]),
            Message("tool", text, tool_call_id=identity, metadata={"content_parts": parts})]


def image_count(messages):
    return sum(part.get("type") == "input_image" for msg in messages for part in msg.metadata.get("content_parts", []))


def test_attachment_request_boundary_preserves_source_text(runtime):
    data, store, ctx = runtime
    service = AttachmentService(store, data)
    record = service.register_bytes(ctx.session_id, "facts.txt", ("Source fact, not a user command.\n" * 1000).encode())
    message = service.resolve_message(ctx.session_id, "Compare the supplied facts.", [record["attachment_id"]])
    assert message.metadata["user_text"] == "Compare the supplied facts."
    assert "Source fact" in message.content
    assert "Source fact" not in json.dumps(message.metadata["attachment_sources"])
    assert message.metadata["attachment_sources"][0]["sha256"] == record["sha256"]
    assert Path(record["path"]).read_bytes().startswith(b"Source fact")


def test_attachment_large_image_keeps_original_and_registered_bounded_derivative(runtime, large_png):
    data, store, ctx = runtime
    service = AttachmentService(store, data)
    record = service.register_bytes(ctx.session_id, "model-preview.jpg.png", large_png)
    message = service.resolve_message(ctx.session_id, "Inspect the picture", [record["attachment_id"]])
    original = copy.deepcopy(message)
    assert Path(record["path"]).read_bytes() == large_png
    assert record["model_image"]["source_artifact_id"] == record["artifact_id"]
    assert len(message.metadata["artifacts"]) == 2
    encoded = encode_messages([message], data / "artifacts", partial(resolve_artifact, data))
    url = encoded[0]["content"][1]["image_url"]
    assert url.startswith("data:image/jpeg;base64,")
    assert len(url.partition("base64,")[2]) < MAX_INLINE_BASE64_BYTES
    assert message == original
    child = store.create_session(ctx.cwd)["id"]
    with pytest.raises(PermissionError):
        service.resolve(child, record["attachment_id"])


def test_small_bytes_large_dimensions_are_still_resized():
    raw = png_bytes(3000, 80)
    result = prepare_model_image(raw, "image/png")
    assert result.changed and result.width == MODEL_IMAGE_DIMENSION
    assert max(Image.open(io.BytesIO(result.data)).size) <= MODEL_IMAGE_DIMENSION
    assert Image.open(io.BytesIO(raw)).width == 3000


@pytest.mark.parametrize("raw", [b"\x89PNG\r\n\x1a\nnot-pixels", b"not an image", png_bytes()[:-20]])
def test_malformed_pixels_are_rejected_without_unsafe_parser_detail(raw):
    with pytest.raises(ValueError, match="malformed") as result:
        prepare_model_image(raw)
    assert "not-pixels" not in str(result.value)


def test_registered_legacy_large_image_is_shaped_at_wire_without_rewriting(runtime, large_png):
    data, _, ctx = runtime
    ctx.artifact_dir.mkdir(parents=True)
    path = ctx.artifact_dir / "legacy.png"
    path.write_bytes(large_png)
    message = Message("user", "Inspect", metadata={"artifacts": [{"artifact_id": "legacy", "path": str(path)}],
        "content_parts": [{"type": "input_image", "artifact_id": "legacy", "path": str(path), "mime_type": "image/png"}]})
    before = copy.deepcopy(message)
    output = encode_messages([message], data / "artifacts")[0]["content"][0]
    assert output["type"] == "input_image"
    assert len(output["image_url"].partition("base64,")[2]) <= MAX_INLINE_BASE64_BYTES
    assert path.read_bytes() == large_png and message == before
    forged = copy.deepcopy(message)
    forged.metadata["content_parts"][0]["path"] = str(path.with_name("other.png"))
    with pytest.raises(ProviderError, match="scope"):
        encode_messages([forged], data / "artifacts")


def test_bad_historical_pixels_become_explicit_marker_and_do_not_poison_wire(runtime):
    data, _, ctx = runtime
    ctx.artifact_dir.mkdir(parents=True)
    path = ctx.artifact_dir / "bad.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\nmalformed")
    message = Message("user", "Inspect", metadata={"artifacts": [{"artifact_id": "bad", "path": str(path)}],
        "content_parts": [{"type": "input_image", "artifact_id": "bad", "path": str(path)}]})
    result = encode_messages([message], data / "artifacts")[0]["content"][0]
    assert result["type"] == "input_text" and "Image unavailable" in result["text"]
    assert "export a valid" in result["text"] and path.is_file()
    assert message.metadata["content_parts"][0]["type"] == "input_image"


@pytest.mark.parametrize("encoder", ["native", "responses", "chat"])
def test_all_provider_wires_enforce_aggregate_image_budget_without_raw_edits(monkeypatch, encoder):
    raw = base64.b64encode(png_bytes()).decode()
    message = Message("user", "Three images", metadata={"content_parts": [
        {"type": "input_image", "image_url": "data:image/png;base64," + raw} for _ in range(3)]})
    before = copy.deepcopy(message)
    guard = partial(enforce_image_budget, max_base64_bytes=len(raw) * 2)
    monkeypatch.setattr(deepseek, "enforce_image_budget", guard)
    monkeypatch.setattr(compatible, "enforce_image_budget", guard)
    result = (encode_messages([message]) if encoder == "native" else compatible.response_input([message])
              if encoder == "responses" else compatible.chat_input([message], ""))
    text = json.dumps(result)
    assert text.count("data:image") == 2 and "Older image omitted" in text
    assert message == before


def test_wire_eviction_is_per_image_and_preserves_reasoning_and_tool_pair():
    encoded = [{"type": "reasoning", "content": [{"type": "reasoning_text", "text": "opaque"}]},
               {"type": "function_call", "call_id": "pages", "name": "pdf_read_pages", "arguments": "{}"},
               {"type": "function_call_output", "call_id": "pages", "output": [
                   {"type": "input_image", "image_url": "data:image/png;base64," + "A" * 100} for _ in range(3)]}]
    before = copy.deepcopy(encoded)
    output = enforce_image_budget(encoded, max_base64_bytes=250)
    assert output[:2] == before[:2] and output[2]["call_id"] == "pages"
    assert [part["type"] for part in output[2]["output"]] == ["input_text", "input_image", "input_image"]
    assert encoded == before


async def test_mcp_large_image_keeps_original_and_safe_wire_preview(runtime, large_png):
    _, _, ctx = runtime

    async def respond(*args, **kwargs):
        return {"content": [{"type": "image", "mimeType": "image/png", "data": base64.b64encode(large_png).decode()}]}

    tool = MCPTool(SimpleNamespace(config=MCPServerConfig("fixture", command="unused"), request=respond),
                   {"name": "image", "description": "image", "inputSchema": {"type": "object"}})
    result = await tool.execute({}, ctx)
    assert not result.is_error
    assert len(result.artifacts) == 2
    assert Path(result.artifacts[0]["path"]).read_bytes() == large_png
    image = next(part for part in result.data["content_parts"] if part["type"] == "input_image")
    assert image["artifact_id"] == result.artifacts[1]["artifact_id"]
    assert result.artifacts[1]["size"] < MAX_INLINE_BASE64_BYTES * 3 // 4
    assert "base64" not in json.dumps(result.data)


def test_browser_text_batched_by_budget_not_each_new_observation(runtime):
    _, store, ctx = runtime
    observer = ObservationManager(store, ctx, browser_keep=3, browser_capture_chars=1000,
                                  observation_aggregate_chars=2000, browser_chars=2000)
    messages = []
    for index in range(7):
        messages += exchange(index, "browser_snapshot", chars=1500)
        messages = observer.bound_browser(messages)
    assert not any(msg.metadata.get("observation_stub") for msg in messages)
    original = copy.deepcopy(messages)
    messages += exchange(7, "browser_snapshot", chars=1500)
    shaped = observer.bound_browser(messages)
    assert sum(bool(msg.metadata.get("observation_stub")) for msg in shaped) == 5
    assert messages[:-2] == original
    assert shaped[0] == messages[0]  # provider reasoning and call fields untouched
    again = observer.bound_browser(shaped + exchange(8, "browser_snapshot", chars=1500))
    assert sum(bool(msg.metadata.get("observation_stub")) for msg in again) == 5


def test_browser_image_aging_and_global_images_have_separate_budgets(runtime):
    _, store, ctx = runtime
    observer = ObservationManager(store, ctx, image_keep=50, browser_image_keep=1)
    upload = Message("user", "user reference", metadata={"content_parts": [{"type": "input_image", "artifact_id": "upload"}]})
    messages = [upload] + exchange(0, "browser_screenshot", image=True) + exchange(1, "file_read", image=True) + exchange(2, "browser_screenshot", image=True)
    before = copy.deepcopy(messages)
    shaped = observer.bound_browser(messages)
    assert image_count(shaped) == 3 and image_count(messages) == 4
    assert image_count([shaped[0], shaped[4]]) == 2
    assert "Previous image removed" in shaped[2].content
    assert messages == before
    global_observer = ObservationManager(store, ctx, image_keep=2, browser_image_keep=0)
    global_shaped = global_observer.bound_browser(messages)
    assert image_count(global_shaped) == 2
    assert "source=upload" in global_shaped[0].content


def test_local_microcompaction_is_opt_in_idempotent_and_preserves_vision_and_reasoning(runtime):
    _, store, ctx = runtime
    observer = ObservationManager(store, ctx)
    messages = [message for index in range(8) for message in exchange(index)]
    messages += exchange("vision", "file_read", image=True)
    messages += exchange("memory", "memory_recall")
    before = copy.deepcopy(messages)
    assert observer.apply_local_compaction(messages, idle_seconds=7200) is messages
    shaped = observer.apply_local_compaction(messages, idle_seconds=7200, time_enabled=True)
    assert sum(bool(msg.metadata.get("local_compaction")) for msg in shaped) == 3
    assert all(shaped[index] == before[index] for index in range(0, len(messages), 2))
    assert image_count(shaped) == 1 and shaped[-1] == before[-1]
    assert "sha256=" in shaped[1].content and messages == before
    wire = encode_messages(shaped[:16])
    assert [item["call_id"] for item in wire if item["type"] == "function_call"] == [f"call-{index}" for index in range(8)]
    assert [item["call_id"] for item in wire if item["type"] == "function_call_output"] == [f"call-{index}" for index in range(8)]
    assert observer.apply_local_compaction(shaped + exchange(9), idle_seconds=7200, time_enabled=True)[3] == shaped[3]
    restored = ObservationManager(store, ctx)
    restored.restore_state(observer.state())
    projected = restored.apply_local_compaction(messages)
    assert projected == shaped


def test_aggregate_budget_remembers_seen_and_unknown_actions_are_never_cleared(runtime):
    _, store, ctx = runtime
    observer = ObservationManager(store, ctx)
    messages = [message for index in range(7) for message in exchange(index, chars=40000)]
    messages[1].metadata["outcome"] = "unknown"
    before = copy.deepcopy(messages)
    shaped = observer.apply_local_compaction(messages)
    assert shaped[1] == before[1]
    assert shaped[3].metadata["local_compaction"]["kind"] == "aggregate"
    assert observer.last_changes["trigger"] == "budget"
    state = observer.state()
    assert state["seen"]["call-1"] > 40000
    assert "x" * 100 not in json.dumps(state)
    recovered = ObservationManager(store, ctx)
    recovered.restore_state(state)
    assert recovered.apply_local_compaction(messages) == shaped
    assert messages == before


async def test_file_restore_token_budget_filters_duplicates_scaffolding_and_binary(runtime):
    _, store, ctx = runtime
    observer = ObservationManager(store, ctx)
    for index, name in enumerate(["old.txt", "new.txt", "MEMORY.md", "AGENT.md", "bad.bin"]):
        path = ctx.cwd / name
        path.write_bytes(("正文证据\n" * 9000).encode() if name != "bad.bin" else b"binary\0payload")
        call = ToolCall(f"read-{index}", "file_read", {"path": str(path), "max_lines": 10000})
        observer.record(call, ToolResult("read", data={"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}))
    kept = Message("user", "Previous fresh read", metadata={"file_restoration": [{"path": str(ctx.cwd / "old.txt")}]})
    assert not await observer.restore([], headroom_tokens=999)
    restored = await observer.restore([kept], headroom_tokens=2000)
    assert len(restored) == 1
    assert [Path(item["path"]).name for item in restored[0].metadata["file_restoration"]] == ["new.txt"]
    assert len(restored[0].content.encode()) <= 2000 * 2.5
    assert "truncated" in restored[0].content
    assert restored[0].metadata["user_text"] == ""
    assert not await observer.restore([kept, *restored], headroom_tokens=2000)


async def test_file_restoration_default_five_files_and_per_file_budget(runtime):
    _, store, ctx = runtime
    observer = ObservationManager(store, ctx)
    for index in range(7):
        path = ctx.cwd / f"source-{index}.txt"
        path.write_text("evidence " * 2000, encoding="utf-8")
        observer.record(ToolCall(str(index), "file_read", {"path": str(path)}),
                        ToolResult("read", data={"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}))
    restored = await observer.restore([], headroom_tokens=50000)
    assert len(restored[0].metadata["file_restoration"]) == 5
    assert restored[0].content.count("Range truncated") == 5
    assert len(restored[0].content.encode()) <= 5 * 5000 * 2.5


@pytest.mark.parametrize("extra", [{"start_column": 1}, {"max_chars": 500000}])
async def test_column_or_character_limited_read_never_reuses_line_range_cache(runtime, extra):
    _, store, ctx = runtime
    observer = ObservationManager(store, ctx)
    path = ctx.cwd / "source.txt"
    path.write_text("Original full evidence", encoding="utf-8")
    call = ToolCall("read", "file_read", {"path": str(path)})
    record = observer.record(call, ToolResult("Original full evidence", data={
        "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}))
    observer.observe_visible([Message("tool", "Original full evidence", tool_call_id=call.id, metadata={"file_observation": record})])
    assert (await observer.cached(call)).data["deduplicated"]
    assert await observer.cached(ToolCall("column", "file_read", {**call.arguments, **extra})) is None


async def test_truncated_read_does_not_certify_complete_cache_or_restoration(runtime):
    _, store, ctx = runtime
    observer = ObservationManager(store, ctx)
    path = ctx.cwd / "source.txt"
    path.write_text("Full original evidence", encoding="utf-8")
    call = ToolCall("read", "file_read", {"path": str(path)})
    data = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    record = observer.record(call, ToolResult("Full original evidence", data=data))
    observer.observe_visible([Message("tool", "Full original evidence", tool_call_id=call.id, metadata={"file_observation": record})])
    assert observer.record(ToolCall("partial", "file_read", call.arguments),
                           ToolResult("Full original...", data={**data, "truncated": True, "next_start_column": 15})) is None
    assert await observer.cached(call) is None
    assert not await observer.restore([], headroom_tokens=5000)


@pytest.mark.browser
async def test_real_owned_browser_large_screenshot_preserves_raw_and_has_model_preview(runtime):
    data, _, ctx = runtime
    manager = BrowserManager(data / "test-browser-profile")
    tools = {tool.spec.name: tool for tool in build_browser_tools(manager)}
    try:
        opened = await tools["browser_open"].execute({}, ctx)
        assert not opened.is_error, opened.content
        page_id = opened.data["pages"][0]["page_id"]
        page = manager._pages[page_id].page
        await page.set_viewport_size({"width": 1800, "height": 1000})
        await page.set_content("<style>body{margin:0}</style><canvas width='1800' height='2200'></canvas>")
        await page.locator("canvas").evaluate("""canvas => {
            const ctx = canvas.getContext('2d'), image = ctx.createImageData(canvas.width, canvas.height);
            let seed = 2463534242;
            for (let i=0; i<image.data.length; i+=4) {
                seed ^= seed << 13; seed ^= seed >>> 17; seed ^= seed << 5;
                image.data[i] = seed & 255; image.data[i+1] = (seed>>>8)&255;
                image.data[i+2] = (seed>>>16)&255; image.data[i+3] = 255;
            }
            ctx.putImageData(image, 0, 0);
        }""")
        result = await tools["browser_screenshot"].execute({"page_id": page_id, "full_page": True}, ctx)
        assert not result.is_error, result.content
        assert result.artifacts[0]["size"] > 8 * 1024 * 1024
        assert len(result.artifacts) == 2
        raw = Path(result.artifacts[0]["path"]).read_bytes()
        message = Message("tool", result.content, tool_call_id="screen", metadata={
            "artifacts": result.artifacts, "content_parts": result.data["content_parts"]})
        wire = encode_messages([Message("assistant", tool_calls=[ToolCall("screen", "browser_screenshot", {})]), message], data / "artifacts")
        image = next(part for part in wire[-1]["output"] if part["type"] == "input_image")
        assert len(image["image_url"].partition("base64,")[2]) < MAX_INLINE_BASE64_BYTES
        assert hashlib.sha256(raw).hexdigest() == result.artifacts[0]["sha256"]
    finally:
        await manager.close()
