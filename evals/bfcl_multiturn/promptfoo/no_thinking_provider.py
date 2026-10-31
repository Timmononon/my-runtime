"""One promptfoo invocation executes one new complete no-thinking Aster task."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path


def call_api(prompt, options, context):
    del prompt  # Frozen evaluator cases remain authoritative.
    campaign = Path(options["config"]["campaign_dir"]).resolve()
    task_id = context.get("vars", {}).get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("An explicit complete task ID is required")
    import sitecustomize

    if not hasattr(sitecustomize, "authorized_backend"):
        raise RuntimeError("Use the guarded no-thinking launcher")
    from aster.evaluation.bfcl_multiturn_unmetered import run_one_current_multiturn

    with sitecustomize.authorized_backend():
        row = asyncio.run(run_one_current_multiturn(task_id, campaign))
    usage = row.get("usage", [])
    paid = [entry for entry in usage if entry.get("input_tokens") is not None]
    count = row.get("model_api_calls", 0)
    token_usage = {}
    if len(paid) == count and all(entry.get("output_tokens") is not None for entry in paid):
        inputs = sum(entry["input_tokens"] for entry in paid)
        outputs = sum(entry["output_tokens"] for entry in paid)
        token_usage = {"prompt": inputs, "completion": outputs, "total": inputs + outputs}
    compact = {"schema_version": 1, "mode": "aster_live_complete_multiturn_task", "id": task_id,
               "result": row.get("result"), "main_score": (float(row["result"] == "pass")
                   if row.get("result") in {"pass", "fail"} else None),
               "protocol_result": row.get("protocol_result"),
               "official_check_valid": row.get("scoring_verification", {}).get("valid"),
               "model_api_calls": count, "judge_api_calls": 0,
               "model": "deepseek-flash", "reasoning_effort": "none", "api_count_limit": None,
               "backend_result_sha256": hashlib.sha256(json.dumps(row, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":"), allow_nan=False).encode()).hexdigest()}
    response = {"output": json.dumps(compact, ensure_ascii=False, allow_nan=False), "tokenUsage": token_usage,
                "metadata": {"mode": "aster_live_complete_multiturn_task", "task_id": task_id,
                             "model_api_calls": count, "judge_api_calls": 0, "model": "deepseek-flash",
                             "reasoning_effort": "none", "api_count_limit": None}}
    if row.get("result") in {"blocked_dataset_conflict", "not_run", "case_error"}:
        response["error"] = "EXCLUDED " + row["result"] + "; authoritative BFCL score is null"
        response["metadata"]["main_score"] = None
    return response
