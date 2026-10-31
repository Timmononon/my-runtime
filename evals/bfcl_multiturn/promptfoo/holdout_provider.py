"""One complete held-out task in a source-bound, task-locked frozen runtime."""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location("holdout_existing_provider", HERE / "concurrent_provider.py")
_existing = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_existing)
_SPEC = importlib.util.spec_from_file_location("holdout_import_identity", HERE / "holdout_identity.py")
_identity = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_identity)
BACKEND_MODE = _existing.BACKEND_MODE


def call_api(prompt, options, context):
    campaign = Path(options["config"]["campaign_dir"]).resolve()
    configuration = options["config"].get("configuration_label")
    task_id = context.get("vars", {}).get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise ValueError("An explicit complete task ID is required")
    import sitecustomize

    if not hasattr(sitecustomize, "authorized_backend"):
        raise RuntimeError("Use the guarded frozen holdout launcher")
    actual, identity_hash = _identity.verify_callback_identity(campaign, configuration)
    from aster.evaluation.bfcl_multiturn_concurrent import run_one_current_multiturn

    telemetry = campaign / "promptfoo/dispatches"
    key = hashlib.sha256(task_id.encode()).hexdigest()[:24]
    started = {"task_id": task_id, "pid": os.getpid(), "started_at": time.time(),
        "started_monotonic": time.monotonic(), "backend_mode": BACKEND_MODE,
        "campaign_dir": str(campaign), "configuration_label": configuration,
        "runtime_import_identity_sha256": identity_hash, "runtime_import_identity": actual,
        "automatic_replay": False}
    _existing._write_new(telemetry / (key + ".started.json"), started)
    row, error_type = None, None
    try:
        with sitecustomize.authorized_backend():
            row = asyncio.run(run_one_current_multiturn(task_id, campaign))
        if row.get("id") != task_id or row.get("backend_mode") != BACKEND_MODE:
            raise ValueError("Frozen holdout backend result identity differs")
        result = _existing.render_response(row)
        compact = json.loads(result["output"])
        compact.update(configuration_label=configuration,
                       runtime_import_identity_sha256=identity_hash, evaluation_split="holdout")
        result["output"] = json.dumps(compact, ensure_ascii=False, allow_nan=False)
        result["metadata"].update(configuration_label=configuration,
                                  runtime_import_identity_sha256=identity_hash, evaluation_split="holdout")
        return result
    except BaseException as error:
        error_type = type(error).__name__
        if not isinstance(error, Exception):
            raise
        compact = {"id": task_id, "result": "concurrent_dispatch_error", "main_score": None,
            "backend_mode": BACKEND_MODE, "error_type": error_type, "judge_api_calls": 0,
            "configuration_label": configuration, "runtime_import_identity_sha256": identity_hash,
            "model": "deepseek-flash", "reasoning_effort": "none", "api_count_limit": None}
        return {"output": json.dumps(compact, sort_keys=True),
                "error": "FROZEN_HOLDOUT dispatch_error; automatic replay disabled",
                "metadata": {"task_id": task_id, "error_type": error_type, "main_score": None}}
    finally:
        _existing._write_new(telemetry / (key + ".finished.json"), {**started,
            "finished_at": time.time(), "finished_monotonic": time.monotonic(),
            "outcome": "provider_exception" if error_type else "backend_returned", "error_type": error_type,
            "http_attempts_for_task": row.get("http_attempts") if row is not None else None})
