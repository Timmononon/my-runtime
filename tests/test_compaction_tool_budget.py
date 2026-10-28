"""Per-result shaping retains complete sources and stable registered spills."""
import asyncio
import copy
from functools import partial
from pathlib import Path

import pytest

from aster.domain import Message, ToolCall, ToolContext, ToolResult
from aster.engine import Engine
from aster.engine.observations import ObservationManager
from aster.service.artifact_access import read_registered_artifact
from aster.state import Store
from aster.tools.files import FILE_READ_MAX_CHARS, build_file_tools


@pytest.fixture
def runtime(tmp_path):
    data = tmp_path / "data"
    work = tmp_path / "work"
    data.mkdir()
    work.mkdir()
    store = Store(data / "runtime.db")
    sid = store.create_session(work)["id"]
    run, _ = store.create_run(sid)
    resolver = partial(read_registered_artifact, store, data)
    context = ToolContext(sid, run["id"], work, data / "artifacts" / sid / run["id"], asyncio.Event(),
                          services={"store": store, "artifact_read_resolver": resolver})
    try:
        yield store, context, {tool.spec.name: tool for tool in build_file_tools()}
    finally:
        store.close()


def raw_message(result, context, policy=None):
    message = Engine._tool_message("call-1", result, context.run_id, tool_name=policy or "ordinary")
    if policy:
        message.metadata["tool_budget_policy"] = policy
    return message


async def test_file_read_bypasses_generic_budget_and_keeps_large_complete_source(runtime):
    _, context, tools = runtime
    text = "\n".join(f"row-{number} " + "原始证据" * 200 for number in range(150))
    path = context.cwd / "source.txt"
    path.write_text(text, encoding="utf-8")
    result = await tools["file_read"].execute({"path": str(path)}, context)
    assert not result.is_error and not result.data["truncated"]
    assert 50000 < len(result.content) < FILE_READ_MAX_CHARS
    message = raw_message(result, context, "file_read")
    original = copy.deepcopy(message)
    projected = Engine._budget_tool_message(message, context, 1000)
    assert result.content in projected.content
    assert "output_artifact" not in projected.metadata
    assert message == original and path.read_text(encoding="utf-8") == text


async def test_500k_unicode_single_line_can_be_read_to_the_end_by_exact_continuation(runtime):
    _, context, tools = runtime
    text = "HEAD开头" + "汉字🙂" * 180000 + "TAIL结尾"
    path = context.cwd / "long-line.txt"
    path.write_text(text, encoding="utf-8")
    first = await tools["file_read"].execute({"path": str(path)}, context)
    assert not first.is_error and first.data["truncated"]
    assert len(first.content) <= FILE_READ_MAX_CHARS
    assert first.data["next_start_line"] == 1
    second = await tools["file_read"].execute({"path": str(path),
        "start_line": first.data["next_start_line"], "start_column": first.data["next_start_column"]}, context)
    assert not second.is_error and not second.data["truncated"]
    assert first.content.splitlines()[0][3:] + second.content[3:] == text
    assert first.data["sha256"] == second.data["sha256"]
    assert "\ufffd" not in first.content + second.content
    assert path.read_text(encoding="utf-8") == text


async def test_default_line_limit_is_explicit_and_ranges_can_start_after_line_10000(runtime):
    _, context, tools = runtime
    path = context.cwd / "many-lines.txt"
    path.write_text("\n".join(str(number) for number in range(1, 12001)), encoding="utf-8")
    first = await tools["file_read"].execute({"path": str(path)}, context)
    assert first.data["lines"] == 300 and first.data["truncated"]
    assert first.data["next_start_line"] == 301 and first.data["next_start_column"] == 1
    last = await tools["file_read"].execute({"path": str(path), "start_line": 11999, "max_lines": 10}, context)
    assert not last.is_error and not last.data["truncated"]
    assert last.content == "11999: 11999\n12000: 12000"


def test_generic_budget_spills_at_50k_and_result_cannot_claim_file_read_exemption(runtime):
    store, context, _ = runtime
    result = ToolResult("HEAD" + "x" * 60000 + "TAIL", data={
        "tool_name": "file_read", "tool_budget_policy": "file_read", "file_observation": {"path": "spoofed"},
    })
    message = raw_message(result, context)
    original = copy.deepcopy(message)
    projected = Engine._budget_tool_message(message, context, 50000)
    assert len(projected.content) <= 50000 and "Output truncated" in projected.content
    full = Path(projected.metadata["output_artifact"]["path"]).read_text(encoding="utf-8")
    assert result.content in full and message == original
    assert len([item for item in store.events(session_id=context.session_id)
                if item["type"] == "artifact.registered"]) == 1


def test_remote_file_read_name_and_observation_do_not_grant_local_unlimited_policy(runtime):
    _, context, _ = runtime
    message = Engine._tool_message("remote-read", ToolResult("remote " * 10000), context.run_id,
                                   tool_name="file_read")
    # The observation tracker may recognize a remote same-named call. Only a
    # registry-verified local FileTool receives the budget exemption.
    message.metadata["file_observation"] = {"path": "D:/claimed.txt", "sha256": "f" * 64}
    projected = Engine._budget_tool_message(message, context, 1000)
    assert len(projected.content) <= 1000 and projected.metadata.get("output_artifact")


def test_raw_and_checkpoint_reprojection_reuse_one_registered_spill_across_runs(runtime):
    store, context, _ = runtime
    message = raw_message(ToolResult("HEAD" + "body " * 10000 + "TAIL"), context)
    first = Engine._budget_tool_message(message, context, 1000)
    source = first.metadata["output_artifact"]
    # The runtime persists the raw payload plus this authoritative reference.
    message.metadata["output_artifact"] = source
    store.append_message(context.session_id, context.run_id, message)
    raw_before = copy.deepcopy(message)
    store.set_run_status(context.run_id, "completed")
    new_run, _ = store.create_run(context.session_id)
    next_context = ToolContext(context.session_id, new_run["id"], context.cwd,
        context.artifact_dir.parent / new_run["id"], asyncio.Event(), services=context.services)
    from_raw = Engine._budget_tool_message(message, next_context, 1000)
    from_checkpoint = Engine._budget_tool_message(first, next_context, 1000)
    tighter = Engine._budget_tool_message(first, next_context, 500)
    assert from_raw.content == first.content == from_checkpoint.content
    for projected in (from_raw, from_checkpoint, tighter):
        assert projected.metadata["output_artifact"] == source
    assert len(tighter.content) <= 500 and "TAIL" in tighter.content
    assert message == raw_before
    assert len([item for item in store.events(session_id=context.session_id)
                if item["type"] == "artifact.registered"]) == 1
    assert not next_context.artifact_dir.exists()
    assert context.services["artifact_read_resolver"](context.session_id, source["artifact_id"]).is_file()


async def test_text_search_reuses_local_20k_spill_and_preserves_recent_results(runtime):
    store, context, tools = runtime
    path = context.cwd / "searchable.txt"
    path.write_text("\n".join(f"needle evidence-{index}" for index in range(1000)), encoding="utf-8")
    result = await tools["text_search"].execute({"path": ".", "query": "needle", "limit": 1000}, context)
    assert not result.is_error and len(result.content) <= 20000
    assert len(result.artifacts) == 1
    source = result.artifacts[0]
    raw = raw_message(result, context, "text_search")
    first = Engine._budget_tool_message(raw, context, 50000)
    assert len(first.content) <= 20000 and "evidence-999" in first.content
    assert first.metadata["output_artifact"]["artifact_id"] == source["artifact_id"]
    raw.metadata["output_artifact"] = first.metadata["output_artifact"]
    store.append_message(context.session_id, context.run_id, raw)
    again = Engine._budget_tool_message(raw, context, 50000)
    assert again.content == first.content
    assert len(list(context.artifact_dir.glob("*.txt"))) == 1
    assert "evidence-999" in Path(source["path"]).read_text(encoding="utf-8")


async def test_file_read_validates_own_cap_and_supplied_column(runtime):
    _, context, tools = runtime
    path = context.cwd / "small.txt"
    path.write_text("short", encoding="utf-8")
    for arguments in ({"max_chars": 500001}, {"max_chars": 500}, {"start_column": 100}):
        result = await tools["file_read"].execute({"path": str(path), **arguments}, context)
        assert result.is_error and result.error_category == "validation"


async def test_completed_line_range_remains_cacheable_and_restorable_when_file_has_more(runtime):
    store, context, tools = runtime
    path = context.cwd / "ranged.txt"
    path.write_text("header\nEXACT_REQUESTED_LINE\nfooter\n", encoding="utf-8")
    observer = ObservationManager(store, context)
    call = ToolCall("requested-range", "file_read", {"path": str(path), "start_line": 2, "max_lines": 1})
    result = await tools["file_read"].execute(call.arguments, context)
    assert result.data["truncated"] and result.data["range_complete"]
    assert result.data["next_start_line"] == 3 and result.data["next_start_column"] == 1
    record = observer.record(call, result)
    assert record is not None
    observer.observe_visible([Message("tool", result.content, tool_call_id=call.id,
                                     metadata={"file_observation": record})])
    cached = await observer.cached(call)
    assert cached and cached.data["deduplicated"]
    restored = await ObservationManager(store, context).restore([], headroom_tokens=5000)
    assert len(restored) == 1 and "2: EXACT_REQUESTED_LINE" in restored[0].content
    assert "1: header" not in restored[0].content and "3: footer" not in restored[0].content


@pytest.mark.parametrize("extra", [{"start_column": 3}, {"max_chars": 512}])
async def test_narrow_column_or_character_window_cannot_certify_a_complete_line_cache(runtime, extra):
    store, context, tools = runtime
    path = context.cwd / "partial-range.txt"
    path.write_text("complete source " * 1000, encoding="utf-8")
    observer = ObservationManager(store, context)
    full_call = ToolCall("full", "file_read", {"path": str(path), "max_lines": 1})
    full_result = await tools["file_read"].execute(full_call.arguments, context)
    record = observer.record(full_call, full_result)
    observer.observe_visible([Message("tool", full_result.content, tool_call_id=full_call.id,
                                     metadata={"file_observation": record})])
    assert (await observer.cached(full_call)).data["deduplicated"]
    narrow_call = ToolCall("narrow", "file_read", {**full_call.arguments, **extra})
    narrow = await tools["file_read"].execute(narrow_call.arguments, context)
    assert narrow.data["range_complete"] is False
    assert observer.record(narrow_call, narrow) is None
    assert await observer.cached(full_call) is None
    assert not await observer.restore([], headroom_tokens=5000)
