"""Deterministic assertion over Aster's existing official BFCL result."""
from __future__ import annotations

import hashlib
import json


def get_assert(output, context):
    row = json.loads(output)
    task_id = context.get("vars", {}).get("task_id")
    if row.get("task_id") != task_id:
        raise ValueError("Assertion task identity mismatch")
    expected = context.get("test", {}).get("metadata", {}).get("record_digests", {}).get(row.get("config_label"))
    actual = hashlib.sha256(json.dumps(row, sort_keys=True, separators=(",", ":"),
                                      ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    if not expected or actual != expected:
        raise ValueError("Saved official result does not match the trusted task record digest")
    replay = row.get("replay", {})
    if (replay.get("mode") != "saved_result" or replay.get("new_model_api_calls") != 0
            or replay.get("new_judge_api_calls") != 0):
        raise ValueError("Only unchanged saved-result replay is admitted")
    if type(row["scoring"]["passed"]) is not bool:
        raise ValueError("Unscoreable task must be excluded, not converted into a model failure")
    passed = row["scoring"]["passed"]
    category = row["outcome"]["category"]
    reasons = row["outcome"].get("reasons", [])
    return {
        "pass": passed,
        "score": 1.0 if passed else 0.0,
        "reason": f"Saved BFCL result: {category}; " + "; ".join(str(item) for item in reasons),
    }
