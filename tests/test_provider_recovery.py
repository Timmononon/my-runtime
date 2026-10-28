"""Real protocol responses drive offline Engine compaction and bounded recovery."""
import asyncio
import copy
import json
import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

from aster.domain import Message, ToolCall, ToolResult, ToolSpec, to_dict
from aster.engine import Engine, RunConfig
from aster.policy import PermissionPolicy
from aster.providers.compatible import CompatibleClient
from aster.providers.deepseek import DeepSeekResponsesClient, ModelCapabilities, _retry_after
from aster.state import Store
from aster.tools.registry import Registry

KEY = "sk-offline-fixture-secret"


def sse(*events):
    return "".join("data: " + json.dumps(event) + "\n\n" for event in events).encode()


def success(protocol, text="done", *, function=None):
    if protocol == "openai_chat":
        delta = ({"tool_calls": [{"index": 0, "id": function["call_id"], "function": {
            "name": function["name"], "arguments": function["arguments"]}}]} if function else {"content": text})
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse(
            {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls" if function else "stop"}]},
        ) + b"data: [DONE]\n\n")
    output = [function] if function else [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}]
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse(
        {"type": "response.completed", "response": {"status": "completed", "output": output}}))


def model(protocol, http):
    if protocol == "deepseek":
        return DeepSeekResponsesClient(KEY, client=http, max_retries=0)
    result = CompatibleClient(KEY, protocol=protocol, base_url="https://fixture.example/v1",
                              model="offline-model", client=http, max_retries=7)
    result.capabilities[result.model] = ModelCapabilities(result.model, 1000000, 8192, ("text",), (), "offline", {})
    return result


def seed(store, tmp_path):
    sid = store.create_session(tmp_path)["id"]
    prior, _ = store.create_run(sid)
    messages = [Message("user", "Keep D:/original.csv unchanged")]
    for number in range(10):
        messages.extend([Message("assistant", tool_calls=[ToolCall(f"old-{number}", "old_read", {})]),
                         Message("tool", "Full historical evidence " + "R" * 2000, tool_call_id=f"old-{number}")])
    for message in messages:
        store.append_message(sid, prior["id"], message)
    store.set_run_status(prior["id"], "completed")
    return sid, copy.deepcopy(to_dict(messages))


@pytest.mark.parametrize("protocol,terminal", [
    ("deepseek", False), ("openai_responses", False), ("openai_chat", False),
    ("deepseek", True), ("openai_responses", True),
])
async def test_context_error_protocol_drives_exactly_one_reactive_compaction(protocol, terminal, tmp_path):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            error = {"code": "context_length_exceeded", "message": f"echo {KEY}: private submitted task"}
            if terminal:
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse(
                    {"type": "response.failed", "response": {"status": "failed", "error": error}}))
            return httpx.Response(400, json={"error": error})
        return success(protocol)

    store = Store(tmp_path / "runtime.db")
    sid, archive = seed(store, tmp_path)
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            outcome = await Engine(model(protocol, http), Registry(), store, config=RunConfig(
                context_tokens=1000000, semantic_compaction=False, file_restore_max_files=0)).run(sid, "Continue")
        assert outcome.status == "completed", outcome.error
        assert len(requests) == 2
        field = "messages" if protocol == "openai_chat" else "input"
        assert len(requests[1][field]) < len(requests[0][field])
        assert "Keep D:/original.csv unchanged" in json.dumps(requests[1])
        events = store.events(run_id=outcome.run_id)
        assert len([event for event in events if event["type"] == "context.compacted"]) == 1
        assert not any(event["type"] == "model.retry" for event in events)
        assert store.get_checkpoint(sid)["metadata"].get("reactive_compaction") is None  # final checkpoint succeeds
        assert to_dict(store.history(sid)[:len(archive)]) == archive
        assert len(store.history(sid)) == len(archive) + 2
        assert "private submitted task" not in json.dumps(events) and KEY not in json.dumps(events)
    finally:
        store.close()


@pytest.mark.parametrize("code", ["invalid_request", "context_length_exceeded extra", {"code": "context_length_exceeded"}])
async def test_free_text_or_malformed_context_identifier_does_not_trigger_compaction(code, tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(400, json={"error": {"code": code, "message": "context_length_exceeded in echoed request"}})

    store = Store(tmp_path / "runtime.db")
    sid, _ = seed(store, tmp_path)
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            result = await Engine(model("openai_chat", http), Registry(), store, config=RunConfig(
                context_tokens=1000000, semantic_compaction=False)).run(sid, "Continue")
        assert result.status == "failed" and len(calls) == 1
        assert not any(event["type"].startswith("context.compact") for event in store.events(run_id=result.run_id))
    finally:
        store.close()


async def test_repeated_context_rejection_is_bounded_at_two_requests(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(400, json={"error": {"type": "context_overflow"}})

    store = Store(tmp_path / "runtime.db")
    sid, _ = seed(store, tmp_path)
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            result = await Engine(model("openai_responses", http), Registry(), store, config=RunConfig(
                context_tokens=1000000, semantic_compaction=False)).run(sid, "Continue")
        assert result.status == "failed" and len(requests) == 2
        assert sum(event["type"] == "context.compacted" for event in store.events(run_id=result.run_id)) == 1
    finally:
        store.close()


@pytest.mark.parametrize("protocol", ["deepseek", "openai_responses"])
@pytest.mark.parametrize("partial", ["text", "tool", "terminal_output"])
async def test_failed_responses_after_any_output_never_replays_or_dispatches(protocol, partial, tmp_path):
    requests = []
    events = []
    if partial == "text":
        events.extend([{"type": "response.output_item.added", "output_index": 0, "item": {"type": "message", "content": []}},
                       {"type": "response.output_text.delta", "output_index": 0, "delta": "partial answer"}])
    if partial == "tool":
        events.append({"type": "response.output_item.added", "output_index": 0,
                       "item": {"type": "function_call", "call_id": "partial", "name": "write_once", "arguments": "{"}})
    failed = {"status": "failed", "error": {"code": "context_length_exceeded"}}
    if partial == "terminal_output":
        failed["output"] = [{"type": "function_call", "call_id": "partial", "name": "write_once", "arguments": "{}"}]
    events.append({"type": "response.failed", "response": failed})

    def handler(request):
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse(*events))

    store = Store(tmp_path / "runtime.db")
    sid, _ = seed(store, tmp_path)
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            result = await Engine(model(protocol, http), Registry(), store, config=RunConfig(
                context_tokens=1000000, semantic_compaction=False)).run(sid, "Continue")
        assert result.status == "failed" and len(requests) == 1
        assert store.tool_executions(result.run_id) == []
        assert not any(event["type"].startswith("context.compact") for event in store.events(run_id=result.run_id))
    finally:
        store.close()


class WriteOnce:
    spec = ToolSpec("write_once", "Persist one synthetic effect", {"type": "object", "properties": {}, "required": []}, risk="write")

    async def execute(self, arguments, context):
        path = context.cwd / "effect.txt"
        assert not path.exists()
        path.write_text("exactly once", encoding="utf-8")
        return ToolResult("Effect persisted")


@pytest.mark.parametrize("protocol", ["openai_chat", "openai_responses"])
@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
async def test_retry_after_http_rejection_does_not_replay_completed_material_tool(protocol, status, tmp_path):
    requests = []
    function = {"type": "function_call", "call_id": "material", "name": "write_once", "arguments": "{}"}

    def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return success(protocol, function=function)
        if len(requests) == 2:
            return httpx.Response(status, headers={"retry-after": "0"}, json={"error": {"message": KEY + " private prompt"}})
        return success(protocol)

    store = Store(tmp_path / "runtime.db")
    sid = store.create_session(tmp_path)["id"]
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            result = await Engine(model(protocol, http), Registry([WriteOnce()]), store,
                                  policy=PermissionPolicy(allowed_tools={"write_once"}),
                                  config=RunConfig(retry_base_seconds=0.001)).run(sid, "Write once and finish")
        assert result.status == "completed", result.error
        assert len(requests) == 3 and requests[1] == requests[2]
        assert (tmp_path / "effect.txt").read_text() == "exactly once"
        assert len(store.tool_executions(result.run_id)) == 1
        assert sum(event["type"] == "model.retry" for event in store.events(run_id=result.run_id)) == 1
        assert KEY not in json.dumps(store.events(run_id=result.run_id))
    finally:
        store.close()


@pytest.mark.parametrize("failure,expected", [
    (httpx.ConnectError, 3), (httpx.ConnectTimeout, 3), (httpx.PoolTimeout, 3),
    (httpx.ReadError, 1), (httpx.ReadTimeout, 1), (httpx.WriteError, 1),
    (httpx.WriteTimeout, 1), (httpx.RemoteProtocolError, 1),
])
async def test_engine_retries_only_safe_pre_send_transport_failures(failure, expected, tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        raise failure("Synthetic transport failure", request=request)

    store = Store(tmp_path / "runtime.db")
    sid = store.create_session(tmp_path)["id"]
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            result = await Engine(model("openai_chat", http), Registry(), store,
                                  config=RunConfig(model_retries=2, retry_base_seconds=0.001)).run(sid, "Say hello")
        assert result.status == "failed" and len(requests) == expected
        assert sum(event["type"] == "model.retry" for event in store.events(run_id=result.run_id)) == expected - 1
    finally:
        store.close()


@pytest.mark.parametrize("protocol", ["openai_chat", "openai_responses"])
async def test_compatible_http_retry_budget_is_not_multiplied_by_adapter(protocol, tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(503, headers={"retry-after": "0"})

    store = Store(tmp_path / "runtime.db")
    try:
        sid = store.create_session(tmp_path)["id"]
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            result = await Engine(model(protocol, http), Registry(), store,
                                  config=RunConfig(model_retries=2)).run(sid, "Say hello")
        assert result.status == "failed" and len(requests) == 3
        assert len(store.usage(result.run_id)) == 3
    finally:
        store.close()


@pytest.mark.parametrize("partial", ["text", "tool"])
async def test_chat_transport_error_after_partial_output_is_not_retried(partial, tmp_path):
    requests = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            delta = ({"content": "visible partial"} if partial == "text" else {
                "tool_calls": [{"index": 0, "id": "partial", "function": {"name": "write_once", "arguments": "{"}}]})
            yield sse({"choices": [{"index": 0, "delta": delta, "finish_reason": None}]})
            # Even a misleadingly categorized transport exception after output
            # cannot make an accepted request safe to replay.
            raise httpx.ConnectError("Synthetic dropped stream")

    def handler(request):
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream())

    store = Store(tmp_path / "runtime.db")
    try:
        sid = store.create_session(tmp_path)["id"]
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            result = await Engine(model("openai_chat", http), Registry([WriteOnce()]), store).run(sid, "Write once")
        assert result.status == "failed" and len(requests) == 1
        assert store.tool_executions(result.run_id) == [] and not (tmp_path / "effect.txt").exists()
    finally:
        store.close()


async def test_engine_honors_retry_after_and_can_cancel_the_wait(tmp_path):
    requested = asyncio.Event()
    requests = []

    def handler(request):
        requests.append(time.monotonic())
        requested.set()
        return httpx.Response(429, headers={"retry-after": "0.04"}) if len(requests) < 2 else success("openai_chat")

    store = Store(tmp_path / "runtime.db")
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            engine = Engine(model("openai_chat", http), Registry(), store, config=RunConfig(retry_base_seconds=0))
            sid = store.create_session(tmp_path)["id"]
            first = await engine.run(sid, "Try")
            assert first.status == "completed" and requests[1] - requests[0] >= 0.035
            requests.clear()
            requested.clear()
            sid = store.create_session(tmp_path)["id"]
            work = asyncio.create_task(engine.run(sid, "Try and cancel"))
            await asyncio.wait_for(requested.wait(), 1)
            await asyncio.sleep(0.005)
            rid = store.list_runs(sid)[0]["id"]
            assert engine.cancel(rid)
            assert (await asyncio.wait_for(work, 1)).status == "cancelled"
            assert len(requests) == 1
    finally:
        store.close()


def test_retry_after_supports_http_dates_and_rejects_nonfinite_values():
    date = format_datetime(datetime.now(UTC) + timedelta(seconds=20), usegmt=True)
    assert 18 <= _retry_after(date) <= 20
    assert _retry_after("999") == 60
    assert _retry_after("-1") == 0
    assert all(_retry_after(value) is None for value in ("invalid", "nan", "inf"))
