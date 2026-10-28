"""Provider-neutral contracts. Never execute data before a complete ModelTurn."""
from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

Emit = Callable[[str, dict], Awaitable[None]]


@dataclasses.dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict
    risk: str = "read"
    concurrency_safe: bool = False
    replay_safe: bool = False
    timeout_seconds: float = 60
    resource: str | None = None


@dataclasses.dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclasses.dataclass
class ToolResult:
    content: str
    is_error: bool = False
    error_category: str | None = None
    outcome: str = "known"
    artifacts: list[dict] = dataclasses.field(default_factory=list)
    data: dict = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class ToolContext:
    session_id: str
    run_id: str
    cwd: Path
    artifact_dir: Path
    cancel: asyncio.Event
    emit: Emit | None = None
    services: dict = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class Message:
    role: str
    content: str = ""
    tool_calls: list[ToolCall] = dataclasses.field(default_factory=list)
    tool_call_id: str | None = None
    reasoning: str | None = None
    provider_items: list[dict] = dataclasses.field(default_factory=list)
    metadata: dict = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    cache_hit_tokens: int | None = None
    cache_miss_tokens: int | None = None
    raw: dict = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class ModelTurn:
    text: str = ""
    tool_calls: list[ToolCall] = dataclasses.field(default_factory=list)
    usage: Usage = dataclasses.field(default_factory=Usage)
    provider_items: list[dict] = dataclasses.field(default_factory=list)
    finish_reason: str = "stop"


def to_dict(value: Any) -> Any:
    """Return explicit JSON-safe values without serializing live execution objects."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_dict(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): to_dict(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_dict(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Not a JSON-safe domain value: {type(value).__name__}")


def message_from_dict(value: dict) -> Message:
    data = dict(value)
    data["tool_calls"] = [ToolCall(**item) for item in data.get("tool_calls", [])]
    return Message(**data)


def result_from_dict(value: dict) -> ToolResult:
    return ToolResult(**value)


def tool_result_message(call_id: str, result: ToolResult, run_id: str, *, project: bool = False) -> Message:
    """One model-visible text surface, while retaining the lossless tool payload."""
    parts = copy.deepcopy(result.data.get("content_parts") or [])
    if not project:
        return Message("tool", result.content, tool_call_id=call_id,
                       metadata={"run_id": run_id, "outcome": result.outcome, "is_error": result.is_error,
                                 "error_category": result.error_category, "artifacts": copy.deepcopy(result.artifacts),
                                 "content_parts": parts, "data": copy.deepcopy(result.data)})
    texts = [result.content] if result.content else []
    for part in parts:
        text = part.get("text") if part.get("type") == "input_text" else None
        if text and not any(text in existing for existing in texts):
            texts.append(text)
    data = {key: value for key, value in result.data.items() if key != "content_parts"}
    # Tools often return the same JSON/text in both content and structured data.
    # Exclude only exact duplicates, retaining hashes, IDs and versions needed
    # for a subsequent guarded call.
    try:
        visible_json = json.loads(result.content)
    except (ValueError, TypeError):
        visible_json = None
    data = {key: value for key, value in data.items()
            if not (isinstance(value, str) and value in texts)
            and not (isinstance(visible_json, dict) and key in visible_json and visible_json[key] == value)}
    references = {"data": data} if data else {}
    if result.artifacts:
        references["artifacts"] = result.artifacts
    body = "\n".join(texts)
    if references:
        body = "[Tool metadata]\n" + json.dumps(to_dict(references), ensure_ascii=False) + "\n\n" + body
    nontext = [part for part in parts if part.get("type") != "input_text"]
    return Message("tool", body, tool_call_id=call_id,
                   metadata={"run_id": run_id, "outcome": result.outcome, "is_error": result.is_error,
                             "error_category": result.error_category, "artifacts": copy.deepcopy(result.artifacts),
                             "content_parts": [{"type": "input_text", "text": body}, *nontext] if nontext else [],
                             "data": copy.deepcopy(result.data), "source_content": result.content,
                             "tool_content_version": 1})
