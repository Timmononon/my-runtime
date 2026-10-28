import asyncio
import base64
import io
import json

import httpx
import pytest
from PIL import Image

from aster.domain import Message, ToolCall
from aster.providers.deepseek import DeepSeekResponsesClient, ProviderError, encode_messages


def png_fixture():
    output = io.BytesIO()
    Image.new("RGB", (2, 2), "white").save(output, "PNG")
    return output.getvalue()


def test_runtime_text_result_with_empty_multimodal_parts_is_not_erased():
    from aster.domain import ToolResult
    from aster.engine import Engine
    from aster.providers.deepseek import encode_messages

    call = ToolCall("real-call", "glob", {"path": ".", "pattern": "*.ini"})
    output = Engine._tool_message(call.id, ToolResult('["nested/settings.ini"]'), "run")
    encoded = encode_messages([Message("assistant", tool_calls=[call]), output])
    assert encoded[-1] == {"type": "function_call_output", "call_id": "real-call", "output": '["nested/settings.ini"]'}

def sse(*events):
    return "".join(
        "event: " + event["type"] + "\ndata: " + json.dumps(event, ensure_ascii=False) + "\n\n"
        for event in events
    ).encode()


def completed(output=None, usage=None):
    response = {
        "status": "completed",
        "output": output
        if output is not None
        else [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "完成"}]}],
    }
    if usage is not None:
        response["usage"] = usage
    return {"type": "response.completed", "response": response}


@pytest.mark.asyncio
async def test_interleaved_calls_reasoning_and_unknown_usage():
    items = [
        {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "分析"}]},
        {"type": "function_call", "call_id": "a", "name": "read", "arguments": '{"path":"中文"}'},
        {"type": "function_call", "call_id": "b", "name": "read", "arguments": '{"path":"second"}'},
    ]
    events = [
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"type": "reasoning", "content": []},
        },
        {"type": "response.reasoning_text.delta", "output_index": 0, "content_index": 0, "delta": "分析"},
        {
            "type": "response.output_item.added",
            "output_index": 1,
            "item": {"type": "function_call", "call_id": "a", "name": "read", "arguments": ""},
        },
        {
            "type": "response.output_item.added",
            "output_index": 2,
            "item": {"type": "function_call", "call_id": "b", "name": "read", "arguments": ""},
        },
        {"type": "response.function_call_arguments.delta", "output_index": 1, "delta": '{"path":'},
        {"type": "response.function_call_arguments.delta", "output_index": 2, "delta": '{"path":"second"}'},
        {"type": "response.function_call_arguments.delta", "output_index": 1, "delta": '"中文"}'},
        completed(items),
    ]
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse(*events))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = DeepSeekResponsesClient("secret", client=http)
        turn = await client.generate([Message("user", "read")], [], system="rules")
    assert [call.arguments for call in turn.tool_calls] == [{"path": "中文"}, {"path": "second"}]
    assert turn.provider_items == items
    assert turn.usage.input_tokens is None
    assert requests[0]["instructions"] == "rules"
    assistant = Message("assistant", provider_items=turn.provider_items, tool_calls=turn.tool_calls)
    wire = encode_messages(
        [
            assistant,
            Message("tool", "A", tool_call_id="a"),
            Message("tool", "B", tool_call_id="b"),
            Message("user", "continue"),
        ]
    )
    assert wire[0]["type"] == "reasoning"
    assert wire[-1]["content"] == "continue"


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [None, "response.incomplete", "response.failed"])
async def test_partial_stream_never_returns_tool(terminal):
    events = [
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"type": "function_call", "call_id": "x", "name": "write", "arguments": '{"x":'},
        }
    ]
    if terminal:
        events.append({"type": terminal, "response": {"status": terminal.rsplit(".", 1)[1]}})
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, content=sse(*events)
            )
        )
    ) as http:
        with pytest.raises(ProviderError):
            await DeepSeekResponsesClient("key", client=http).generate([Message("user", "x")], [], system="")


@pytest.mark.asyncio
async def test_retry_429_then_usage():
    attempts = 0

    def handle(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"retry-after": "0"}, json={"secret": "never expose"})
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse(
                completed(
                    usage={
                        "input_tokens": 20,
                        "output_tokens": 8,
                        "input_tokens_details": {"cached_tokens": 12},
                        "output_tokens_details": {"reasoning_tokens": 4},
                    }
                )
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        turn = await DeepSeekResponsesClient("key", client=http).generate(
            [Message("user", "x")], [], system=""
        )
    assert attempts == 2
    assert (turn.usage.cache_hit_tokens, turn.usage.cache_miss_tokens, turn.usage.reasoning_tokens) == (
        12,
        8,
        4,
    )


class WaitingStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        await asyncio.sleep(60)
        yield b""


@pytest.mark.asyncio
async def test_cancel_closes_pending_read():
    cancel = asyncio.Event()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda req: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=WaitingStream()
            )
        )
    ) as http:
        task = asyncio.create_task(
            DeepSeekResponsesClient("key", client=http).generate(
                [Message("user", "x")], [], system="", cancel=cancel
            )
        )
        await asyncio.sleep(0.01)
        cancel.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)


@pytest.mark.asyncio
async def test_no_credentials_clear_error_without_request():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: pytest.fail("network request"))
    ) as http:
        client = DeepSeekResponsesClient("", client=http)
        assert not client.is_configured()
        with pytest.raises(ProviderError, match="not configured") as error:
            await client.generate([Message("user", "x")], [], system="")
        assert error.value.category == "authentication"


def test_orphan_duplicate_and_reasoning_history():
    with pytest.raises(ProviderError, match="Orphan"):
        encode_messages([Message("tool", "x", tool_call_id="orphan")])
    with pytest.raises(ProviderError, match="unresolved"):
        encode_messages([Message("assistant", tool_calls=[ToolCall("a", "read", {})])])
    wire = encode_messages([Message("assistant", "done", reasoning="reason"), Message("user", "next")])
    assert wire[0]["content"][0]["text"] == "reason"


def test_image_reference_is_encoded_only_for_request(tmp_path):
    root = tmp_path / "artifacts"
    root.mkdir()
    image = root / "image.png"
    image.write_bytes(png_fixture())
    message = Message(
        "tool",
        "screenshot",
        tool_call_id="a",
        metadata={
            "artifacts": [{"artifact_id": "image", "path": str(image)}],
            "content_parts": [
                {"type": "input_image", "path": str(image), "artifact_id": "image", "mime_type": "image/png"}
            ],
        },
    )
    history = [Message("assistant", tool_calls=[ToolCall("a", "screenshot", {})]), message]
    wire = encode_messages(history, root)
    assert wire[-1]["output"][0]["image_url"].startswith("data:image/png;base64,")
    assert "image_url" not in message.metadata["content_parts"][0]
    with pytest.raises(ProviderError, match="scope"):
        encode_messages(history, root / "other")


@pytest.mark.asyncio
async def test_model_capability_body_is_validated_and_images_rejected():
    response = {
        "data": [
            {
                "id": "deepseek-v4-pro",
                "context_window": 100,
                "max_output_tokens": 20,
                "input_modalities": ["text"],
            }
        ]
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json=response))
    ) as http:
        client = DeepSeekResponsesClient("key", client=http, model="deepseek-v4-pro", max_output_tokens=10)
        caps = await client.refresh_capabilities()
        assert caps["deepseek-v4-pro"].context_window == 100
        with pytest.raises(ProviderError, match="Image capability"):
            await client.generate(
                [
                    Message(
                        "user",
                        metadata={
                            "content_parts": [
                                {"type": "input_image", "image_url": "data:image/png;base64," + base64.b64encode(png_fixture()).decode()}
                            ]
                        },
                    )
                ],
                [],
                system="",
            )


@pytest.mark.asyncio
async def test_http_diagnostics_are_allowlisted_and_redacted():
    key = "sk-test-super-secret-value"
    raw = {
        "error": {
            "code": "invalid",
            "type": "invalid_request_error",
            "param": "input",
            "message": f'Bad tool history. Authorization: Bearer {key}; request body: {{"input":"private prompt"}}',
            "request": {"api_key": key},
            "headers": {"Authorization": key},
        }
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(400, json=raw))
    ) as http:
        with pytest.raises(ProviderError) as failure:
            await DeepSeekResponsesClient(key, client=http, max_retries=0).generate(
                [Message("user", "hello")], [], system=""
            )
    details = failure.value.details
    assert set(details) == {"code", "type", "param", "message"}
    serialized = json.dumps(details)
    assert key not in serialized and "private prompt" not in serialized
    assert details["param"] == "input"


@pytest.mark.asyncio
async def test_credentials_never_follow_redirects():
    urls = []

    def handle(request):
        urls.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://attacker.invalid/steal"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle), follow_redirects=True) as http:
        with pytest.raises(ProviderError) as error:
            await DeepSeekResponsesClient("secret", client=http).generate(
                [Message("user", "test")], [], system=""
            )
    assert error.value.status_code == 302
    assert urls == ["https://api.deepseek.com/responses"]
