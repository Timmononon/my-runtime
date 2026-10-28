"""Typed, content-free performance projection over the durable event log."""
from __future__ import annotations

import math
import re
import time
from decimal import Decimal
from pathlib import Path

import yaml

from aster.providers.pricing import PricingCatalog

PHASES = {"queued", "setup", "awaiting_model", "retrying_model", "executing_tools", "awaiting_approval",
          "awaiting_input", "compacting", "done"}
KINDS = {"run.phase", "model.request", "model.first_progress", "model.finished", "model.retry",
         "tool.started", "tool.finished", "tools.batch", "context.compacted", "context.compaction_failed",
         "context.snapshot_failed", "context.learning_extraction", "context.cache_rewrite", "context.summary_fallback",
         "context.summary_backoff", "context.file_restore_failed"}
TOKEN_FIELDS = ("input_tokens", "output_tokens", "reasoning_tokens", "cache_hit_tokens", "cache_miss_tokens")


def _number(value):
    return value if type(value) in {int, float} and math.isfinite(value) and value >= 0 else None


def _token_count(value):
    return value if type(value) is int and value >= 0 else None


def _identifier(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,200}", value) else None


def _projection(payload):
    output = {}
    for key in ("model", "tool_call_id", "name"):
        if key in payload and (value := _identifier(payload[key])) is not None:
            output[key] = value
    enums = {"phase": PHASES | {"proactive", "preflight", "reactive", "manual", "force_stop", "observations"},
             "purpose": {"main", "compaction", "compaction_learning", "web_search"},
             "risk": {"read", "write", "delete", "execute", "external"},
             "outcome": {"completed", "failed", "interrupted", "cancelled", "unknown", "known", "not_executed"},
             "category": {"authentication", "rate_limit", "unavailable", "server", "transport", "timeout",
                          "context_length", "context_length_exceeded", "context_overflow", "invalid_request",
                          "endpoint", "http", "protocol", "incomplete", "cancelled", "steering", "unknown",
                          "TypeError", "ValueError", "RuntimeError", "ConnectionError", "ProviderError"}}
    for key, values in enums.items():
        if isinstance(payload.get(key), str) and payload[key] in values:
            output[key] = payload[key]
    for key in ("request_id", "duration_ms", "latency_ms", "count", "max_concurrency", "attempt", "messages",
                "input_tokens", "output_tokens", "before_tokens", "after_tokens", "estimated_tokens",
                "calibration_tokens", "consecutive_failures", "failures", "retry_after_iteration"):
        if key in payload:
            output[key] = _number(payload[key])
    for key in ("parallel", "is_error", "reactive", "saved"):
        if type(payload.get(key)) is bool:
            output[key] = payload[key]
    if isinstance(payload.get("tool_call_ids"), list):
        output["tool_call_ids"] = [item for item in payload["tool_call_ids"] if _identifier(item)]
    return output


def cost_estimate(store, run, data_dir):
    usage, budget = store.usage(run["id"]), store.run_budget(run["id"])
    model = run["config"].get("_runtime_identity", {}).get("model", "")
    path = Path(data_dir) / "prices.yaml"
    if not path.exists():
        path = Path(data_dir) / "pricing.yaml"
    try:
        pricing = PricingCatalog.from_yaml(path)
    except (ValueError, TypeError, OSError, yaml.YAMLError):
        return {"status": "unknown", "amount": None, "known_subtotal": "0", "unknown_requests": len(usage),
                "currency": None, "pricing_version": None, "requests": len(usage),
                "reason": "Local pricing configuration is invalid"}
    groups = {}
    for row in usage:
        raw = row.get("raw", {})
        actual = model
        if isinstance(raw, dict) and raw.get("purpose") == "web_search":
            actual = _identifier(raw.get("model")) if raw.get("provider_id") == "deepseek" else None
        groups.setdefault(actual or "", []).append(row)
    totals = [pricing.total(actual, rows) for actual, rows in groups.items()]
    known = sum((Decimal(total["known_subtotal"]) for total in totals), Decimal(0))
    expected = budget["model_requests"] if budget else len(usage)
    unknown = sum(total["unknown_requests"] for total in totals) + max(0, expected - len(usage))
    return {"status": "unknown" if unknown or not usage else "estimated", "amount": None if unknown or not usage else format(known, "f"),
            "known_subtotal": format(known, "f"), "unknown_requests": unknown, "currency": pricing.currency,
            "pricing_version": pricing.version, "requests": len(usage)}


def performance(store, run_id, *, data_dir=None):
    run = store.get_run(run_id)
    cursor, rows = 0, []
    while True:
        batch = store.events(run_id=run_id, after=cursor)
        rows.extend(batch)
        if len(batch) < 1000:
            break
        cursor = batch[-1]["event_id"]
    phases, timeline, current = {}, [], None
    tools = {}
    started = run["created_at"]
    for event in rows:
        kind, payload = event["type"], event["payload"]
        projected = _projection(payload)
        if kind == "run.phase" and projected.get("phase"):
            if current:
                phases[current[0]] = phases.get(current[0], 0) + max(0, event["created_at"] - current[1])
            current = (projected["phase"], event["created_at"])
        if kind in KINDS:
            item = {"event_id": event["event_id"], "type": kind, "at": event["created_at"],
                    "offset_ms": round(max(0, event["created_at"] - started) * 1000, 3), **projected}
            timeline.append(item)
            call_id = projected.get("tool_call_id")
            if kind in {"tool.started", "tool.finished"} and call_id:
                tool = tools.setdefault(call_id, {"tool_call_id": call_id, "name": projected.get("name"),
                                                 "started_at": None, "finished_at": None, "duration_ms": None,
                                                 "outcome": None, "is_error": None})
                tool["started_at" if kind == "tool.started" else "finished_at"] = event["created_at"]
                if kind == "tool.finished":
                    tool.update({key: projected.get(key) for key in ("duration_ms", "outcome", "is_error")})
    if current:
        terminal = run["status"] in {"completed", "failed", "cancelled", "timed_out", "budget_exhausted", "needs_reconciliation"}
        boundary = run["updated_at"] if terminal else time.time()
        phases[current[0]] = phases.get(current[0], 0) + max(0, boundary - current[1])
    batches = []
    for event in timeline:
        if event["type"] != "tools.batch":
            continue
        ids = event.get("tool_call_ids", [])
        children = [tools[call_id] for call_id in ids if call_id in tools]
        starts = [child["started_at"] for child in children if child["started_at"] is not None]
        ends = [child["finished_at"] for child in children if child["finished_at"] is not None]
        complete = len(children) == len(ids) and all(child["finished_at"] is not None for child in children)
        batches.append({**{key: event.get(key) for key in ("event_id", "at", "count", "parallel", "max_concurrency")},
                        "tool_call_ids": ids, "tools": children,
                        "duration_ms": round(max(0, max(ends) - min(starts)) * 1000, 3)
                        if complete and starts and ends else None})
    usage = [{key: _token_count(item.get(key)) for key in TOKEN_FIELDS} for item in store.usage(run_id)]
    budget = store.run_budget(run_id)
    expected = budget["model_requests"] if budget else len(usage)
    reported = sum(item["cache_hit_tokens"] is not None and item["cache_miss_tokens"] is not None
                   and item["input_tokens"] == item["cache_hit_tokens"] + item["cache_miss_tokens"] for item in usage)
    hit = sum(item["cache_hit_tokens"] for item in usage) if usage and reported == expected else None
    miss = sum(item["cache_miss_tokens"] for item in usage) if usage and reported == expected else None
    result = {"run_id": run_id, "status": run["status"], "phase": current[0] if current else "queued",
              "phase_seconds": {key: round(value, 4) for key, value in phases.items()},
              "budget": budget, "usage": usage,
              "trace": [item for item in timeline if item["type"] not in {"run.phase", "tool.started", "model.retry"}],
              "timeline": timeline, "batches": batches,
              "cache_summary": {"requests": expected, "reported_requests": reported,
                                "unknown_requests": max(0, expected - reported),
                                "cache_hit_tokens": hit, "cache_miss_tokens": miss,
                                "hit_ratio": hit / (hit + miss) if hit is not None and hit + miss > 0 else None}}
    if data_dir is not None:
        result["cost_estimate"] = cost_estimate(store, run, data_dir)
    return result
