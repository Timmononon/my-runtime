"""Observe concurrent callbacks without changing the existing Aster backend."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location("aster_probe_existing_provider", HERE / "no_thinking_provider.py")
_provider = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_provider)


def classify_error(error: Exception) -> str:
    """Only recognize exact local admission errors; never emit provider prose."""
    if (isinstance(error, TimeoutError) and str(error).startswith("Lock busy: ")
            and str(error).endswith("campaign.lock")):
        return "backend_campaign_lock_busy"
    if isinstance(error, ValueError) and str(error) == "Only the next serial promptfoo task may begin":
        return "backend_serial_order_rejected"
    return "unclassified_provider_exception"


def _write_new(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")


def _http_attempts(campaign: Path, task_id: str) -> int | None:
    try:
        ledger = json.loads((campaign / "results/http-budget.json").read_bytes())
        return sum(call["case_id"] == task_id for call in ledger["calls"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def call_api(prompt, options, context):
    campaign = Path(options["config"]["campaign_dir"]).resolve()
    telemetry = Path(options["config"]["probe_telemetry_dir"]).resolve()
    task_id = context.get("vars", {}).get("task_id")
    if (not isinstance(task_id, str)
            or not re.fullmatch(r"multi_turn_(?:base|miss_func|miss_param|long_context)_[0-9]+", task_id)
            or not telemetry.is_relative_to(campaign)):
        raise ValueError("Invalid explicit probe task or telemetry scope")
    key = hashlib.sha256(task_id.encode()).hexdigest()[:24]
    started = {"task_id": task_id, "pid": os.getpid(), "started_at": time.time(),
               "started_monotonic": time.monotonic(), "automatic_replay": False}
    # This marker prevents a repeated callback from replaying a failed/unknown
    # task. It does not wait for, acquire, or bypass the backend campaign lock.
    try:
        _write_new(telemetry / (key + ".started.json"), started)
    except FileExistsError:
        return {"error": "PROBE duplicate_callback_refused; automatic replay disabled",
                "metadata": {"probe_error_kind": "duplicate_callback_refused", "task_id": task_id}}
    error_kind = None
    error_type = None
    try:
        response = _provider.call_api(prompt, options, context)
    except Exception as error:  # noqa: BLE001 - diagnostic wrapper records types, never raw exception text.
        error_kind, error_type = classify_error(error), type(error).__name__
        compact = {"id": task_id, "mode": "aster_promptfoo_concurrency_probe_error",
                   "result": "probe_dispatch_error", "main_score": None,
                   "probe_error_kind": error_kind, "error_type": error_type,
                   "model": "deepseek-flash", "reasoning_effort": "none", "api_count_limit": None,
                   "judge_api_calls": 0}
        response = {"output": json.dumps(compact, sort_keys=True),
                    "error": "PROBE " + error_kind + "; automatic replay disabled",
                    "metadata": {"task_id": task_id, "probe_error_kind": error_kind,
                                 "error_type": error_type, "main_score": None}}
    finished = {**started, "finished_at": time.time(), "finished_monotonic": time.monotonic(),
                "outcome": "provider_exception" if error_kind else "backend_returned",
                "probe_error_kind": error_kind, "error_type": error_type,
                "http_attempts_for_task": _http_attempts(campaign, task_id)}
    _write_new(telemetry / (key + ".finished.json"), finished)
    return response
