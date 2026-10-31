"""One complete Aster Multi-Turn task on the explicitly selected A6 campaign."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from pathlib import Path

BACKEND_MODE = "same_campaign_task_locks_v1"
MODEL = "deepseek-v4-flash"
PROVIDER_ID = "provider-cd3755c2b87245fc9705011b4cef067d"
WIRE_POLICY = "a6_deepseek_v4_flash_none_v1"


def _write_new(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")


def render_response(row):
    """Compact projection of the durable receipt; no prompts, answers or keys."""
    count = row.get("model_api_calls", 0)
    paid = [entry for entry in row.get("usage", []) if entry.get("input_tokens") is not None]
    token_usage = {}
    if len(paid) == count and all(entry.get("output_tokens") is not None for entry in paid):
        inputs = sum(entry["input_tokens"] for entry in paid)
        outputs = sum(entry["output_tokens"] for entry in paid)
        token_usage = {"prompt": inputs, "completion": outputs, "total": inputs + outputs}
    identity = {"model": MODEL, "provider_id": PROVIDER_ID, "reasoning_effort": "none",
        "wire_policy": WIRE_POLICY, "api_count_limit": None}
    compact = {"schema_version": 1, "mode": "aster_a6_concurrent_complete_multiturn_task",
        "id": row["id"], "result": row.get("result"), "backend_mode": BACKEND_MODE,
        "main_score": float(row["result"] == "pass") if row.get("result") in {"pass", "fail"} else None,
        "protocol_result": row.get("protocol_result"),
        "official_check_valid": row.get("scoring_verification", {}).get("valid"),
        "model_api_calls": count, "judge_api_calls": 0, **identity,
        "backend_result_sha256": hashlib.sha256(json.dumps(row, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False).encode()).hexdigest()}
    response = {"output": json.dumps(compact, ensure_ascii=False, allow_nan=False), "tokenUsage": token_usage,
        "metadata": {"task_id": row["id"], "backend_mode": BACKEND_MODE, "model_api_calls": count,
            "judge_api_calls": 0, **identity}}
    if row.get("result") in {"blocked_dataset_conflict", "not_run", "case_error"}:
        response["error"] = "EXCLUDED " + row["result"] + "; authoritative BFCL score is null"
        response["metadata"]["main_score"] = None
    return response


def call_api(prompt, options, context):
    campaign = Path(options["config"]["campaign_dir"]).resolve()
    task_id = context.get("vars", {}).get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("An explicit complete task ID is required")
    import sitecustomize

    if not hasattr(sitecustomize, "authorized_backend"):
        raise RuntimeError("Use the guarded A6 concurrent launcher")
    from aster.evaluation.bfcl_multiturn_a6 import run_one_current_multiturn

    telemetry = campaign / "promptfoo/dispatches"
    key = hashlib.sha256(task_id.encode()).hexdigest()[:24]
    started = {"task_id": task_id, "pid": os.getpid(), "started_at": time.time(),
        "started_monotonic": time.monotonic(), "backend_mode": BACKEND_MODE,
        "campaign_dir": str(campaign), "automatic_replay": False}
    _write_new(telemetry / (key + ".started.json"), started)
    row, error_type = None, None
    try:
        with sitecustomize.authorized_backend():
            row = asyncio.run(run_one_current_multiturn(task_id, campaign))
        if row.get("id") != task_id or row.get("backend_mode") != BACKEND_MODE:
            raise ValueError("Concurrent A6 backend result identity differs")
        return render_response(row)
    except BaseException as error:
        error_type = type(error).__name__
        if not isinstance(error, Exception):
            raise
        compact = {"id": task_id, "result": "concurrent_dispatch_error", "main_score": None,
            "backend_mode": BACKEND_MODE, "error_type": error_type, "judge_api_calls": 0,
            "provider_id": PROVIDER_ID, "model": MODEL, "reasoning_effort": "none",
            "wire_policy": WIRE_POLICY, "api_count_limit": None}
        return {"output": json.dumps(compact, sort_keys=True),
            "error": "CONCURRENT dispatch_error; automatic replay disabled",
            "metadata": {"task_id": task_id, "error_type": error_type, "main_score": None}}
    finally:
        _write_new(telemetry / (key + ".finished.json"), {**started,
            "finished_at": time.time(), "finished_monotonic": time.monotonic(),
            "outcome": "provider_exception" if error_type else "backend_returned", "error_type": error_type,
            "http_attempts_for_task": row.get("http_attempts") if row is not None else None})
