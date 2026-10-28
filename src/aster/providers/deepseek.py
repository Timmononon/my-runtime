"""DeepSeek's stateless Responses protocol, independent of the agent loop.

Reference: https://api-docs.deepseek.com/guides/responses_api/
The complete terminal response is required before any tool call is returned.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import math
import random
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx

from aster.domain import Message, ModelTurn, ToolCall, ToolSpec, Usage
from aster.providers.images import (
    MAX_SOURCE_IMAGE_BYTES,
    enforce_image_budget,
    image_data_url,
    normalize_inline_image,
)


class ProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        category: str = "provider",
        retryable: bool = False,
        status_code: int | None = None,
        retry_after: float | None = None,
        details: dict | None = None,
    ):
        super().__init__(message)
        self.category = category
        self.retryable = retryable
        self.status_code = status_code
        self.retry_after = retry_after
        self.details = details or {}


def _context_category(error) -> str | None:
    """Classify only protocol error identifiers; echoed prose is untrusted."""
    if isinstance(error, dict):
        for field in ("code", "type"):
            value = error.get(field)
            if isinstance(value, str) and value in {"context_length", "context_length_exceeded", "context_overflow"}:
                return "context_length_exceeded"
    return None


def _retry_after(value: str) -> float | None:
    try:
        seconds = float(value)
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            seconds = (date - datetime.now(UTC)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    return min(60.0, max(0.0, seconds)) if math.isfinite(seconds) else None


@dataclass(frozen=True)
class ModelCapabilities:
    model: str
    context_window: int | None
    max_output_tokens: int | None
    input_modalities: tuple[str, ...]
    effort_levels: tuple[str, ...]
    fetched_at: str
    raw: dict[str, Any]


def native_effort_levels(levels) -> tuple[str, ...]:
    """Official Responses `none` disables thinking; /models lists only enabled levels."""
    if not isinstance(levels, (list, tuple)) or any(value not in {"none", "low", "high", "max"} for value in levels):
        raise ProviderError("Invalid native reasoning capability response", category="protocol")
    return tuple(dict.fromkeys(("none", *levels)))


async def _cancellable(awaitable: Awaitable, cancel: asyncio.Event | None):
    if cancel is None:
        return await awaitable
    if cancel.is_set():
        if inspect.iscoroutine(awaitable):
            awaitable.close()
        elif isinstance(awaitable, asyncio.Future):
            awaitable.cancel()
        raise asyncio.CancelledError()
    work = asyncio.ensure_future(awaitable)
    stopped = asyncio.create_task(cancel.wait())
    try:
        done, _ = await asyncio.wait((work, stopped), return_when=asyncio.FIRST_COMPLETED)
        if stopped in done and cancel.is_set():
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
            raise asyncio.CancelledError()
        return await work
    finally:
        for task in (work, stopped):
            if not task.done():
                task.cancel()
        await asyncio.gather(work, stopped, return_exceptions=True)


def _input_item(item: dict) -> dict:
    kind = item.get("type")
    fields = {
        "reasoning": ("type", "content"),
        "message": ("type", "role", "content"),
        "function_call": ("type", "call_id", "name", "arguments"),
    }
    if kind not in fields:
        raise ProviderError(f"Unsupported historical output item: {kind}", category="history")
    return {key: copy.deepcopy(item[key]) for key in fields[kind] if key in item}


def _content_parts(message: Message, artifact_root: Path | None, artifact_resolver=None):
    # Runtime always stores content_parts, using [] for ordinary text tools.
    # An empty optional multimodal list must not erase the actual tool result.
    parts = copy.deepcopy(message.metadata.get("content_parts") or message.content)
    if not isinstance(parts, list):
        return parts
    registered = {item.get("artifact_id"): item for item in message.metadata.get("artifacts", [])}
    for part in parts:
        if part.get("type") != "input_image":
            continue
        if "path" not in part:
            try:
                part["image_url"] = normalize_inline_image(part.get("image_url"))
            except ValueError as exc:
                part.clear()
                part.update(type="input_text", text=f"[Image unavailable: {exc}. Original source remains in session history.]")
            continue
        record = registered.get(part.get("artifact_id"))
        if artifact_root is None or not record:
            raise ProviderError("Local image has no registered artifact scope", category="artifact")
        try:
            original = Path(part["path"]).resolve()
            if original != Path(record["path"]).resolve():
                raise ValueError()
            candidate = (
                artifact_resolver(artifact_id=part["artifact_id"], path=str(original))
                if artifact_resolver
                else original
            )
            path = Path(candidate).resolve(strict=True)
            if not path.is_relative_to(artifact_root.resolve()):
                raise ValueError()
            if not path.is_file():
                raise ValueError()
            if path.stat().st_size > MAX_SOURCE_IMAGE_BYTES:
                part.clear()
                part.update(type="input_text", text="[Image exceeds the 25 MiB source budget; original artifact is preserved. "
                            "Request a cropped or lower resolution image.]")
                continue
            raw = path.read_bytes()
            if record.get("sha256") and hashlib.sha256(raw).hexdigest() != record["sha256"]:
                raise ValueError()
        except (OSError, ValueError, KeyError) as exc:
            raise ProviderError(
                "Image artifact is outside its scope, missing, or changed after registration", category="artifact"
            ) from exc
        try:
            url = image_data_url(raw, part.get("mime_type"))
        except ValueError as exc:
            part.clear()
            part.update(type="input_text", text=f"[Image unavailable: {exc}. Original artifact is preserved.]")
            continue
        part.clear()
        part.update(type="input_image", image_url=url)
    return parts


def encode_messages(
    messages: list[Message], artifact_root: Path | None = None, artifact_resolver=None
) -> list[dict]:
    """Keep reasoning adjacent to its response and preserve tool/result identities."""
    encoded: list[dict] = []
    pending: set[str] = set()
    seen: set[str] = set()
    for message in messages:
        if message.role == "assistant" and message.provider_items:
            items = [_input_item(item) for item in message.provider_items]
        elif message.role == "tool":
            if not message.tool_call_id:
                raise ProviderError("Tool result has no call id", category="history")
            items = [
                {
                    "type": "function_call_output",
                    "call_id": message.tool_call_id,
                    "output": _content_parts(message, artifact_root, artifact_resolver),
                }
            ]
        elif message.role == "assistant":
            items = []
            if message.reasoning is not None:
                items.append(
                    {"type": "reasoning", "content": [{"type": "reasoning_text", "text": message.reasoning}]}
                )
            if message.content:
                items.append({"type": "message", "role": "assistant", "content": message.content})
            items.extend(
                {
                    "type": "function_call",
                    "call_id": call.id,
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                }
                for call in message.tool_calls
            )
        else:
            if message.role not in {"user", "system", "developer"}:
                raise ProviderError(f"Unsupported message role: {message.role}", category="history")
            items = [
                {
                    "type": "message",
                    "role": message.role,
                    "content": _content_parts(message, artifact_root, artifact_resolver),
                }
            ]
        for item in items:
            if item["type"] == "function_call":
                call_id = item.get("call_id")
                if not call_id or call_id in seen:
                    raise ProviderError("Missing or duplicate historical call id", category="history")
                seen.add(call_id)
                pending.add(call_id)
            elif item["type"] == "function_call_output":
                call_id = item["call_id"]
                if call_id not in pending:
                    raise ProviderError("Orphan or duplicate tool result", category="history")
                pending.remove(call_id)
        encoded.extend(items)
    if pending:
        raise ProviderError("History contains unresolved tool calls", category="history")
    return enforce_image_budget(encoded)


def _usage(raw: dict | None) -> Usage:
    raw = copy.deepcopy(raw) if isinstance(raw, dict) else {}

    def count(value):
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

    hit = count((raw.get("input_tokens_details") or {}).get("cached_tokens"))
    total = count(raw.get("input_tokens"))
    # A derived miss count is valid only when both source counters were received.
    miss = total - hit if total is not None and hit is not None and total >= hit else None
    return Usage(
        input_tokens=total,
        output_tokens=count(raw.get("output_tokens")),
        reasoning_tokens=count((raw.get("output_tokens_details") or {}).get("reasoning_tokens")),
        cache_hit_tokens=hit,
        cache_miss_tokens=miss,
        raw=raw,
    )


class DeepSeekResponsesClient:
    def __init__(
        self,
        api_key: str | Callable[[], str],
        *,
        model: str = "deepseek-flash",
        base_url: str = "https://api.deepseek.com",
        effort: str | None = "high",
        max_output_tokens: int = 8192,
        max_retries: int = 2,
        timeout_seconds: float = 300,
        idle_seconds: float = 120,
        retry_base_seconds: float = 0.5,
        client: httpx.AsyncClient | None = None,
        capability_overrides: dict[str, dict] | None = None,
        artifact_root: Path | None = None,
        artifact_resolver=None,
    ):
        if effort not in {None, "none", "low", "high", "max"}:
            raise ValueError("Unsupported reasoning effort")
        if max_output_tokens < 1 or max_retries < 0 or timeout_seconds <= 0 or idle_seconds <= 0:
            raise ValueError("Provider budgets must be positive")
        self._api_key = api_key
        self.model = model
        endpoint = httpx.URL(base_url)
        if (
            endpoint.userinfo
            or endpoint.query
            or endpoint.fragment
            or (endpoint.scheme != "https" and client is None)
        ):
            raise ValueError("Provider endpoint must use HTTPS without credentials, query or fragment")
        if client is None and (
            endpoint.host != "api.deepseek.com"
            or endpoint.path not in {"", "/"}
            or endpoint.port not in {None, 443}
        ):
            raise ValueError("Production DeepSeek credentials require the official API endpoint")
        self.base_url = base_url.rstrip("/")
        self.effort = effort
        self.max_output_tokens = max_output_tokens
        self.max_retries = max_retries
        self.timeout_seconds = timeout_seconds
        self.idle_seconds = idle_seconds
        self.retry_base_seconds = retry_base_seconds
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds, connect=20))
        self._owns_client = client is None
        self.capabilities: dict[str, ModelCapabilities] = {}
        self.capability_overrides = copy.deepcopy(capability_overrides or {})
        self.artifact_root = Path(artifact_root) if artifact_root is not None else None
        self.artifact_resolver = artifact_resolver

    def is_configured(self) -> bool:
        return bool(self._api_key() if callable(self._api_key) else self._api_key)

    async def aclose(self):
        if self._owns_client:
            await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        key = self._api_key() if callable(self._api_key) else self._api_key
        if not key:
            raise ProviderError("DeepSeek API key is not configured", category="authentication")
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    async def refresh_capabilities(
        self, *, cancel: asyncio.Event | None = None
    ) -> dict[str, ModelCapabilities]:
        response = await _cancellable(
            self._client.get(self.base_url + "/models", headers=self._headers(), follow_redirects=False),
            cancel,
        )
        if response.status_code != 200:
            raise self._http_error(response)
        try:
            rows = response.json()["data"]
            if not isinstance(rows, list):
                raise TypeError()
            found = {}
            for row in rows:
                raw = {**row, **self.capability_overrides.get(row["id"], {})}
                found[row["id"]] = ModelCapabilities(
                    model=row["id"],
                    context_window=raw.get("context_window"),
                    max_output_tokens=raw.get("max_output_tokens"),
                    input_modalities=tuple(raw.get("input_modalities", ["text"])),
                    effort_levels=native_effort_levels((raw.get("effort") or {}).get("supported_levels", [])),
                    fetched_at=datetime.now(UTC).isoformat(),
                    raw=copy.deepcopy(raw),
                )
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderError("Invalid model capability response", category="protocol") from exc
        self.capabilities = found
        return copy.deepcopy(found)

    def _http_error(self, response: httpx.Response) -> ProviderError:
        code = response.status_code
        category = {
            400: "invalid_request",
            401: "authentication",
            402: "balance",
            422: "invalid_parameters",
            429: "rate_limit",
            500: "server",
            503: "overloaded",
        }.get(code, "http")
        retry_after = _retry_after(response.headers.get("retry-after", ""))
        details = {}
        try:
            error = response.json().get("error", {})
            if code in {400, 413, 422}:
                category = _context_category(error) or category
            key = self._api_key() if callable(self._api_key) else self._api_key
            for field in ("code", "type", "param", "message"):
                value = error.get(field) if isinstance(error, dict) else None
                if not isinstance(value, str):
                    continue
                if key:
                    value = value.replace(key, "[redacted]")
                value = re.sub(r"(?i)bearer\s+[^\s,;]+", "Bearer [redacted]", value)
                value = re.sub(r"\bsk-[A-Za-z0-9_-]{6,}", "[redacted]", value)
                value = re.sub(
                    r"(?is)(authorization|api[-_ ]?key|token)\s*[:=]\s*[^\s,;]+", r"\1=[redacted]", value
                )
                value = re.sub(r"(?is)data:[^\s\"']+", "[image omitted]", value)
                value = re.sub(r"(?s)\{.*\}", "[payload omitted]", value)
                value = re.sub(r"(?is)(request\s*(body|payload)|headers)\s*[:=].*", r"\1=[omitted]", value)
                details[field] = value[: 400 if field == "message" else 80]
        except (ValueError, TypeError, AttributeError):
            pass
        # Only allowlisted, redacted diagnostics survive; never retain response/request objects.
        return ProviderError(
            f"DeepSeek returned HTTP {code} ({category})",
            category=category,
            status_code=code,
            retryable=code in {429, 500, 502, 503, 504},
            retry_after=retry_after,
            details=details,
        )

    async def generate(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        *,
        system: str,
        emit=None,
        cancel: asyncio.Event | None = None,
    ) -> ModelTurn:
        body = {
            "model": self.model,
            "instructions": system,
            "input": encode_messages(messages, self.artifact_root, self.artifact_resolver),
            "stream": True,
            "max_output_tokens": self.max_output_tokens,
            "tools": [
                {
                    "type": "function",
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": copy.deepcopy(spec.parameters),
                }
                for spec in tools
            ],
        }
        if self.effort is not None:
            body["reasoning"] = {"effort": self.effort}
        cap = self.capabilities.get(self.model)
        contains_images = self._has_image(body["input"])
        if contains_images and (
            (cap and "image" not in cap.input_modalities) or (not cap and self.model != "deepseek-flash")
        ):
            raise ProviderError(
                "Image capability has not been verified for this model", category="capability"
            )
        if cap and cap.max_output_tokens and self.max_output_tokens > cap.max_output_tokens:
            raise ProviderError("Configured output budget exceeds model capability", category="budget")
        try:
            async with asyncio.timeout(self.timeout_seconds):
                return await self._attempts(body, emit, cancel)
        except TimeoutError as exc:
            raise ProviderError("Model request exceeded its total time budget", category="timeout") from exc

    async def _attempts(self, body, emit, cancel):
        for attempt in range(self.max_retries + 1):
            progress = {"received": False}
            try:
                return await _cancellable(self._stream(body, emit, progress), cancel)
            except httpx.TransportError as exc:
                error = ProviderError("DeepSeek transport failed", category="transport",
                                      retryable=isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)))
                error.__cause__ = exc
            except ProviderError as exc:
                error = exc
            if not error.retryable or progress["received"] or attempt == self.max_retries:
                # The adapter owns its bounded retry loop. Do not multiply that
                # budget in Engine, or replay after any streamed output.
                error.retryable = False
                raise error
            delay = error.retry_after
            if delay is None:
                delay = min(30, self.retry_base_seconds * (2**attempt)) * random.uniform(0.8, 1.2)
            if emit:
                await emit("model.retry", {"attempt": attempt + 1, "category": error.category})
            await _cancellable(asyncio.sleep(delay), cancel)
        raise AssertionError("unreachable")

    @staticmethod
    def _has_image(value) -> bool:
        if isinstance(value, dict):
            return value.get("type") == "input_image" or any(
                DeepSeekResponsesClient._has_image(item) for item in value.values()
            )
        if isinstance(value, list):
            return any(DeepSeekResponsesClient._has_image(item) for item in value)
        return False

    async def _stream(self, body, emit, progress) -> ModelTurn:
        async with self._client.stream(
            "POST", self.base_url + "/responses", headers=self._headers(), json=body, follow_redirects=False
        ) as response:
            if response.status_code != 200:
                await response.aread()
                raise self._http_error(response)
            if "text/event-stream" not in response.headers.get("content-type", ""):
                raise ProviderError("Expected Responses SSE stream", category="protocol")
            lines = response.aiter_lines().__aiter__()
            data_lines: list[str] = []
            items: dict[int, dict] = {}
            last_sequence = -1
            loop = asyncio.get_running_loop()
            last_progress = loop.time()
            while True:
                remaining = self.idle_seconds - (loop.time() - last_progress)
                if remaining <= 0:
                    raise ProviderError(
                        "Model produced no effective progress before idle timeout", category="timeout"
                    )
                try:
                    line = await asyncio.wait_for(anext(lines), remaining)
                except StopAsyncIteration:
                    break
                except TimeoutError as exc:
                    raise ProviderError("Model stream idle timeout", category="timeout") from exc
                if line.startswith(":"):
                    continue
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip(" "))
                    if sum(map(len, data_lines)) > 16 * 1024 * 1024:
                        raise ProviderError("Provider event exceeds receive budget", category="budget")
                    continue
                if line or not data_lines:
                    continue
                try:
                    event = json.loads("\n".join(data_lines))
                except (ValueError, TypeError) as exc:
                    raise ProviderError("Malformed SSE event", category="protocol") from exc
                data_lines.clear()
                if not isinstance(event, dict):
                    raise ProviderError("SSE event is not an object", category="protocol")
                seq = event.get("sequence_number")
                if seq is not None:
                    if not isinstance(seq, int) or seq <= last_sequence:
                        raise ProviderError("Non-increasing stream sequence", category="protocol")
                    last_sequence = seq
                kind = event.get("type", "")
                if kind in {
                    "response.output_text.delta",
                    "response.reasoning_text.delta",
                    "response.function_call_arguments.delta",
                    "response.output_item.added",
                    "response.output_item.done",
                }:
                    progress["received"] = True
                    last_progress = loop.time()
                self._aggregate(items, event)
                if kind == "response.output_text.delta" and emit:
                    await emit("model.text.delta", {"text": event.get("delta", "")})
                if kind in {"response.completed", "response.incomplete", "response.failed"}:
                    full = event.get("response") or {}
                    if kind != "response.completed" or full.get("status", "completed") != "completed":
                        category = "incomplete" if kind == "response.incomplete" else "failed"
                        if kind == "response.failed" and not progress["received"] and not full.get("output"):
                            category = _context_category(full.get("error")) or category
                        raise ProviderError(
                            f"Model response {category}; no tools dispatched", category=category
                        )
                    output = full.get("output")
                    if output is None:
                        output = [items[index] for index in sorted(items)]
                    return self._turn(output, full.get("usage"))
            raise ProviderError("Stream ended without a terminal response", category="incomplete")

    @staticmethod
    def _aggregate(items: dict[int, dict], event: dict):
        kind = event.get("type")
        index = event.get("output_index")
        if kind in {"response.output_item.added", "response.output_item.done"}:
            if not isinstance(index, int) or not isinstance(event.get("item"), dict):
                raise ProviderError("Malformed output item", category="protocol")
            items[index] = copy.deepcopy(event["item"])
        elif kind == "response.function_call_arguments.delta":
            if index not in items:
                raise ProviderError("Tool argument delta precedes output item", category="protocol")
            items[index]["arguments"] = items[index].get("arguments", "") + event.get("delta", "")
        elif kind in {"response.output_text.delta", "response.reasoning_text.delta"}:
            if index not in items:
                raise ProviderError("Text delta precedes output item", category="protocol")
            part_index = event.get("content_index", 0)
            content = items[index].setdefault("content", [])
            while len(content) <= part_index:
                content.append(
                    {"type": "reasoning_text" if "reasoning" in kind else "output_text", "text": ""}
                )
            content[part_index]["text"] += event.get("delta", "")

    @staticmethod
    def _turn(items, raw_usage) -> ModelTurn:
        if not isinstance(items, list):
            raise ProviderError("Response output is not a list", category="protocol")
        text, calls, seen = [], [], set()
        for item in items:
            if not isinstance(item, dict) or item.get("status") in {"incomplete", "in_progress"}:
                raise ProviderError("Incomplete output item", category="incomplete")
            kind = item.get("type")
            if kind == "function_call":
                call_id, name = item.get("call_id"), item.get("name")
                if not call_id or not name or call_id in seen:
                    raise ProviderError("Missing or duplicate tool identity", category="protocol")
                try:
                    arguments = json.loads(item["arguments"])
                    if not isinstance(arguments, dict):
                        raise TypeError()
                except (ValueError, TypeError, KeyError) as exc:
                    raise ProviderError(
                        "Tool arguments are not a complete JSON object", category="invalid_tool"
                    ) from exc
                calls.append(ToolCall(call_id, name, arguments))
                seen.add(call_id)
            elif kind == "message":
                text.extend(
                    part.get("text", "")
                    for part in item.get("content", [])
                    if part.get("type") == "output_text"
                )
            elif kind != "reasoning":
                raise ProviderError(f"Unsupported model output type: {kind}", category="protocol")
        return ModelTurn(
            text="".join(text),
            tool_calls=calls,
            usage=_usage(raw_usage),
            provider_items=copy.deepcopy(items),
            finish_reason="tool_calls" if calls else "stop",
        )
