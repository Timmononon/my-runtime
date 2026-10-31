"""Route whole promptfoo tasks to independent, unchanged Aster campaigns."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location("aster_isolated_existing_provider", HERE / "no_thinking_provider.py")
_provider = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_provider)
FATAL_KINDS = {"execution_infrastructure_error", "provider_infrastructure_error", "unknown_provider_request_outcome"}


class BatchDispatchError(ValueError):
    """Safe fixed error codes only; never provider exception prose."""


def digest(payload):
    return hashlib.sha256(payload).hexdigest()


def child_relative(task_id):
    return "children/" + digest(task_id.encode())[:24]


def write_new(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")


def read_control(batch, expected_digest):
    payload = (batch / "batch-control.json").read_bytes()
    control = json.loads(payload)
    if digest(payload) != expected_digest:
        raise BatchDispatchError("batch_control_identity_changed")
    for name, key in (("campaign-plan.json", "plan_sha256"), ("source-manifest.json", "source_manifest_sha256")):
        if digest((batch / name).read_bytes()) != control[key]:
            raise BatchDispatchError("batch_source_or_plan_identity_changed")
    if len(control["routes"]) != 5 or len(set(control["task_ids"])) != 5 or set(control["routes"]) != set(control["task_ids"]):
        raise BatchDispatchError("invalid_batch_task_mapping")
    return control


def route_child(batch, control, task_id):
    if not isinstance(task_id, str) or task_id not in control["routes"]:
        raise BatchDispatchError("task_outside_frozen_batch")
    route = control["routes"][task_id]
    relative = route["relative_path"]
    child = (batch / relative).resolve()
    if relative != child_relative(task_id) or not child.is_relative_to((batch / "children").resolve()):
        raise BatchDispatchError("child_route_outside_frozen_scope")
    current = json.loads((child / "execution-control.json").read_bytes())
    if (current["case_ids"] != [task_id] or current["dataset_binding"] != route["dataset_binding"]
            or current["profile_guard"] != control["profile_guard"]
            or current["profile"]["model"] != "deepseek-flash" or current["profile"]["reasoning_effort"] != "none"
            or current["source-manifest.json_sha256"] != route["source_manifest_sha256"]
            or current["campaign-plan.json_sha256"] != route["plan_sha256"]
            or digest((child / "campaign-plan.json").read_bytes()) != route["plan_sha256"]
            or digest((child / "source-manifest.json").read_bytes()) != route["source_manifest_sha256"]):
        raise BatchDispatchError("child_campaign_identity_changed")
    return child, current


def _halt_unlocked(batch, task_id, kind):
    target = batch / "batch-halt.json"
    if not target.exists():
        write_new(target, {"task_id": task_id, "reason": kind, "halted_at": time.time(),
                           "automatic_replay": False, "policy": "Block new callbacks; already admitted tasks finish normally"})


def halt_batch(batch, task_id, kind):
    from aster.platform.filesystem import FileLock

    with FileLock(batch / "batch-admission.lock"):
        _halt_unlocked(batch, task_id, kind)


def claim_task(batch, expected_digest, task_id):
    """One brief shared admission section, never held across model execution."""
    from aster.platform.filesystem import FileLock

    with FileLock(batch / "batch-admission.lock"):
        control = read_control(batch, expected_digest)
        child, _ = route_child(batch, control, task_id)
        key = digest(task_id.encode())[:24]
        intent = batch / "callback-intents" / (key + ".json")
        if intent.exists():
            raise BatchDispatchError("duplicate_callback_refused")
        # A fatal child must stop subsequent admissions even if its worker has
        # not yet written the batch halt marker. In-flight request ledger rows
        # are deliberately not mistaken for terminal unknown outcomes.
        for known_id in control["task_ids"]:
            _, state = route_child(batch, control, known_id)
            if state.get("fatal"):
                fatal = state["fatal"] if state["fatal"] in FATAL_KINDS else "unclassified_child_fatal"
                _halt_unlocked(batch, known_id, fatal)
                break
        started = {"task_id": task_id, "pid": os.getpid(), "started_at": time.time(),
                   "started_monotonic": time.monotonic(), "child_relative_path": child_relative(task_id),
                   "automatic_replay": False,
                   "admission_status": "batch_halted" if (batch / "batch-halt.json").exists() else "admitted"}
        write_new(intent, started)
        return child, started


def _http_attempts(child, task_id):
    try:
        return sum(call["case_id"] == task_id for call in json.loads((child / "results/http-budget.json").read_bytes())["calls"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def error_response(task_id, kind, error_type):
    compact = {"id": task_id, "mode": "aster_promptfoo_task_isolated_dispatch_error",
               "result": "probe_dispatch_error", "main_score": None,
               "probe_error_kind": kind, "error_type": error_type,
               "model": "deepseek-flash", "reasoning_effort": "none", "api_count_limit": None,
               "judge_api_calls": 0}
    return {"output": json.dumps(compact, sort_keys=True),
            "error": "PROBE " + kind + "; automatic replay disabled",
            "metadata": {"task_id": task_id, "probe_error_kind": kind, "error_type": error_type, "main_score": None}}


def call_api(prompt, options, context):
    config = options["config"]
    batch = Path(config["batch_dir"]).resolve()
    task_id = context.get("vars", {}).get("task_id")
    child = started = None
    kind = error_type = None
    try:
        child, started = claim_task(batch, config["batch_control_sha256"], task_id)
        key = digest(task_id.encode())[:24]
        telemetry = batch / "promptfoo/dispatches"
        write_new(telemetry / (key + ".started.json"), started)
        if started["admission_status"] == "batch_halted":
            raise BatchDispatchError("batch_halted_before_dispatch")
        # Each callback delegates its complete multi-turn task unchanged. No
        # process-global cwd/env or per-task tool state is shared or modified.
        response = _provider.call_api(prompt, {**options, "config": {**config, "campaign_dir": str(child)}}, context)
        state = json.loads((child / "execution-control.json").read_bytes())
        if state.get("fatal"):
            fatal = state["fatal"] if state["fatal"] in FATAL_KINDS else "unclassified_child_fatal"
            halt_batch(batch, task_id, fatal)
    except Exception as error:  # noqa: BLE001 - only safe codes and exception types are persisted.
        kind = str(error) if isinstance(error, BatchDispatchError) else "provider_callback_exception"
        error_type = type(error).__name__
        if started is not None and kind != "batch_halted_before_dispatch":
            halt_batch(batch, task_id, "provider_callback_exception")
        response = error_response(task_id, kind, error_type)
    if started is not None:
        finished = {**started, "finished_at": time.time(), "finished_monotonic": time.monotonic(),
            "outcome": "provider_exception" if kind else "backend_returned",
            "probe_error_kind": kind, "error_type": error_type,
            "http_attempts_for_task": _http_attempts(child, task_id)}
        write_new(batch / "promptfoo/dispatches" / (digest(task_id.encode())[:24] + ".finished.json"), finished)
    return response
