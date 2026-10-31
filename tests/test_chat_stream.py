"""Nullable compatible Chat Completions deltas without partial tool dispatch."""
from __future__ import annotations

import asyncio
import json
import os

import httpx
import pytest

from aster.domain import Message, ToolCall
from aster.providers.compatible import CompatibleClient
from aster.providers.deepseek import ModelCapabilities, ProviderError


def chunk(delta=None, *, finish=None):
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def wire(events, *, done=True):
    return "".join("data: " + json.dumps(event, ensure_ascii=False) + "\r\n\r\n" for event in events) + (
        "data: [DONE]\r\n\r\n" if done else "")


def model_for(http, *, base_url="https://provider.example/v1"):
    model = CompatibleClient("fixture-key", protocol="openai_chat", base_url=base_url,
                             model="fixture-model", client=http, max_retries=0)
    model.capabilities[model.model] = ModelCapabilities(model.model, 50000, 8192, ("text",), (), "fixture", {})
    return model


async def generate(events, *, done=True, emit=None):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=wire(events, done=done))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await model_for(http).generate([Message("user", "fixture")], [], system="fixture", emit=emit)
    assert len(requests) == 1
    return result


async def test_nullable_text_and_usage_deltas_keep_emitted_text_and_completion():
    emitted = []

    async def emit(kind, value):
        emitted.append((kind, value))

    result = await generate([
        chunk({"role": "assistant", "content": None, "tool_calls": None}),
        chunk({"content": "Hello", "tool_calls": None}),
        chunk({"content": None, "tool_calls": None}, finish="stop"),
        {"choices": None, "usage": {"prompt_tokens": 4, "completion_tokens": 1,
                                   "prompt_tokens_details": None, "completion_tokens_details": None}},
    ], emit=emit)
    assert result.text == "Hello" and result.tool_calls == [] and result.finish_reason == "stop"
    assert result.usage.input_tokens == 4 and result.usage.output_tokens == 1
    assert result.usage.cache_hit_tokens is None and result.usage.reasoning_tokens is None
    assert emitted == [("model.text.delta", {"text": "Hello"})]


async def test_nullable_function_fields_aggregate_two_interleaved_complete_calls():
    result = await generate([
        chunk({"content": None, "tool_calls": [
            {"index": 0, "id": "c1", "type": "function", "function": {"name": "file_read", "arguments": None}},
            {"index": 1, "id": "c2", "type": "function", "function": {"name": "file_write", "arguments": '{"text":'}},
        ]}),
        chunk({"content": None, "tool_calls": None}),
        chunk({"tool_calls": [{"index": 0, "id": None, "type": None, "function": None}]}),
        chunk({"tool_calls": [
            {"index": 0, "function": {"name": None, "arguments": '{"path":'}},
            {"index": 1, "id": None, "function": {"name": None, "arguments": '"ok"}'}},
        ]}),
        chunk({"tool_calls": [{"index": 0, "function": {"name": None, "arguments": '"a.txt"}'}}]}),
        chunk({"content": None, "tool_calls": None}, finish="tool_calls"),
    ])
    assert result.tool_calls == [ToolCall("c1", "file_read", {"path": "a.txt"}),
                                 ToolCall("c2", "file_write", {"text": "ok"})]


@pytest.mark.parametrize("event", [
    [], None, {"choices": {}}, {"choices": None}, {"choices": [None]},
    {"choices": [{"index": False, "delta": {}}]},
    chunk([]), chunk(False), chunk({"content": []}), chunk({"content": {"text": "unsafe"}}),
    chunk({"tool_calls": {}}), chunk({"tool_calls": "unsafe"}), chunk({"tool_calls": [None]}),
    chunk({"tool_calls": [{"index": False}]}),
    chunk({"tool_calls": [{"index": 0, "id": 7}]}),
    chunk({"tool_calls": [{"index": 0, "type": "custom"}]}),
    chunk({"tool_calls": [{"index": 0, "function": []}]}),
    chunk({"tool_calls": [{"index": 0, "function": {"name": False}}]}),
    chunk({"tool_calls": [{"index": 0, "function": {"arguments": {"path": "unsafe"}}}]}),
    {"choices": [], "usage": []},
    {"choices": None, "usage": {"prompt_tokens_details": []}},
    {"choices": None, "usage": {"completion_tokens_details": False}},
])
async def test_wrong_types_remain_protocol_errors_without_returning_calls(event):
    with pytest.raises(ProviderError) as error:
        await generate([event, chunk({}, finish="stop")])
    assert error.value.category == "protocol" and not error.value.retryable
    assert "unsafe" not in str(error.value)


@pytest.mark.parametrize("events,done", [
    ([chunk({"content": "text", "tool_calls": None})], True),
    ([chunk({"content": "text", "tool_calls": None}, finish="stop")], False),
    ([{"choices": None, "usage": {"prompt_tokens": 1}}], True),
    ([chunk({"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "write", "arguments": '{"x":'}}]},
            finish="tool_calls")], True),
    ([chunk({"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "write", "arguments": "{}"}}]},
            finish="stop")], True),
    ([chunk({"content": None, "tool_calls": None}, finish="tool_calls")], True),
    ([chunk({"tool_calls": [{"index": 0, "id": None, "function": {"name": "write", "arguments": "{}"}}]},
            finish="tool_calls")], True),
    ([chunk({"tool_calls": [{"index": 0, "id": "c1", "function": {"name": None, "arguments": "{}"}}]},
            finish="tool_calls")], True),
    ([chunk({"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "write", "arguments": "[]"}}]},
            finish="tool_calls")], True),
])
async def test_null_compatibility_never_completes_partial_or_missing_tool_exchange(events, done):
    with pytest.raises(ProviderError) as error:
        await generate(events, done=done)
    assert error.value.category in {"incomplete", "invalid_tool"} and not error.value.retryable


async def test_nullable_partial_tool_then_stream_error_never_returns_calls_or_echoes_error():
    with pytest.raises(ProviderError) as error:
        await generate([
            chunk({"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "write", "arguments": "{}"}}]}),
            {"error": {"message": "fixture-key"}},
        ])
    assert error.value.category == "protocol" and "fixture-key" not in str(error.value)


async def test_cancellation_after_nullable_tool_delta_closes_without_dispatch():
    started, closed, cancel = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            try:
                yield wire([chunk({"tool_calls": [{"index": 0, "id": "c1", "function": None}]})], done=False).encode()
                started.set()
                await asyncio.Event().wait()
            finally:
                closed.set()

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Stream()))) as http:
        task = asyncio.create_task(model_for(http).generate([], [], system="fixture", cancel=cancel))
        await asyncio.wait_for(started.wait(), 2)
        cancel.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert closed.is_set()


@pytest.mark.windows
@pytest.mark.skipif(os.name != "nt", reason="Windows HTTP/SSE socket integration")
async def test_windows_real_http_fragmentation_handles_nullable_utf8_sse():
    requests, handlers = [], set()
    completed = asyncio.Event()
    body = (": keepalive\r\n\r\n" + wire([
        chunk({"role": "assistant", "content": None, "tool_calls": None}),
        chunk({"content": "你好", "tool_calls": None}),
        chunk({"content": None, "tool_calls": None}, finish="stop"),
        {"choices": None, "usage": {"prompt_tokens": 2, "completion_tokens": 1}},
    ])).encode("utf-8")

    async def serve(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            length = next(int(line.split(b":", 1)[1]) for line in headers.split(b"\r\n")
                          if line.lower().startswith(b"content-length:"))
            requests.append((headers.split(b"\r\n", 1)[0], json.loads(await reader.readexactly(length))))
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n"
                         b"Connection: close\r\n\r\n")
            # Small arbitrary network fragments split JSON, CRLF and UTF-8 bytes.
            for offset in range(0, len(body), 11):
                part = body[offset:offset + 11]
                writer.write(f"{len(part):X}\r\n".encode() + part + b"\r\n")
                await writer.drain()
                await asyncio.sleep(0)
            writer.write(b"0\r\n\r\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            completed.set()
            handlers.discard(task)

    async with await asyncio.start_server(serve, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with httpx.AsyncClient(trust_env=False, timeout=3) as http:
            model = model_for(http, base_url=f"http://127.0.0.1:{port}/v1")
            turn = await model.generate([], [], system="fixture")
        await asyncio.wait_for(completed.wait(), 2)
    if handlers:
        await asyncio.gather(*handlers)
    assert turn.text == "你好" and turn.tool_calls == []
    assert turn.usage.input_tokens == 2 and turn.usage.output_tokens == 1
    assert len(requests) == 1 and requests[0][0] == b"POST /v1/chat/completions HTTP/1.1"
    assert requests[0][1]["stream"] is True
