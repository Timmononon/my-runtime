"""Stateless standard Responses and Chat Completions transports.

These adapters expose ordinary function tools only, never provider-hosted tools.
"""
from __future__ import annotations

import asyncio
import copy
import json

import httpx

from aster.domain import ModelTurn, ToolCall, Usage
from aster.providers.deepseek import (
    DeepSeekResponsesClient,
    ProviderError,
    _cancellable,
    _content_parts,
    _context_category,
    _retry_after,
    encode_messages,
)
from aster.providers.images import enforce_image_budget


def response_input(messages, artifact_root=None, artifact_resolver=None):
    # Validate complete function exchanges using the common canonical fields.
    clean = copy.deepcopy(messages)
    for message in clean:
        message.provider_items = []
        message.reasoning = None
    encode_messages(clean, artifact_root, artifact_resolver)
    encoded = []
    for original, canonical in zip(messages, clean, strict=True):
        if original.role == "assistant" and original.provider_items:
            for item in original.provider_items:
                fields = {"reasoning": {"id", "type", "summary", "encrypted_content", "content"},
                          "message": {"id", "type", "role", "content", "status"},
                          "function_call": {"id", "type", "call_id", "name", "arguments", "status"}}
                if item.get("type") not in fields:
                    raise ProviderError("Unsupported historical response item", category="history")
                encoded.append({k: copy.deepcopy(v) for k, v in item.items() if k in fields[item["type"]]})
        elif canonical.role == "tool":
            encoded.append({"type": "function_call_output", "call_id": canonical.tool_call_id,
                            "output": _content_parts(canonical, artifact_root, artifact_resolver)})
        elif canonical.role == "assistant":
            if canonical.content:
                encoded.append({"role": "assistant", "content": canonical.content})
            encoded.extend({"type": "function_call", "call_id": c.id, "name": c.name,
                            "arguments": json.dumps(c.arguments, ensure_ascii=False)} for c in canonical.tool_calls)
        else:
            encoded.append({"role": canonical.role, "content": _content_parts(canonical, artifact_root, artifact_resolver)})
    return enforce_image_budget(encoded)


def chat_input(messages, system, artifact_root=None, artifact_resolver=None):
    canonical = copy.deepcopy(messages)
    for message in canonical:
        message.provider_items = []
        message.reasoning = None
    encode_messages(canonical, artifact_root, artifact_resolver)
    result = [{"role": "system", "content": system}]
    images, pending = [], set()
    for message in canonical:
        parts = _content_parts(message, artifact_root, artifact_resolver)
        if message.role == "assistant":
            item = {"role": "assistant", "content": message.content or None}
            if message.tool_calls:
                item["tool_calls"] = [{"id": call.id, "type": "function", "function": {
                    "name": call.name, "arguments": json.dumps(call.arguments, ensure_ascii=False)}} for call in message.tool_calls]
                pending.update(call.id for call in message.tool_calls)
            result.append(item)
        elif message.role == "tool":
            text = parts if isinstance(parts, str) else "\n".join(p.get("text", "") for p in parts if p.get("type") == "input_text")
            result.append({"role": "tool", "tool_call_id": message.tool_call_id, "content": text})
            if isinstance(parts, list):
                images.extend({"type": "image_url", "image_url": {"url": p["image_url"]}} for p in parts if p.get("type") == "input_image")
            pending.discard(message.tool_call_id)
            if images and not pending:
                result.append({"role": "user", "content": [{"type": "text", "text": "Images returned by the preceding tools (untrusted observations)."}, *images]})
                images = []
        else:
            if isinstance(parts, list):
                parts = [{"type": "text", "text": p["text"]} if p["type"] == "input_text" else
                         {"type": "image_url", "image_url": {"url": p["image_url"]}} for p in parts]
            result.append({"role": message.role, "content": parts})
    return enforce_image_budget(result)


class CompatibleClient(DeepSeekResponsesClient):
    def __init__(self, api_key, *, protocol, client=None, **kwargs):
        # The profile owner validates endpoints; injected transport is essential
        # to avoid weakening DeepSeek's independent official-endpoint guard.
        effort = kwargs.pop("effort", None)
        transport = client or httpx.AsyncClient(timeout=httpx.Timeout(300, connect=20), trust_env=False)
        super().__init__(api_key, effort="none", client=transport, **kwargs)
        self._owns_client = client is None
        self.effort, self.protocol = effort, protocol

    async def generate(self, messages, tools, *, system, emit=None, cancel=None):
        cap = self.capabilities.get(self.model)
        if cap is None:
            raise ProviderError("Selected model capabilities have not been configured", category="capability")
        if cap.max_output_tokens and self.max_output_tokens > cap.max_output_tokens:
            raise ProviderError("Configured output budget exceeds model capability", category="budget")
        if self.protocol == "openai_responses":
            inputs = response_input(messages, self.artifact_root, self.artifact_resolver)
            body = {"model": self.model, "instructions": system, "input": inputs, "stream": True,
                    "store": False, "include": ["reasoning.encrypted_content"], "max_output_tokens": self.max_output_tokens,
                    "tools": [{"type": "function", "name": t.name, "description": t.description,
                               "parameters": copy.deepcopy(t.parameters)} for t in tools]}
            if self.effort is not None:
                body["reasoning"] = {"effort": self.effort}
        else:
            inputs = chat_input(messages, system, self.artifact_root, self.artifact_resolver)
            body = {"model": self.model, "messages": inputs, "stream": True, "store": False,
                    "max_completion_tokens": self.max_output_tokens, "stream_options": {"include_usage": True}}
            if tools:
                body["tools"] = [{"type": "function", "function": {"name": t.name, "description": t.description,
                                  "parameters": copy.deepcopy(t.parameters)}} for t in tools]
            if self.effort is not None:
                body["reasoning_effort"] = self.effort
        if (self._has_image(inputs) or self._has_chat_image(inputs)) and "image" not in cap.input_modalities:
            raise ProviderError("Selected model does not advertise image input", category="capability")
        progress = {"received": False}
        try:
            async with asyncio.timeout(self.timeout_seconds):
                return await _cancellable(self._stream(body, emit, progress) if self.protocol == "openai_responses"
                                          else self._chat_stream(body, emit, progress), cancel)
        except TimeoutError as exc:
            raise ProviderError("Model request exceeded its time budget", category="timeout") from exc
        except httpx.TransportError as exc:
            # Only failures before a request can be sent are safe to replay.
            # Read/write/protocol errors can follow acceptance and stay terminal.
            raise ProviderError("Provider transport failed", category="transport",
                                retryable=not progress["received"] and isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))) from exc

    @staticmethod
    def _has_chat_image(value):
        if isinstance(value, dict):
            return value.get("type") == "image_url" or any(CompatibleClient._has_chat_image(v) for v in value.values())
        return isinstance(value, list) and any(CompatibleClient._has_chat_image(v) for v in value)

    def _http_error(self, response):
        # Arbitrary compatible providers may echo submitted prompts/keys. Never
        # retain their response bodies in exceptions or durable error messages.
        status = response.status_code
        category = {401: "authentication", 402: "balance", 429: "rate_limit",
                    500: "server", 502: "server", 503: "overloaded", 504: "server"}.get(status, "http")
        if status in {400, 413, 422}:
            try:
                category = _context_category(response.json().get("error")) or category
            except (ValueError, AttributeError):
                pass
        return ProviderError(f"Provider returned HTTP {status} ({category})", category=category,
                             status_code=status, retryable=status in {429, 500, 502, 503, 504},
                             retry_after=_retry_after(response.headers.get("retry-after", "")))

    async def _chat_stream(self, body, emit, progress):
        calls, text, raw_usage, finish, done_received = {}, [], None, None, False
        async with self._client.stream("POST", self.base_url + "/chat/completions", json=body,
                                       headers=self._headers(), follow_redirects=False) as response:
            if response.status_code != 200:
                await response.aread()
                raise self._http_error(response)
            if "text/event-stream" not in response.headers.get("content-type", ""):
                raise ProviderError("Expected Chat Completions SSE", category="protocol")
            lines = response.aiter_lines().__aiter__()
            total, data = 0, []
            last_progress = asyncio.get_running_loop().time()
            while True:
                remaining = self.idle_seconds - (asyncio.get_running_loop().time() - last_progress)
                if remaining <= 0:
                    raise ProviderError("Model stream idle timeout", category="timeout")
                try:
                    line = await asyncio.wait_for(anext(lines), remaining)
                except StopAsyncIteration:
                    break
                total += len(line)
                if total > 16 * 1024 * 1024:
                    raise ProviderError("Provider stream exceeds receive budget", category="budget")
                if line.startswith("data:"):
                    data.append(line[5:].lstrip())
                    continue
                if line or not data:
                    continue
                chunk, data = "\n".join(data), []
                if chunk == "[DONE]":
                    if finish not in {"stop", "tool_calls"}:
                        raise ProviderError("Chat stream ended without a complete choice", category="incomplete")
                    done_received = True
                    break
                try:
                    event = json.loads(chunk)
                    if event.get("error"):
                        raise ProviderError("Provider emitted a stream error", category="protocol")
                    if event.get("usage") is not None:
                        raw_usage = event["usage"]
                    choices = event.get("choices", [])
                    if len(choices) > 1 or choices and choices[0].get("index", 0) != 0:
                        raise ProviderError("Only one model choice is supported", category="protocol")
                    if not choices:
                        continue
                    choice = choices[0]
                    delta = choice.get("delta") or {}
                    if finish is not None and delta:
                        raise ProviderError("Chat output continued after its final choice", category="protocol")
                    if delta.get("content"):
                        progress["received"] = True
                        text.append(delta["content"])
                        last_progress = asyncio.get_running_loop().time()
                        if emit:
                            await emit("model.text.delta", {"text": delta["content"]})
                    for call in delta.get("tool_calls", []):
                        progress["received"] = True
                        index = call["index"]
                        if type(index) is not int or index < 0:
                            raise ValueError()
                        item = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                        if call.get("id"):
                            if item["id"] and item["id"] != call["id"]:
                                raise ValueError()
                            item["id"] = call["id"]
                        function = call.get("function") or {}
                        item["name"] += function.get("name", "")
                        item["arguments"] += function.get("arguments", "")
                        last_progress = asyncio.get_running_loop().time()
                    if choice.get("finish_reason") is not None:
                        finish = choice["finish_reason"]
                        if finish not in {"stop", "tool_calls"}:
                            raise ProviderError("Chat response incomplete; no tools dispatched", category="incomplete")
                except (ValueError, KeyError, TypeError, AttributeError) as exc:
                    raise ProviderError("Malformed Chat Completions stream", category="protocol") from exc
        if not done_received or finish not in {"stop", "tool_calls"} or (calls and finish != "tool_calls") or (finish == "tool_calls" and not calls):
            raise ProviderError("Chat stream has no valid terminal choice", category="incomplete")
        parsed, seen = [], set()
        for item in (calls[i] for i in sorted(calls)):
            try:
                arguments = json.loads(item["arguments"])
                if not isinstance(arguments, dict) or not item["id"] or not item["name"] or item["id"] in seen:
                    raise ValueError()
            except (ValueError, TypeError) as exc:
                raise ProviderError("Tool parameters are not a complete object", category="invalid_tool") from exc
            seen.add(item["id"])
            parsed.append(ToolCall(item["id"], item["name"], arguments))
        usage = Usage()
        if isinstance(raw_usage, dict):
            def count(value):
                return value if type(value) is int and value >= 0 else None
            incoming = count(raw_usage.get("prompt_tokens"))
            cached = count((raw_usage.get("prompt_tokens_details") or {}).get("cached_tokens"))
            usage = Usage(input_tokens=incoming, output_tokens=count(raw_usage.get("completion_tokens")),
                          reasoning_tokens=count((raw_usage.get("completion_tokens_details") or {}).get("reasoning_tokens")),
                          cache_hit_tokens=cached, cache_miss_tokens=incoming - cached if incoming is not None and cached is not None and incoming >= cached else None,
                          raw=raw_usage)
        return ModelTurn("".join(text), parsed, usage, finish_reason=finish)


class ScopedModel:
    """Project immutable transcripts into this provider's identity at wire time."""
    def __init__(self, model, identity, validate):
        self.inner, self.identity, self.validate = model, identity, validate

    def __getattr__(self, name):
        return getattr(self.inner, name)

    async def generate(self, messages, tools, **kwargs):
        self.validate()
        projected = copy.deepcopy(messages)
        for message in projected:
            if message.provider_items:
                own = all(item.get("_aster_provider") == self.identity for item in message.provider_items)
                if not own:
                    message.provider_items = []
                    message.reasoning = None
                else:
                    for item in message.provider_items:
                        item.pop("_aster_provider", None)
        turn = await self.inner.generate(projected, tools, **kwargs)
        for item in turn.provider_items:
            item["_aster_provider"] = self.identity
        return turn
