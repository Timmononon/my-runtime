"""Concurrent complete BFCL tasks in one campaign, with task-scoped locks.

Use this module's initialize/run/read/finalize functions together. The legacy
serial backend is deliberately unchanged. HTTP transport, immutable source
checks, Runtime isolation and official scorers are reused without modification.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from time import monotonic as _io_clock
from time import sleep as _io_pause

from aster.evaluation import bfcl_multiturn_unmetered as serial
from aster.evaluation.bfcl_v4_live import _CurrentModelDriver, _LiveProtocolBudget, _status
from aster.evaluation.bfcl_v4_live import _write as _raw_write
from aster.evaluation.bfcl_v4_runner import OfflineLimits, _run_case_protocol
from aster.platform.filesystem import FileLock
from aster.providers.deepseek import ProviderError

MODE = "same_campaign_task_locks_v1"
LOCK_TIMEOUT = 300
PERFORMANCE_POLICY = {
    "version": "task_locked_campaign_infrastructure_v3",
    "batch_lock_wait_seconds": 300,
    "ledger_lock_wait_seconds": 300,
    "immutable_validation": "outside_shared_batch_lock",
    "aggregate_report": "finalize_only",
    "retained_case_scope": "selected_task_only",
    "completed_receipt_validation": "on_discovery_explicit_read_and_finalize",
    "windows_atomic_replace_retry": {
        "maximum_wait_seconds": 5,
        "initial_delay_ms": 5,
        "maximum_delay_ms": 50,
        "winerrors": [5, 32, 33],
        "scope": "known_failed_same_directory_atomic_tmp_replace_only",
        "trace": "results/io-retries/<pid>.jsonl",
        "http_or_task_replay": False,
    },
}


def _known_replace_collision(error, target):
    """A failed local replace is retryable only with both typed file names."""
    if (os.name != "nt" or not isinstance(error, PermissionError)
            or getattr(error, "winerror", None) not in {5, 32, 33}
            or not error.filename or not error.filename2):
        return False
    try:
        origin, destination, target = (Path(value).resolve() for value in
            (error.filename, error.filename2, target))
    except (OSError, TypeError, ValueError):
        return False
    return (destination == target and origin.parent == target.parent
        and origin.name.startswith("." + target.name + ".") and origin.name.endswith(".tmp"))


def _replace_trace(target, error, attempts, elapsed, event):
    """Best-effort, fixed-field diagnostics; never persist exception prose."""
    try:
        target = Path(target).resolve()
        campaign = next((parent for parent in (target.parent, *target.parents)
            if (parent / "execution-control.json").is_file() and (parent / "results").is_dir()), None)
        if campaign is None:
            return
        directory = campaign / "results/io-retries"
        directory.mkdir(parents=True, exist_ok=True)
        record = {"event": event, "pid": os.getpid(), "errno": error.errno,
            "winerror": getattr(error, "winerror", None), "target_basename": target.name,
            "retry_count": attempts, "elapsed_seconds": round(elapsed, 6)}
        with (directory / (str(os.getpid()) + ".jsonl")).open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")
    except (OSError, StopIteration, TypeError, ValueError):
        pass  # Diagnostics must not change the outcome of the durable write.


def _write(path, value):
    """Retry only a known-uncompleted Windows local atomic replacement."""
    started, attempts, last_error = _io_clock(), 0, None
    while True:
        if attempts and _io_clock() - started >= 5:
            _replace_trace(path, last_error, attempts, _io_clock() - started, "atomic_replace_retry_exhausted")
            raise last_error
        try:
            _raw_write(path, value)
        except PermissionError as error:
            if not _known_replace_collision(error, path):
                raise
            attempts += 1
            last_error = error
            elapsed = _io_clock() - started
            if elapsed >= 5:
                _replace_trace(path, error, attempts, elapsed, "atomic_replace_retry_exhausted")
                raise
            if attempts == 1:
                _replace_trace(path, error, attempts, elapsed, "atomic_replace_retry_started")
            _io_pause(min(.005 * 2 ** min(attempts - 1, 4), .05, 5 - elapsed))
        else:
            if attempts:
                _replace_trace(path, last_error, attempts, _io_clock() - started, "atomic_replace_retry_succeeded")
            return


def _batch_lock(root):
    return FileLock(root / "campaign.lock", timeout=LOCK_TIMEOUT)


def _task_lock(root, case_id):
    return FileLock(root / "results/task-locks" / (serial._suffix(case_id) + ".lock"), timeout=0)


def _intent_path(root, case_id):
    return root / "results/intents" / (serial._suffix(case_id) + ".json")


def _policy_binding(control, maximum):
    identity = {"dataset_binding": control["dataset_binding"], "mode": MODE,
        "maximum": maximum, "performance_policy": control.get("concurrency_performance_policy")}
    if control.get("agent_policy") is not None:
        identity["agent_policy"] = control["agent_policy"]
    if control.get("agent_policy_identity") is not None:
        identity["agent_policy_identity"] = control["agent_policy_identity"]
    return serial._digest(identity)


def _validate_mode(control):
    mode = control.get("concurrent_tasks", {})
    maximum = mode.get("maximum")
    if (mode.get("mode") != MODE or type(maximum) is not int or maximum < 1
            or control.get("concurrency_performance_policy") != PERFORMANCE_POLICY
            or type(mode.get("finalized")) is not bool
            or set(mode.get("states", {})) != set(control["case_ids"])
            or any(state not in {"pending", "running", "committing", "interrupted", "completed"}
                   for state in mode["states"].values())
            or mode.get("policy_binding") != _policy_binding(control, maximum)):
        raise ValueError("Concurrent campaign policy differs from admission")


def _immutable_control(control):
    """Compare every admitted field except the explicitly mutable task state."""
    result = {key: value for key, value in control.items() if key not in {"fatal", "concurrent_tasks"}}
    if "concurrent_tasks" in control:
        result["concurrent_tasks"] = {key: value for key, value in control["concurrent_tasks"].items()
            if key not in {"states", "finalized", "halt_events"}}
    return result


def _fresh_control(root, verified, *, check_mode=True):
    """Inside the batch lock, use current state after checking its static bind."""
    latest = serial._read(root / "execution-control.json")
    if _immutable_control(latest) != _immutable_control(verified):
        raise ValueError("Concurrent immutable control changed after validation")
    if check_mode:
        _validate_mode(latest)
    return latest


def _verified(root):
    # Preserve the full original cases/source/plan verification. It is expensive
    # for 200 complete cases, so callers perform it before the shared lock.
    control, cases = serial._verified_control(root)
    _validate_mode(control)
    return control, cases


def _receipt(root, control, case_id):
    receipt = serial._read(serial._receipt_path(root, case_id))
    if (receipt.get("dataset_binding") != control["dataset_binding"]
            or receipt.get("result", {}).get("id") != case_id):
        raise ValueError("Concurrent task receipt identity differs")
    serial._validate_prompt_result(control, receipt["result"])
    return receipt["result"]


def _halt(control, case_id, reason):
    # Never clear a stop discovered by another worker using a stale snapshot.
    control["fatal"] = control.get("fatal") or reason
    event = {"case_id": case_id, "reason": reason}
    events = control["concurrent_tasks"]["halt_events"]
    if event not in events:
        events.append(event)


def _reconcile(root, control, *, owned_case=None):
    """A released task lock plus an intent without a receipt is an orphan."""
    changed = False
    states = control["concurrent_tasks"]["states"]
    for case_id in control["case_ids"]:
        if serial._receipt_path(root, case_id).exists():
            if states[case_id] != "completed":
                # A newly discovered receipt (including a crash between its
                # write and control update) still needs identity validation.
                _receipt(root, control, case_id)
                states[case_id], changed = "completed", True
            continue
        if not _intent_path(root, case_id).exists():
            continue
        if case_id != owned_case:
            lock = _task_lock(root, case_id)
            try:
                lock.acquire()
            except TimeoutError:
                continue  # The request may legitimately be in flight.
            else:
                lock.release()
        _halt(control, case_id, "unknown_prior_task_outcome")
        states[case_id], changed = "interrupted", True
    if changed:
        _write(root / "execution-control.json", control)


def _merge_report(root, control):
    report = serial._merge_report(root, control)
    report["concurrent_tasks"] = control["concurrent_tasks"]
    report["fatal"] = control.get("fatal")
    report["concurrency_performance_policy"] = control["concurrency_performance_policy"]
    _write(root / "results/results.json", report)
    return report


def initialize_campaign(cases, campaign_dir, *, maximum_concurrency=5, **kwargs):
    """Explicitly admit this mode; do not upgrade or resume a serial campaign."""
    if type(maximum_concurrency) is not int or maximum_concurrency < 1:
        raise ValueError("A positive integer concurrency limit is required")
    root = Path(campaign_dir).resolve()
    if not kwargs.get("_test_only", False):
        plan = serial._read(root / "campaign-plan.json")
        if (plan.get("backend_mode") != MODE
                or plan.get("maximum_concurrency") != maximum_concurrency
                or (maximum_concurrency > 5 and plan.get("concurrency_performance_policy") != PERFORMANCE_POLICY)):
            raise ValueError("Paid concurrency must be pinned in the execution plan")
        workspace = Path(serial.__file__).resolve().parents[3]
        source_path = Path(__file__).resolve()
        manifest = serial._read(root / "source-manifest.json")
        source_name = source_path.relative_to(workspace).as_posix()
        if not any(entry.get("file") == source_name and entry.get("sha256") ==
                   hashlib.sha256(source_path.read_bytes()).hexdigest() for entry in manifest.get("files", [])):
            raise ValueError("The concurrent backend must be pinned in the source snapshot")
    serial.initialize_campaign(cases, root, **kwargs)
    verified, unused = serial._verified_control(root)
    del unused
    with _batch_lock(root):
        control = _fresh_control(root, verified, check_mode=False)
        control["concurrency_performance_policy"] = dict(PERFORMANCE_POLICY)
        control["concurrent_tasks"] = {
            "mode": MODE, "maximum": maximum_concurrency, "finalized": False,
            "states": dict.fromkeys(control["case_ids"], "pending"), "halt_events": [],
            "policy_binding": _policy_binding(control, maximum_concurrency),
        }
        _write(root / "execution-control.json", control)
        _merge_report(root, control)
    return control


class _TaskHTTPBudget(serial.UnmeteredMultiTurnHTTPBudget):
    """One locked global ledger, with a task-local view for all task counters."""

    def __init__(self, root, control, case_id):
        self.root, self.case_id, self.halted = root, case_id, False
        self.control_snapshot = control
        super().__init__(root / "results/http-budget.json", binding=control["dataset_binding"],
            case_step_bounds=control["case_official_step_bounds"],
            max_output_tokens=control["max_output_tokens"], wire_policy=control.get("wire_policy"))

    def lock(self):
        # Only this concurrent backend changes infrastructure waiting. The
        # original global ledger and all API/protocol limits are unchanged.
        return FileLock(self.path.with_suffix(self.path.suffix + ".lock"), timeout=LOCK_TIMEOUT)

    def snapshot(self):
        document = super().snapshot()
        document["calls"] = [row for row in document["calls"] if row["case_id"] == self.case_id]
        return document

    async def before_request(self, request, *, profile, case_id):
        if case_id != self.case_id:
            raise ProviderError("Concurrent task HTTP identity differs", category="endpoint")
        with _batch_lock(self.root):
            control = _fresh_control(self.root, self.control_snapshot)
            mode = control["concurrent_tasks"]
            if (control.get("fatal") or mode["finalized"]
                    or mode["states"].get(case_id) != "running"):
                self.halted = True
                raise ProviderError("Concurrent campaign stopped before dispatch", category="budget")
            # This inherited hook does local validation/reservation only. It
            # does not await network I/O; the batch lock is released before
            # httpx dispatches the reserved request to its transport.
            await super().before_request(request, profile=profile, case_id=case_id)


def _admit(root, case_id, profile, test_factory):
    profile_override = profile
    verified, cases = _verified(root)
    if case_id not in verified["case_ids"]:
        raise ValueError("Task is outside the immutable concurrent campaign")
    # Do not retain all 200 task matrices for the whole network execution.
    case = next(item for item in cases if item["id"] == case_id)
    del cases
    profile = profile or serial.capture_current_profile(verified["source_runtime"],
        provider_id=verified["profile"]["provider_id"], model=verified["profile"]["model"],
        reasoning_effort=verified["profile"]["reasoning_effort"])
    if serial._profile_guard(profile) != verified["profile_guard"]:
        raise ValueError("Concurrent provider identity changed")
    profile.validate()
    with _batch_lock(root):
        control = _fresh_control(root, verified)
        if case_id not in control["case_ids"]:
            raise ValueError("Task is outside the immutable concurrent campaign")
        test_only = control["test_only"]
        if test_only != (test_factory is not None) or not test_only and profile_override is not None:
            raise ValueError("Concurrent test and paid transports cannot be interchanged")
        _reconcile(root, control, owned_case=case_id)
        if serial._receipt_path(root, case_id).exists():
            return control, None, None, _receipt(root, control, case_id)
        if _intent_path(root, case_id).exists():
            raise ValueError("A task with an unknown prior outcome cannot be replayed")
        mode = control["concurrent_tasks"]
        if mode["finalized"]:
            raise ValueError("The concurrent campaign is finalized")
        if (not control.get("fatal") and sum(state in {"running", "committing"}
                for state in mode["states"].values()) >= mode["maximum"]):
            raise ValueError("Concurrent campaign worker limit reached before execution")
        _write(_intent_path(root, case_id), {"dataset_binding": control["dataset_binding"],
            "task_id": case_id, "status": "execution_intent_no_automatic_replay", "backend_mode": MODE})
        mode["states"][case_id] = "running"
        _write(root / "execution-control.json", control)
        return control, case, profile, None


def _task_local_gateway_error(control, exception_kind, failures, calls):
    """Isolate only the explicitly admitted, known A6 gateway response.

    Every failed HTTP receipt must match a canonical client error. Unknown
    delivery, stream/parse failures and all other provider failures stay fatal.
    This candidate never clears a fatal already stored by another worker.
    """
    categories = {502: "server", 503: "overloaded", 504: "server"}
    return (control.get("wire_policy") == serial.A6_WIRE_POLICY
        and control.get("provider_failure_policy") == serial.A6_GATEWAY_FAILURE_POLICY
        and exception_kind in {None, "ProviderError"}
        and bool(failures) and bool(calls)
        and all(failure.get("type") == "ProviderError"
            and failure.get("status_code") in categories
            and failure.get("category") == categories[failure["status_code"]] for failure in failures)
        and all(call.get("status") == "response_headers_received"
            and call.get("status_code") in {200, *categories} for call in calls)
        and [call["status_code"] for call in calls if call["status_code"] != 200]
            == [failure["status_code"] for failure in failures])


async def _execute(root, case, profile, control, test_factory):
    case_id, test_only = case["id"], control["test_only"]
    bound = control["case_official_step_bounds"][case_id]
    budget = _TaskHTTPBudget(root, control, case_id)
    protocol, drivers, fatal, exception_kind = None, [], None, None
    issues = control["reference_admission_issues"][case_id]
    if issues:
        result = {"id": case_id, "status": "blocked_dataset_conflict", "reference_admission_issues": issues,
                  "model_quality": "unsupported", "model_api_calls": 0}
    elif control.get("fatal"):
        result = {"id": case_id, "status": "not_run", "reason": control["fatal"],
                  "model_quality": "unsupported", "model_api_calls": 0}
    else:
        limits = OfflineLimits(max_requests=bound, max_tool_calls=serial.CASE_TOOL_SAFETY_LIMIT,
            max_steps_per_turn=serial.OFFICIAL_MAX_STEPS_PER_TURN, deadline_seconds=serial.CASE_DEADLINE_SECONDS)
        protocol = _LiveProtocolBudget(limits, http_budget=budget,
            path=root / "results/protocol-budgets" / (serial._suffix(case_id) + ".json"), test_only=test_only)

        def factory(_environment, _protocol):
            driver = _CurrentModelDriver(profile, budget, case_id, protocol_budget=_protocol,
                max_output_tokens=control["max_output_tokens"], test_transport_factory=test_factory,
                dns_fallback=control.get("http_dns_fallback", "off"))
            drivers.append(driver)
            return driver

        try:
            result = await _run_case_protocol(case, None, root / "results/cases" / serial._suffix(case_id),
                limits=limits, shared_budget=protocol, driver_factory=factory,
                execution_mode="test_transport" if test_only else "live_current_model",
                model_quality="unsupported" if test_only else "evaluated",
                model_api_counter=lambda: 0 if test_only else len(budget.snapshot()["calls"]),
                **({"agent_policy": control["agent_policy"]} if control.get("agent_policy") is not None else {}))
        except Exception as error:  # noqa: BLE001 - never persist provider prose or credentials.
            exception_kind, fatal = type(error).__name__, "execution_infrastructure_error"
            result = {"id": case_id, "status": "case_error", "error_type": exception_kind,
                      "model_quality": "unsupported"}
        finally:
            for driver in drivers:
                await driver.aclose()
        failures = [failure for driver in drivers for failure in driver.failures]
        if (any(run.get("runtime_status") == "budget_exhausted" for run in result.get("runs", []))
                and not any(failure["category"] == "budget" for failure in failures)):
            termination = serial._termination_kind(result, protocol)
            result["termination_kind"] = termination
            failures.append({"type": "AsterRuntimeBudgetStop", "category": "budget", "status_code": None,
                "origin": "official_per_turn_steps" if termination == "official_per_turn_step_limit"
                          else "runtime_safety_limit", "termination_kind": termination})
        if failures:
            result["model_failures"] = failures
            nonbudget = [failure for failure in failures if failure["category"] != "budget"]
            if nonbudget:
                result["status"], result["model_quality"] = "case_error", "unsupported"
                calls = budget.snapshot()["calls"]
                if (exception_kind in {None, "ProviderError"}
                        and all(failure["category"] == "incomplete" for failure in nonbudget)
                        and calls and all(call.get("status_code") == 200
                            and call.get("status") == "response_headers_received" for call in calls)):
                    result["task_local_provider_incomplete"] = True
                    fatal = None  # A local candidate only; shared fatal is never cleared.
                elif _task_local_gateway_error(control, exception_kind, nonbudget, calls):
                    result["task_local_gateway_error"] = True
                    result["provider_failure_policy"] = control["provider_failure_policy"]
                    fatal = None  # The failed request is never retried or scored.
                else:
                    fatal = "provider_infrastructure_error"
    if protocol is not None:
        protocol._persist()
    calls = budget.snapshot()["calls"]
    if any(call.get("status") == "outcome_unknown" for call in calls):
        fatal = "unknown_provider_request_outcome"
    if budget.halted:
        result.update(status="case_error", model_quality="unsupported", termination_kind="campaign_halted")
    result["model_api_calls"] = 0 if test_only else len(calls)
    result["http_attempts"], result["http_status_codes"] = len(calls), [call.get("status_code") for call in calls]
    result["verification_result"] = _status(result)
    result["result"] = ("unsupported" if test_only and result["verification_result"] not in {
        "blocked_dataset_conflict", "not_run", "case_error"} else result["verification_result"])
    result.update(budget_group=case_id, case_provenance=case.get("provenance", {}), allocated_api_call_cap=None,
        authorized_unlimited_api=True, official_model_step_bound=bound, backend_mode=MODE)
    if control.get("agent_policy") is not None:
        result["agent_policy"] = control["agent_policy"]
    if control.get("agent_policy_identity") is not None:
        result["agent_policy_identity"] = control["agent_policy_identity"]
    result.setdefault("termination_kind", serial._termination_kind(result, protocol))
    return result, fatal


def _commit(root, case_id, result, fatal):
    verified, unused = _verified(root)
    del unused
    with _batch_lock(root):
        control = _fresh_control(root, verified)
        if fatal:
            _halt(control, case_id, fatal)
        mode = control["concurrent_tasks"]
        mode["states"][case_id] = "committing"
        # Persist any global stop before publishing a completed receipt.
        _write(root / "execution-control.json", control)
        _write(serial._receipt_path(root, case_id), {"dataset_binding": control["dataset_binding"],
            "result": result})
        mode["states"][case_id] = "completed"
        _write(root / "execution-control.json", control)


async def run_one_current_multiturn(case_id, campaign_dir, *, _profile=None, _test_transport_factory=None):
    """Hold only this task's lock across execution; reject duplicate writers."""
    root = Path(campaign_dir).resolve()
    admitted, unused = _verified(root)
    del unused
    if not isinstance(case_id, str) or case_id not in admitted["case_ids"]:
        raise ValueError("Task is outside the immutable concurrent campaign")
    if (admitted["test_only"] != (_test_transport_factory is not None)
            or not admitted["test_only"] and _profile is not None):
        raise ValueError("Concurrent test and paid transports cannot be interchanged")
    with _task_lock(root, case_id):
        control, case, profile, receipt = _admit(root, case_id, _profile, _test_transport_factory)
        if receipt is not None:
            return receipt
        try:
            result, fatal = await _execute(root, case, profile, control, _test_transport_factory)
            _commit(root, case_id, result, fatal)
            return result
        except BaseException:
            # Cancellation or process interruption must never turn an unknown
            # execution into a replayable pending task.
            with _batch_lock(root):
                latest = _fresh_control(root, admitted)
                _halt(latest, case_id, "interrupted_task_outcome")
                if not serial._receipt_path(root, case_id).exists():
                    latest["concurrent_tasks"]["states"][case_id] = "interrupted"
                _write(root / "execution-control.json", latest)
            raise


def read_task_receipt(campaign_dir, case_id):
    root = Path(campaign_dir).resolve()
    verified, unused = _verified(root)
    del unused
    with _batch_lock(root):
        control = _fresh_control(root, verified)
        if case_id not in control["case_ids"]:
            raise ValueError("Task is outside the concurrent campaign")
        return _receipt(root, control, case_id)


def finalize_campaign(campaign_dir):
    """Seal only after all live writers have released their task locks."""
    root = Path(campaign_dir).resolve()
    verified, unused = _verified(root)
    del unused
    with _batch_lock(root):
        control = _fresh_control(root, verified)
        # Nonblocking probes avoid inversion with task -> batch lock order.
        for case_id in control["case_ids"]:
            lock = _task_lock(root, case_id)
            try:
                lock.acquire()
            except TimeoutError:
                raise ValueError("Active tasks prevent campaign finalization") from None
            else:
                lock.release()
        _reconcile(root, control)
        # Completed receipts were not repeatedly parsed on the admission hot
        # path. Final sealing verifies every existing receipt explicitly.
        for case_id in control["case_ids"]:
            if serial._receipt_path(root, case_id).exists():
                _receipt(root, control, case_id)
        mode = control["concurrent_tasks"]
        mode["finalized"] = True
        _write(root / "execution-control.json", control)
        ledger = serial._read(root / "results/http-budget.json")["calls"]
        for case_id in control["case_ids"]:
            path = serial._receipt_path(root, case_id)
            if not path.exists():
                unknown = _intent_path(root, case_id).exists()
                result = {"id": case_id, "result": "case_error" if unknown else "not_run",
                    "status": "case_error" if unknown else "not_run", "model_quality": "unsupported",
                    "reason": "unknown_prior_task_outcome" if unknown else "task_not_invoked",
                    "http_attempts": sum(call["case_id"] == case_id for call in ledger),
                    "model_api_calls": 0 if control["test_only"] else
                        sum(call["case_id"] == case_id for call in ledger), "backend_mode": MODE}
                if control.get("agent_policy_identity") is not None:
                    result.update(agent_policy=control["agent_policy"],
                        agent_policy_identity=control["agent_policy_identity"])
                _write(path, {"dataset_binding": control["dataset_binding"], "result": result})
            mode["states"][case_id] = "completed"
        _write(root / "execution-control.json", control)
        return _merge_report(root, control)
