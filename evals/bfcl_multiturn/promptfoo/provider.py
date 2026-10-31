"""promptfoo provider for a complete, already-recorded Aster Multi-Turn task.

This first adapter deliberately has no live mode.  Aster owns generation,
cross-turn state, tool execution and budgets; promptfoo consumes its frozen
result rather than recreating an agent loop or making a model request.
"""
from __future__ import annotations

import json
from pathlib import Path

from aster.evaluation.bfcl_multiturn_export import load_bundle


def call_api(prompt, options, context):
    config = options.get("config", {})
    bundle_dir = config.get("bundle_dir")
    if not isinstance(bundle_dir, str) or not bundle_dir:
        raise ValueError("An explicit saved-result bundle_dir is required")
    task_id = context.get("vars", {}).get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("One complete task_id is required per promptfoo test")
    rows = load_bundle(Path(bundle_dir))
    expected_label = config.get("config_label")
    matches = [row for row in rows if row["task_id"] == task_id
               and (expected_label is None or row["config_label"] == expected_label)]
    if len(matches) != 1:
        raise ValueError("Saved bundle must contain exactly one matching complete task")
    row = matches[0]
    if row["replay"]["new_model_api_calls"] != 0 or row["replay"]["new_judge_api_calls"] != 0:
        raise ValueError("Only saved-result offline records are admitted")
    if expected_label is not None and row["config_label"] != expected_label:
        raise ValueError("Configuration label does not match the saved record")
    return {
        "output": json.dumps(row, ensure_ascii=False, allow_nan=False),
        "tokenUsage": {"total": 0, "prompt": 0, "completion": 0},
        "metadata": {
            "mode": "saved_result_offline",
            "task_id": row["task_id"],
            "subcategory": row["subcategory"],
            "config_label": row["config_label"],
            "recorded_usage": row["recorded_usage"],
            "recorded_outcome": row["outcome"],
            "new_model_api_calls": 0,
            "new_judge_api_calls": 0,
            "model_capability_measurement": False,
        },
    }
