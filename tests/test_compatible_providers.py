import asyncio
import copy
import json

import httpx
import pytest

from aster.domain import Message, ToolCall, ToolSpec
from aster.providers.compatible import CompatibleClient, ScopedModel
from aster.providers.deepseek import ModelCapabilities, ProviderError


def client(protocol, handler, *, effort=None):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    model = CompatibleClient("fixture-key", protocol=protocol, base_url="https://provider.example/v1",
                             model="fixture-model", effort=effort, client=http, max_retries=0)
    model.capabilities[model.model] = ModelCapabilities(model.model, 50000, 8192, ("text", "image"), ("low", "high"), "fixture", {})
    return model, http


def response_stream(output, *, status="completed", usage=None):
    terminal = {"type": "response." + status, "sequence_number": 1,
                "response": {"status": status, "output": output, "usage": usage}}
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, text="data: " + json.dumps(terminal) + "\n\n")


async def test_responses_preserves_same_identity_encrypted_reasoning_and_tool_exchange():
    requests = []
    reasoning = {"type": "reasoning", "id": "r1", "summary": [{"type": "summary_text", "text": "short reasoning summary"}], "encrypted_content": "opaque-ciphertext"}
    function = {"type": "function_call", "id": "fc1", "call_id": "call1", "name": "file_read", "arguments": '{"path":"note.txt"}'}

    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert request.url.path == "/v1/responses"
        assert body["store"] is False and body["include"] == ["reasoning.encrypted_content"]
        assert body["reasoning"] == {"effort": "high"}
        if len(requests) == 1:
            return response_stream([reasoning, function])
        return response_stream([{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "complete"}]}],
                               usage={"input_tokens": 15, "output_tokens": 4})

    raw, http = client("openai_responses", handler, effort="high")
    model = ScopedModel(raw, {"provider_id": "p1", "model": "fixture-model"}, lambda: None)
    async with http:
        first = await model.generate([Message("user", "read")], [], system="system")
        assert first.tool_calls[0].arguments == {"path": "note.txt"}
        messages = [Message("user", "read"), Message("assistant", first.text, tool_calls=first.tool_calls, provider_items=first.provider_items),
                    Message("tool", "sha256: actual-hash", tool_call_id="call1")]
        original = copy.deepcopy(messages)
        second = await model.generate(messages, [], system="system")
    assert second.text == "complete" and second.usage.input_tokens == 15
    serialized = requests[1]["input"]
    assert next(row for row in serialized if row.get("type") == "reasoning") == reasoning
    assert serialized[-1] == {"type": "function_call_output", "call_id": "call1", "output": "sha256: actual-hash"}
    assert "_aster_provider" not in json.dumps(serialized)
    assert messages == original


async def test_switch_provider_or_model_strips_opaque_items_but_keeps_actual_tool_result():
    received = []

    def handler(request):
        received.append(json.loads(request.content))
        return response_stream([{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "converted"}]}])

    raw, http = client("openai_responses", handler)
    model = ScopedModel(raw, {"provider_id": "new", "model": "new-model"}, lambda: None)
    items = [{"type": "reasoning", "encrypted_content": "old-provider-private", "summary": [], "_aster_provider": {"provider_id": "old", "model": "old-model"}}]
    messages = [Message("user", "read"), Message("assistant", "I read it", tool_calls=[ToolCall("call1", "file_read", {})], provider_items=items),
                Message("tool", "actual file content", tool_call_id="call1")]
    async with http:
        await model.generate(messages, [], system="system")
    wire = json.dumps(received[0])
    assert "old-provider-private" not in wire and "actual file content" in wire and "call1" in wire
    assert messages[1].provider_items[0]["encrypted_content"] == "old-provider-private"


async def test_chat_stream_aggregates_complete_calls_and_preserves_text_results():
    requests = []
    chunks = [
        {"choices": [{"index": 0, "delta": {"content": "Reading", "tool_calls": [{"index": 0, "id": "c1", "type": "function", "function": {"name": "file_read", "arguments": '{"pa'}}]}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'th":"a.txt"}'}}]}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 4, "prompt_tokens_details": {"cached_tokens": 7}, "completion_tokens_details": {"reasoning_tokens": 2}}},
    ]

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text="".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n")

    model, http = client("openai_chat", handler, effort="low")
    emitted = []

    async def emit(kind, value):
        emitted.append((kind, value))

    async with http:
        turn = await model.generate([Message("user", "read")], [ToolSpec("file_read", "read", {"type": "object"})], system="safe", emit=emit)
    assert turn.tool_calls == [ToolCall("c1", "file_read", {"path": "a.txt"})]
    assert turn.usage.input_tokens == 12 and turn.usage.output_tokens == 4 and turn.usage.reasoning_tokens == 2
    assert turn.usage.cache_miss_tokens == 5
    assert requests[0]["messages"][0] == {"role": "system", "content": "safe"}
    assert requests[0]["tools"][0]["function"]["name"] == "file_read"
    assert emitted == [("model.text.delta", {"text": "Reading"})]


@pytest.mark.parametrize("finish,arguments", [("length", '{"path":"a"}'), ("tool_calls", '{"path":'), (None, '{}'), ("content_filter", '{}')])
async def test_chat_incomplete_or_invalid_arguments_never_return_calls(finish, arguments):
    delta = {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "write", "arguments": arguments}}]}, "finish_reason": finish}]}
    model, http = client("openai_chat", lambda _: httpx.Response(200, headers={"content-type": "text/event-stream"},
                        text="data: " + json.dumps(delta) + "\n\ndata: [DONE]\n\n"))
    async with http:
        with pytest.raises(ProviderError):
            await model.generate([Message("user", "task")], [], system="safe")


async def test_chat_tool_images_use_explicit_user_observation_and_real_hash(tmp_path):
    from PIL import Image

    from aster.providers.compatible import chat_input

    image = tmp_path / "image.png"
    Image.new("RGB", (2, 2)).save(image)
    artifact = {"artifact_id": "a1", "path": str(image), "mime_type": "image/png"}
    tool = Message("tool", "image plus sha256", tool_call_id="c1", metadata={"artifacts": [artifact], "content_parts": [
        {"type": "input_text", "text": "image plus sha256"}, {"type": "input_image", **artifact}]})
    messages = [Message("assistant", tool_calls=[ToolCall("c1", "screenshot", {})]), tool]
    encoded = chat_input(messages, "system", tmp_path)
    assert encoded[2] == {"role": "tool", "tool_call_id": "c1", "content": "image plus sha256"}
    assert encoded[3]["role"] == "user" and encoded[3]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "image_url" not in tool.metadata["content_parts"][1]


async def test_cancellation_closes_stream_without_emitting_tool_call():
    started, closed, cancel = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            started.set()
            try:
                await asyncio.Event().wait()
                yield b"unreachable"
            finally:
                closed.set()

    model, http = client("openai_chat", lambda _: httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Stream()))
    async with http:
        work = asyncio.create_task(model.generate([Message("user", "read")], [], system="safe", cancel=cancel))
        await asyncio.wait_for(started.wait(), 2)
        cancel.set()
        with pytest.raises(asyncio.CancelledError):
            await work
        assert closed.is_set()


async def test_provider_http_errors_do_not_retain_echoed_keys():
    model, http = client("openai_chat", lambda _: httpx.Response(400, json={"error": {"message": "fixture-key"}}))
    async with http:
        with pytest.raises(ProviderError) as failure:
            await model.generate([], [], system="safe")
    assert "fixture-key" not in str(failure.value) and failure.value.details == {}


async def test_deepseek_null_effort_omits_reasoning_and_none_is_explicit():
    from aster.providers.deepseek import DeepSeekResponsesClient

    bodies = []
    def handler(request):
        bodies.append(json.loads(request.content))
        return response_stream([{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]}])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        for effort in (None, "none"):
            model = DeepSeekResponsesClient("fixture-key", effort=effort, client=http, max_retries=0)
            await model.generate([Message("user", "test")], [], system="system")
    assert "reasoning" not in bodies[0]
    assert bodies[1]["reasoning"] == {"effort": "none"}
