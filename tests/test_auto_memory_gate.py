"""Offline TypeSafe wire, bounded inputs and source-only memory extraction."""
import asyncio
import json

import httpx
import pytest

from aster.memory.auto_capture import (
    DEFAULTS,
    JEV_ENDPOINT,
    JevMemoryGate,
    build_capture_state,
    contains_sensitive_text,
    parse_candidates,
    validate_auto_memory_config,
)
from aster.providers.deepseek import ProviderError


class Credentials:
    def __init__(self, key="offline-test-key"):
        self.key = key

    def resolve(self, reference):
        assert reference == "keyring:aster/jev"
        return self.key


def state():
    return build_capture_state([{"id": "m1", "role": "user", "content": "以后默认中文回答。"}],
                               assistant_text="好的。")


def config(**changes):
    return {**DEFAULTS, "enabled": True, **changes}


def response(probability=0.92, **changes):
    return {"model": "jev-1.14.0", "answers": {"remember": {"type": "noul", "noul": probability}},
            "usage": {"input_tokens": 42, "output_tokens": 4}, **changes}


def gate(handler, *, credentials=None):
    return JevMemoryGate(credentials or Credentials(),
                         client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def fact(**changes):
    return {"entity": "user", "relation": "preferred_language", "value": "中文",
            "message_id": "m1", "quote": "以后默认中文回答。", **changes}


def extraction(*facts):
    return json.dumps({"facts": list(facts)}, ensure_ascii=False)


@pytest.mark.parametrize("change", [
    {"enabled": 1}, {"model": "secret value"}, {"extractor_model": ""},
    {"threshold": float("nan")}, {"threshold": True}, {"threshold": 1.1},
    {"timeout_seconds": float("inf")}, {"timeout_seconds": 0},
    {"input_chars": True}, {"input_chars": 99}, {"max_facts": 0}, {"max_facts": 21},
    {"credential_ref": "plaintext-secret"}, {"api_key": "plaintext-secret"},
    {"base_url": "https://foreign.example"},
])
def test_config_denies_unsafe_or_unbounded_fields(change):
    with pytest.raises(ValueError) as error:
        validate_auto_memory_config(change)
    assert "plaintext-secret" not in str(error.value)


def test_config_default_disabled_and_copies_defaults():
    result = validate_auto_memory_config({})
    assert result == DEFAULTS and not result["enabled"]
    result["enabled"] = True
    assert not DEFAULTS["enabled"]
    assert validate_auto_memory_config({"credential_ref": "env:TYPESAFE_API_KEY"})["credential_ref"].startswith("env:")


@pytest.mark.parametrize("secret", [
    "这是 key：sk-this-is-a-fake-test-key-12345",
    "DEEPSEEK_API_KEY=fake-sample-credential",
    '"api_key": "fake-sample-credential"',
    "Authorization: Bearer fake-sample-credential",
    "我的密码是 123456",
    "password = hunter2",
    "-----BEGIN PRIVATE KEY-----\nonly-test-data",
    "https://user:password@example.com/api",
    "AKIA1234567890ABCDEF",
])
def test_secret_messages_are_omitted_before_truncation(secret):
    assert contains_sensitive_text(secret)
    assert build_capture_state([{"id": "m", "role": "user", "content": "x" * 200 + secret}],
                               input_chars=100) is None
    result = build_capture_state([
        {"id": "m0", "role": "user", "content": secret},
        {"id": "m1", "role": "user", "content": "请默认用中文回答"},
    ], context_messages=[{"role": "assistant", "content": secret}], assistant_text=secret)
    assert result["omitted_sensitive_messages"] == 3
    assert result["recent_context"] == [] and result["assistant_answer"] == ""
    assert secret not in json.dumps(result, ensure_ascii=False)


def test_known_secret_excluded_even_without_a_recognizable_pattern():
    result = build_capture_state([{"id": "m", "role": "user", "content": "safe topic opaquevalue"}],
                                 known_secrets=("opaquevalue",))
    assert result is None


def test_role_filter_context_budget_and_contiguous_quotations():
    result = build_capture_state([
        {"id": "tool", "role": "tool", "content": "untrusted tool content"},
        {"id": "assistant", "role": "assistant", "content": "model inference"},
        {"id": "old", "role": "user", "content": "旧" * 25},
        {"id": "new", "role": "user", "content": "新" * 75},
    ], context_messages=[{"id": "past", "role": "user", "content": "prior context"}],
       assistant_text="answer", input_chars=100)
    assert result["current_user_messages"] == [
        {"message_id": "old", "text": "旧" * 25}, {"message_id": "new", "text": "新" * 75}]
    assert result["input_truncated"]
    assert result["recent_context"] == [] and result["assistant_answer"] == ""
    assert build_capture_state([{"id": "a", "role": "assistant", "content": "fact"}]) is None


def test_oversized_user_message_never_discards_trailing_correction():
    assert build_capture_state([
        {"id": "m1", "role": "user", "content": "以后默认 CPU。" + "补充背景。" * 30 + "更正：还是用 GPU。"},
    ], input_chars=100) is None
    assert build_capture_state([
        {"id": "m1", "role": "user", "content": "以后默认 CPU。"},
        {"id": "m2", "role": "user", "content": "补充背景。" * 30 + "更正：还是用 GPU。"},
    ], input_chars=100) is None


async def test_real_jev_wire_shape_and_unknown_usage_preserved():
    calls = []

    def handle(request):
        calls.append(request)
        assert str(request.url) == JEV_ENDPOINT and request.method == "POST"
        assert request.headers["authorization"] == "Bearer offline-test-key"
        body = json.loads(request.content)
        assert set(body) == {"model", "state", "questions"}
        assert body["state"]["current_user_messages"][0]["text"] == "以后默认中文回答。"
        question = body["questions"]["remember"]
        assert question["type"] == "noul" and "context only" in question["instructions"]
        assert "explicit request not to remember" in question["instructions"]
        assert "quoted third-party instruction" in question["instructions"]
        assert "explicitly asked not to remember" in question["criteria"]["false"]
        assert set(question["criteria"]) == {"true", "false"}
        return httpx.Response(200, json=response(usage={}))

    result = await gate(handle).evaluate(state(), config())
    assert result == {"probability": 0.92, "model": "jev-1.14.0",
                      "usage": {"input_tokens": None, "output_tokens": None}}
    assert len(calls) == 1


@pytest.mark.parametrize("probability", [True, -0.1, 1.01, "0.9", None])
async def test_invalid_probability_is_not_treated_as_yes(probability):
    with pytest.raises(ProviderError) as error:
        await gate(lambda _: httpx.Response(200, json=response(probability))).evaluate(state(), config())
    assert error.value.category == "protocol"


@pytest.mark.parametrize("raw", [
    b'{"answers":{"remember":{"type":"noul","noul":NaN}},"model":"jev-1.14.0"}',
    b'{"answers":{"remember":{"type":"noul","noul":0.9,"noul":0.1}},"model":"jev-1.14.0"}',
    b'{"answers":{"remember":{"type":"choice","noul":0.9}},"model":"jev-1.14.0"}',
    b'{"answers":{"remember":{"type":"noul","noul":0.9}},"model":"jev-1.14.0","usage":{"input_tokens":true}}',
    b'not json; secret provider error body',
])
async def test_malformed_wire_response_is_safe_error(raw):
    with pytest.raises(ProviderError) as error:
        await gate(lambda _: httpx.Response(200, content=raw)).evaluate(state(), config())
    assert error.value.category == "protocol"
    assert "secret provider" not in str(error.value)


@pytest.mark.parametrize("status", [301, 401, 429, 503])
async def test_non_success_does_not_follow_redirect_retry_or_echo_body(status):
    calls = []

    def handle(request):
        calls.append(str(request.url))
        return httpx.Response(status, headers={"Location": "https://foreign.example/"},
                              text="private response carrying offline-test-key")

    with pytest.raises(ProviderError) as error:
        await gate(handle).evaluate(state(), config())
    assert error.value.status_code == status
    assert calls == [JEV_ENDPOINT]
    assert "offline-test-key" not in str(error.value)


async def test_transport_error_has_no_raw_headers_or_request_replay():
    calls = []

    def handle(request):
        calls.append(request)
        raise httpx.ReadError("raw sensitive diagnostic", request=request)

    with pytest.raises(ProviderError) as error:
        await gate(handle).evaluate(state(), config())
    assert error.value.category == "transport" and len(calls) == 1
    assert "raw sensitive" not in str(error.value)


async def test_no_network_when_disabled_cancelled_or_secret_is_present():
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=response())

    service = gate(handle)
    with pytest.raises(ProviderError, match="disabled"):
        await service.evaluate(state(), DEFAULTS)
    cancel = asyncio.Event()
    cancel.set()
    with pytest.raises(asyncio.CancelledError):
        await service.evaluate(state(), config(), cancel)
    with pytest.raises(ProviderError) as error:
        await service.evaluate({"raw": "offline-test-key"}, config())
    assert error.value.category == "sensitive_input"
    assert calls == []


async def test_inflight_cancellation_releases_http_work():
    started, released, cancel = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def handle(_):
        started.set()
        try:
            await asyncio.Future()
        finally:
            released.set()

    task = asyncio.create_task(gate(handle).evaluate(state(), config(), cancel))
    await asyncio.wait_for(started.wait(), 1)
    cancel.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert released.is_set()


async def test_total_timeout_releases_http_work():
    released = asyncio.Event()

    async def handle(_):
        try:
            await asyncio.Future()
        finally:
            released.set()

    with pytest.raises(ProviderError) as error:
        await gate(handle).evaluate(state(), config(timeout_seconds=1))
    assert error.value.category == "timeout" and released.is_set()


async def test_receive_budget_rejects_oversized_reply():
    with pytest.raises(ProviderError) as error:
        await gate(lambda _: httpx.Response(200, content=b"x" * (256 * 1024 + 1))).evaluate(state(), config())
    assert error.value.category == "budget"


async def test_hand_built_states_cannot_add_tool_payloads_or_exceed_text_budget():
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=response())

    for invalid in ({**state(), "tool_result": "do not send"},
                    {**state(), "recent_context": [{"role": "tool", "text": "do not send"}]},
                    {**state(), "assistant_answer": "x" * 12001}):
        with pytest.raises(ProviderError) as error:
            await gate(handle).evaluate(invalid, config())
        assert error.value.category == "validation"
    assert calls == []


async def test_response_metadata_cannot_echo_resolved_key():
    with pytest.raises(ProviderError) as error:
        await gate(lambda _: httpx.Response(200, json=response(model="offline-test-key"))).evaluate(state(), config())
    assert error.value.category == "protocol" and "offline-test-key" not in str(error.value)


def test_candidates_require_current_human_quote_and_deduplicate():
    sources = {"m1": "以后默认中文回答。"}
    result = parse_candidates(extraction(fact(), fact()), sources=sources)
    assert result == [fact()]
    assert parse_candidates("```json\n" + extraction(fact()) + "\n```", sources=sources) == [fact()]
    assert parse_candidates(extraction(), sources=sources) == []


@pytest.mark.parametrize("candidate", [
    fact(message_id="old-user"), fact(message_id="assistant"), fact(quote="用户喜欢英文"),
    fact(scope="global"), fact(entity=""), fact(relation="x" * 301),
    fact(value="sk-this-is-a-fake-test-key-12345"), fact(value=float("nan")),
    fact(quote=""),
])
def test_candidates_cannot_supply_own_scope_or_unseen_evidence(candidate):
    with pytest.raises(ProviderError) as error:
        parse_candidates(extraction(candidate), sources={"m1": "以后默认中文回答。"})
    assert error.value.category == "protocol" and "sk-this" not in str(error.value)


def test_candidate_limits_and_sensitive_source_are_fail_closed():
    with pytest.raises(ProviderError):
        parse_candidates(extraction(fact(), fact()), sources={"m1": "以后默认中文回答。"}, max_facts=1)
    with pytest.raises(ProviderError):
        parse_candidates(extraction(fact()), sources={"m1": "以后默认中文回答。密码是abc"})
    with pytest.raises(ProviderError):
        parse_candidates('{"facts": [], "facts": []}', sources={})
