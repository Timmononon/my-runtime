"""Deterministic assertion against the A6 campaign's official BFCL receipt."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def get_assert(output, context):
    compact = json.loads(output)
    task_id = context.get("vars", {}).get("task_id")
    if (compact.get("id") != task_id or compact.get("model") != "deepseek-v4-flash"
            or compact.get("provider_id") != "provider-cd3755c2b87245fc9705011b4cef067d"
            or compact.get("wire_policy") != "a6_deepseek_v4_flash_none_v1"
            or compact.get("backend_mode") != "same_campaign_task_locks_v1"
            or compact.get("reasoning_effort") != "none" or "api_count_limit" not in compact
            or compact["api_count_limit"] is not None):
        raise ValueError("A6 no-thinking assertion identity mismatch")
    campaign = Path(context["test"]["metadata"]["campaign_dir"]).resolve()
    from aster.evaluation.bfcl_multiturn_a6 import read_task_receipt

    row = read_task_receipt(campaign, task_id)
    digest = hashlib.sha256(json.dumps(row, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    if (digest != compact.get("backend_result_sha256") or row.get("id") != task_id
            or row.get("result") != compact.get("result")
            or row.get("model_api_calls") != compact.get("model_api_calls") or compact.get("judge_api_calls") != 0):
        raise ValueError("A6 no-thinking output differs from the durable Aster task receipt")
    if row.get("result") not in {"pass", "fail"}:
        raise ValueError("Unscorable live outcomes are excluded, not zero-scored")
    passed = row["result"] == "pass"
    if passed != (row.get("scoring_verification", {}).get("valid") is True and row.get("protocol_result") == "pass"):
        raise ValueError("Formal result disagrees with official checker/runtime outcome")
    return {"pass": passed, "score": float(passed),
            "reason": "Live BFCL official complete-task result: " + row["result"]}
