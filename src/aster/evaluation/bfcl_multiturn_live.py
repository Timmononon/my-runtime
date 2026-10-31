"""One-shot, promptfoo-dispatched complete Multi-Turn tasks with a shared cap.

The existing Aster execution core and official subcheckers own each task. This
module only admits serial task invocations and enforces a durable campaign-wide
HTTP budget across provider processes. It never contacts a provider on import.
"""
from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from pathlib import Path

from aster.evaluation.bfcl_current import CapturedProfile, capture_current_profile
from aster.evaluation.bfcl_v4_live import (
    V4HTTPCallBudget,
    _CurrentModelDriver,
    _LiveProtocolBudget,
    _public_profile,
    _status,
    _write,
)
from aster.evaluation.bfcl_v4_runner import OfflineLimits, _run_case_protocol, reference_admission_issues
from aster.platform.filesystem import FileLock

MAX_MULTITURN_API_CALLS = 200
SUBCATEGORIES = ("multi_turn_base", "multi_turn_miss_func", "multi_turn_miss_param", "multi_turn_long_context")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _suffix(identifier):
    return hashlib.sha256(identifier.encode()).hexdigest()[:24]


def _profile_guard(profile):
    return _digest({"public": _public_profile(profile), "credential_binding": profile.credential_digest})


class MultiTurnHTTPBudget(V4HTTPCallBudget):
    """Keep V4's checked request/response hooks; permit a separate <=200 cap.

    Only campaign initialization creates the file. Later instances validate and
    open that existing ledger; they do not reset its request reservations.
    """

    def __init__(self, path, *, maximum, binding, case_ids, create=False):
        if type(maximum) is not int or not 1 <= maximum <= MAX_MULTITURN_API_CALLS:
            raise ValueError("An explicit Multi-Turn cap between 1 and 200 is required")
        self.path, self.maximum = Path(path), maximum
        self.case_groups = {identifier: identifier for identifier in case_ids}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock():
            if create:
                if self.path.exists():
                    raise ValueError("Existing campaign HTTP evidence cannot be reset")
                self.case_caps = {identifier: 20 for identifier in case_ids}
                _write(self.path, {"schema_version": 1, "maximum": maximum, "binding": binding,
                                   "case_caps": self.case_caps, "case_groups": self.case_groups, "calls": []})
            else:
                document = self.snapshot_unlocked()
                caps = document.get("case_caps", {})
                if (document.get("binding") != binding or document.get("maximum") != maximum
                        or document.get("case_groups") != self.case_groups or set(caps) != set(case_ids)
                        or any(type(value) is not int or not 0 <= value <= 20 for value in caps.values())
                        or len(document.get("calls", [])) > maximum):
                    raise ValueError("Campaign HTTP budget identity changed")
                self.case_caps = dict(caps)

    def admit_cap(self, case_id, cap):
        if case_id not in self.case_groups or type(cap) is not int or not 0 <= cap <= 20:
            raise ValueError("Invalid one-shot task allocation")
        with self.lock():
            document = self.snapshot_unlocked()
            if any(call["case_id"] == case_id for call in document["calls"]):
                raise ValueError("An executed task allocation cannot be changed")
            document["case_caps"][case_id] = cap
            self.case_caps = dict(document["case_caps"])
            _write(self.path, document)


def initialize_campaign(cases, campaign_dir, *, profile, max_api_calls=200, max_output_tokens=2048,
                        binding=None, _test_only=False):
    """Create one immutable evaluator control file, never dispatch a request."""
    if not isinstance(profile, CapturedProfile):
        raise TypeError("A read-only captured profile is required")
    if type(max_api_calls) is not int or not 1 <= max_api_calls <= MAX_MULTITURN_API_CALLS:
        raise ValueError("An explicit API limit from 1 to 200 is required")
    if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 2048:
        raise ValueError("Output tokens must be bounded by 2048")
    if (not isinstance(cases, list) or not cases
            or any(case.get("category") not in SUBCATEGORIES for case in cases)):
        raise ValueError("Only explicit complete Multi-Turn tasks are admitted")
    if type(_test_only) is not bool:
        raise ValueError("Test transport admission must be explicit")
    ids = [case["id"] for case in cases]
    if len(set(ids)) != len(ids):
        raise ValueError("Tasks must have distinct IDs")
    root = Path(campaign_dir).resolve()
    if root.is_relative_to(profile.source) or root.is_relative_to(Path.home() / ".aster"):
        raise ValueError("Evidence cannot be written into user Runtime")
    if not _test_only and any(not (root / name).is_file() for name in (
            "source-manifest.json", "campaign-plan.json")):
        raise ValueError("An approved plan and immutable source snapshot are required")
    if not _test_only:
        planned = [identifier for group in _read(root / "campaign-plan.json")["groups"]
                   for identifier in group["entry_ids"]]
        if planned != ids:
            raise ValueError("Execution tasks/order differ from the approved promptfoo plan")
    root.mkdir(parents=True, exist_ok=True)
    with FileLock(root / "campaign.lock", timeout=.1):
        if (root / "execution-control.json").exists() or (root / "results").exists():
            raise ValueError("Campaign execution cannot be initialized twice")
        profile.validate()
        issues = {case["id"]: reference_admission_issues(case) for case in cases}
        identity = {"profile": _public_profile(profile), "cases": cases, "max_api_calls": max_api_calls,
                    "max_output_tokens": max_output_tokens, "allocation": {"per_case_maximum": 20,
                    "reserved_requests_per_future_valid_case": 6}, "test_only": _test_only}
        calculated = _digest(identity)
        if binding is not None and binding != calculated:
            raise ValueError("Explicit campaign binding does not match selected execution inputs")
        control = {"schema_version": 1, "dataset_binding": calculated, "profile": _public_profile(profile),
                   "profile_guard": _profile_guard(profile), "source_runtime": str(profile.source),
                   "max_api_calls": max_api_calls, "max_output_tokens": max_output_tokens,
                   "case_ids": ids, "cases_sha256": _digest(cases), "reference_admission_issues": issues,
                   "test_only": _test_only, "per_case_maximum": 20, "future_valid_case_reserve": 6,
                   "started_at": time.time(), "fatal": None}
        for name in ("source-manifest.json", "campaign-plan.json"):
            if (root / name).is_file():
                control[name + "_sha256"] = hashlib.sha256((root / name).read_bytes()).hexdigest()
        (root / "results/receipts").mkdir(parents=True)
        (root / "results/intents").mkdir()
        _write(root / "private-cases.json", cases)
        _write(root / "execution-control.json", control)
        MultiTurnHTTPBudget(root / "results/http-budget.json", maximum=max_api_calls,
                            binding=calculated, case_ids=ids, create=True)
        _merge_report(root, control)
    return control


def _verified_control(root):
    control = _read(root / "execution-control.json")
    cases = _read(root / "private-cases.json")
    if _digest(cases) != control["cases_sha256"] or [case["id"] for case in cases] != control["case_ids"]:
        raise ValueError("Campaign task binding changed")
    for name in ("source-manifest.json", "campaign-plan.json"):
        expected = control.get(name + "_sha256")
        if expected and hashlib.sha256((root / name).read_bytes()).hexdigest() != expected:
            raise ValueError("Campaign source/plan binding changed")
    if not control["test_only"]:
        workspace = Path(__file__).resolve().parents[3]
        for entry in _read(root / "source-manifest.json")["files"]:
            relative = Path(entry["file"])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Unsafe source snapshot path")
            if hashlib.sha256((workspace / relative).read_bytes()).hexdigest() != entry["sha256"]:
                raise ValueError("Execution sources changed after campaign admission")
    return control, cases


def _receipt_path(root, identifier):
    return root / "results/receipts" / (_suffix(identifier) + ".json")


def read_task_receipt(campaign_dir, case_id):
    """Read the exact durable local outcome; never start or resume execution."""
    root = Path(campaign_dir).resolve()
    control, _cases = _verified_control(root)
    if case_id not in control["case_ids"]:
        raise ValueError("Task is outside the campaign")
    receipt = _read(_receipt_path(root, case_id))
    if (receipt.get("dataset_binding") != control["dataset_binding"]
            or receipt.get("result", {}).get("id") != case_id):
        raise ValueError("Task receipt identity differs")
    return receipt["result"]


def _merge_report(root, control):
    rows = []
    for identifier in control["case_ids"]:
        path = _receipt_path(root, identifier)
        if path.is_file():
            receipt = _read(path)
            if receipt.get("dataset_binding") != control["dataset_binding"]:
                raise ValueError("Task receipt campaign binding differs")
            rows.append(receipt["result"])
    http = _read(root / "results/http-budget.json")
    report = {"schema_version": 1, "mode": "test_transport" if control["test_only"] else "live_current_model",
              "model_quality": "unsupported" if control["test_only"] else "evaluated", "official_score": False,
              "paid_calls_authorized": not control["test_only"], "profile": control["profile"],
              "dataset_binding": control["dataset_binding"], "max_api_calls": control["max_api_calls"],
              "max_output_tokens": control["max_output_tokens"], "metadata_requests": 0,
              "planned_cases": len(control["case_ids"]), "results": rows,
              "totals": dict(Counter(row["result"] for row in rows)), "case_api_caps": http["case_caps"],
              "case_groups": http["case_groups"], "model_api_calls": 0 if control["test_only"] else len(http["calls"]),
              "http_transport_attempts": len(http["calls"]),
              "elapsed_seconds": round(time.time() - control["started_at"], 4), "full_leaderboard_score": None,
              "orchestrator": "promptfoo", "new_judge_api_calls": 0}
    _write(root / "results/results.json", report)
    return report


async def run_one_current_multiturn(case_id, campaign_dir, *, _profile=None, _test_transport_factory=None):
    """Execute exactly one admitted full task; promptfoo calls this serially.

    A known completed receipt is a read. An intent without a receipt is an
    unknown prior outcome and can never start the task again.
    """
    root = Path(campaign_dir).resolve()
    with FileLock(root / "campaign.lock", timeout=.1):
        control, cases = _verified_control(root)
        if case_id not in control["case_ids"]:
            raise ValueError("Task is outside the immutable campaign")
        test_only = control["test_only"]
        if test_only != (_test_transport_factory is not None):
            raise ValueError("Test and paid transports cannot be interchanged")
        receipt_path = _receipt_path(root, case_id)
        if receipt_path.exists():
            receipt = _read(receipt_path)
            if receipt.get("dataset_binding") != control["dataset_binding"]:
                raise ValueError("Known receipt has a different binding")
            return receipt["result"]
        expected_next = next((identifier for identifier in control["case_ids"]
                              if not _receipt_path(root, identifier).exists()), None)
        if expected_next != case_id:
            raise ValueError("Only the next serial promptfoo task may begin")
        intent_path = root / "results/intents" / (_suffix(case_id) + ".json")
        if intent_path.exists():
            raise ValueError("A task with an unknown prior outcome cannot be replayed")
        if not test_only and _profile is not None:
            raise ValueError("Paid dispatch resolves only the campaign-pinned provider")
        profile = _profile or capture_current_profile(control["source_runtime"],
            provider_id=control["profile"]["provider_id"], model=control["profile"]["model"],
            reasoning_effort=control["profile"]["reasoning_effort"])
        if _profile_guard(profile) != control["profile_guard"]:
            raise ValueError("The campaign provider/model/credential identity changed")
        profile.validate()
        case = next(item for item in cases if item["id"] == case_id)
        budget = MultiTurnHTTPBudget(root / "results/http-budget.json", maximum=control["max_api_calls"],
                                    binding=control["dataset_binding"], case_ids=control["case_ids"])
        prior_http = len(budget.snapshot()["calls"])
        issues = control["reference_admission_issues"][case_id]
        future = control["case_ids"][control["case_ids"].index(case_id) + 1:]
        future_valid = sum(not control["reference_admission_issues"][identifier] for identifier in future)
        remaining = control["max_api_calls"] - prior_http
        cap = 0 if issues else min(20, max(0, remaining - 6 * future_valid))
        budget.admit_cap(case_id, cap)
        _write(intent_path, {"dataset_binding": control["dataset_binding"], "task_id": case_id,
                            "started_at": time.time(), "api_call_cap": cap,
                            "prior_http_attempts": prior_http, "future_valid_cases": future_valid,
                            "status": "execution_intent_no_automatic_replay"})
        drivers = []
        protocol_budget = None
        if issues:
            result = {"id": case_id, "status": "blocked_dataset_conflict", "reference_admission_issues": issues,
                      "model_quality": "unsupported", "model_api_calls": 0}
        elif control.get("fatal") or cap <= 0:
            result = {"id": case_id, "status": "not_run", "reason": control.get("fatal") or "campaign_budget_exhausted",
                      "model_quality": "unsupported", "model_api_calls": 0}
        else:
            limits = OfflineLimits(max_requests=cap, max_tool_calls=120, max_steps_per_turn=20, deadline_seconds=600)
            protocol_budget = _LiveProtocolBudget(limits, http_budget=budget,
                path=root / "results/protocol-budgets" / (_suffix(case_id) + ".json"), test_only=test_only)
            execution_exception_kind = None

            def factory(_environment, _protocol):
                driver = _CurrentModelDriver(profile, budget, case_id, protocol_budget=_protocol,
                    max_output_tokens=control["max_output_tokens"], test_transport_factory=_test_transport_factory)
                drivers.append(driver)
                return driver

            try:
                result = await _run_case_protocol(case, None, root / "results/cases" / _suffix(case_id),
                    limits=limits, shared_budget=protocol_budget, driver_factory=factory,
                    execution_mode="test_transport" if test_only else "live_current_model",
                    model_quality="unsupported" if test_only else "evaluated",
                    model_api_counter=lambda: 0 if test_only else len(budget.snapshot()["calls"]))
            except Exception as error:  # noqa: BLE001 - never persist raw provider exception prose.
                execution_exception_kind = type(error).__name__
                result = {"id": case_id, "status": "case_error", "error_type": type(error).__name__,
                          "model_quality": "unsupported", "model_api_calls": 0 if test_only else
                          len(budget.snapshot()["calls"]) - prior_http}
                control["fatal"] = "execution_infrastructure_error"
            finally:
                for driver in drivers:
                    await driver.aclose()
            failures = [failure for driver in drivers for failure in driver.failures]
            # Engine admission may stop before another driver.generate call.
            # Record this real Runtime stop separately from an HTTP failure;
            # the original core transcript/checker decision remains untouched.
            if (any(run.get("runtime_status") == "budget_exhausted" for run in result.get("runs", []))
                    and not any(failure["category"] == "budget" for failure in failures)):
                failures.append({"type": "AsterRuntimeBudgetStop", "category": "budget",
                                 "status_code": None, "origin": "engine_admission"})
            if failures:
                result["model_failures"] = failures
                nonbudget = [failure for failure in failures if failure["category"] != "budget"]
                if nonbudget:
                    result["status"], result["model_quality"] = "case_error", "unsupported"
                    known_calls = [call for call in budget.snapshot()["calls"] if call["case_id"] == case_id]
                    task_local_incomplete = (
                        execution_exception_kind in {None, "ProviderError"}
                        and all(failure["category"] == "incomplete" for failure in nonbudget)
                        and bool(known_calls)
                        and all(call.get("status_code") == 200 and call.get("status") == "response_headers_received"
                                for call in known_calls)
                    )
                    if task_local_incomplete:
                        # A known HTTP 200 with incomplete model output cannot
                        # execute partial arguments or retry this task. A later,
                        # independent task may still use the remaining budget.
                        result["task_local_provider_incomplete"] = True
                        control["fatal"] = None
                    else:
                        control["fatal"] = "provider_infrastructure_error"
        if protocol_budget is not None:
            protocol_budget._persist()
        calls = [call for call in budget.snapshot()["calls"] if call["case_id"] == case_id]
        result["http_attempts"] = len(calls)
        result["http_status_codes"] = [call.get("status_code") for call in calls]
        if any(call.get("status") == "outcome_unknown" for call in calls):
            control["fatal"] = "unknown_provider_request_outcome"
        result["verification_result"] = _status(result)
        result["result"] = ("unsupported" if test_only and result["verification_result"] not in {
            "blocked_dataset_conflict", "not_run", "case_error"} else result["verification_result"])
        result["budget_group"] = case_id
        result["case_provenance"] = case.get("provenance", {})
        result["allocated_api_call_cap"] = cap
        _write(receipt_path, {"dataset_binding": control["dataset_binding"], "result": result})
        _write(root / "execution-control.json", control)
        _merge_report(root, control)
        return result


def finalize_campaign(campaign_dir):
    """Collect durable outcomes; never execute or resume an uninvoked task."""
    root = Path(campaign_dir).resolve()
    with FileLock(root / "campaign.lock", timeout=.1):
        control, cases = _verified_control(root)
        for case in cases:
            identifier = case["id"]
            target = _receipt_path(root, identifier)
            if target.exists():
                continue
            intent = root / "results/intents" / (_suffix(identifier) + ".json")
            result = {"id": identifier, "status": "case_error" if intent.exists() else "not_run",
                      "reason": "unknown_prior_task_outcome" if intent.exists() else "promptfoo_did_not_invoke_task",
                      "result": "case_error" if intent.exists() else "not_run", "model_quality": "unsupported",
                      "model_api_calls": 0 if control["test_only"] else sum(call["case_id"] == identifier for call in
                          _read(root / "results/http-budget.json")["calls"]),
                      "case_provenance": case.get("provenance", {}), "budget_group": identifier}
            _write(target, {"dataset_binding": control["dataset_binding"], "result": result})
        return _merge_report(root, control)
