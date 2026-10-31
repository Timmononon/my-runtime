"""Holdout-sized form of the existing sealed native-campaign collector.

The validation/statistics body is derived from the frozen-development launcher;
only explicit membership cardinality and held-out scope reporting are changed.
Historical source is never modified.
"""
from __future__ import annotations

import importlib.util
import json
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location("holdout_collector_existing_core", HERE / "run_compact_development.py")
_core = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_core)
_helpers = _core._helpers
BACKEND_MODE = _core.BACKEND_MODE
PERFORMANCE_POLICY = _core.PERFORMANCE_POLICY
_dataset = _core._dataset
_classification = _core._classification
_runtime_infrastructure = _core._runtime_infrastructure
_category, _rate, _peak = _core._category, _core._rate, _core._peak

DERIVED_COLLECTOR_SOURCE_SHA256 = "3486117c0fbe6ab04bf61971041e79b4dff80a4edd2596df0c7b05e2d7d72e26"


def validate_native(payload, rows):
    from copy import deepcopy

    normalized = deepcopy(payload)
    policies = {row.get("id"): row.get("agent_policy") for row in rows}
    expected = {None: "A0", "readiness_state_v1": "A3", "capability_check_v1": "MF-Gate-v1"}
    errors = []
    for item in normalized.get("results", {}).get("results", []):
        task_id = item.get("vars", {}).get("task_id")
        label = expected.get(policies.get(task_id))
        if item.get("provider", {}).get("label") != f"{label}-holdout60-flash-none":
            errors.append("holdout_provider_configuration_mismatch")
        item.setdefault("provider", {})["label"] = _core.PROVIDER_LABEL
    return sorted(set(errors + _core.validate_native(normalized, rows)))


def collect_base_campaign(campaign, plan, payload, output, *, expected_model="deepseek-flash",
                     native_validator=None, receipt_reader=None, classification=None, runtime_infrastructure=None):
    """Check every admitted holdout native outcome; data conflicts are null, never model zeros."""
    from aster.evaluation.bfcl_multiturn_concurrent import read_task_receipt

    classify = classification or _classification
    runtime_failure = runtime_infrastructure or _runtime_infrastructure

    def is_scorable(row):
        return (row.get("result") in {"pass", "fail"} and classify(row) in {
            "pass", "task_failure", "budget_termination", "agent_control_failure"})

    campaign, output = Path(campaign).resolve(), Path(output).resolve()
    report = json.loads((campaign / "results/results.json").read_bytes())
    control = json.loads((campaign / "execution-control.json").read_bytes())
    ledger = json.loads((campaign / "results/http-budget.json").read_bytes())
    rows, calls = report["results"], ledger["calls"]
    ids = [group["group"] for group in plan["groups"]]
    expected_turns = {group["group"]: group["user_turns"] for group in plan["groups"]}
    conflicts = set(plan["reference_admission_issues"])
    errors = (native_validator or validate_native)(payload, rows)
    receipt_reader = receipt_reader or read_task_receipt
    if (len(ids) != plan["evaluation_task_count"] or len(set(ids)) != plan["evaluation_task_count"] or [row["id"] for row in rows] != ids
            or control["case_ids"] != ids or ledger["binding"] != control["dataset_binding"]
            or report["dataset_binding"] != control["dataset_binding"]):
        errors.append("shared_campaign_identity_mismatch")
    if {key for key, value in control["reference_admission_issues"].items() if value} != conflicts:
        errors.append("admission_conflict_set_changed")
    mode = control.get("concurrent_tasks", {})
    if (mode.get("mode") != BACKEND_MODE or mode.get("maximum") != plan["maximum_concurrency"]
            or mode.get("finalized") is not True or set(mode.get("states", {}).values()) != {"completed"}):
        errors.append("concurrent_campaign_not_sealed")
    if (control.get("concurrency_performance_policy") != plan.get("concurrency_performance_policy")
            or control.get("concurrency_performance_policy") != PERFORMANCE_POLICY):
        errors.append("concurrent_infrastructure_policy_mismatch")
    if control["test_only"] or ledger.get("maximum") is not None or report.get("max_api_calls") is not None:
        errors.append("unexpected_test_or_api_budget_policy")
    control_count = len(list(campaign.rglob("execution-control.json")))
    ledger_count = len(list(campaign.rglob("http-budget.json")))
    if control_count != 1 or ledger_count != 1 or (campaign / "children").exists():
        errors.append("multiple_backend_campaigns_or_ledgers")
    callbacks, intervals, usage, sessions, task_details = [], [], [], set(), []
    completed_tasks = completed_turns = 0
    for row in rows:
        task_id = row["id"]
        if task_id not in expected_turns:
            errors.append("unknown_result_task")
            continue
        if receipt_reader(campaign, task_id) != row:
            errors.append("durable_task_receipt_mismatch")
        key = _helpers.digest(task_id.encode())[:24]
        start_path, end_path = (output / "dispatches" / (key + suffix)
            for suffix in (".started.json", ".finished.json"))
        if not start_path.is_file() or not end_path.is_file():
            errors.append("missing_task_callback_evidence")
            continue
        started, ended = json.loads(start_path.read_bytes()), json.loads(end_path.read_bytes())
        local = [call for call in calls if call["case_id"] == task_id]
        if (started.get("task_id") != task_id or ended.get("task_id") != task_id
                or started.get("pid") != ended.get("pid")
                or ended.get("campaign_dir") != str(campaign) or ended.get("backend_mode") != BACKEND_MODE
                or ended.get("outcome") != "backend_returned" or ended.get("error_type") is not None
                or ended.get("http_attempts_for_task") != len(local)
                or row.get("http_attempts") != len(local) or row.get("model_api_calls") != len(local)):
            errors.append("task_callback_or_accounting_mismatch")
        if not (campaign / "results/intents" / (key + ".json")).is_file():
            errors.append("missing_task_intent")
        if task_id in conflicts:
            if (row.get("result") != "blocked_dataset_conflict" or local
                    or row.get("reference_admission_issues") != plan["reference_admission_issues"][task_id]):
                errors.append("quarantined_source_was_dispatched_or_changed")
        elif row.get("result") == "blocked_dataset_conflict":
            errors.append("undeclared_source_conflict")
        if row.get("result") in {"pass", "fail"}:
            if not local:
                errors.append("scored_task_without_model_request")
            passed = row["result"] == "pass"
            scoring = row.get("scoring_verification", {})
            if (scoring.get("valid") is not passed or row.get("protocol_result") not in {"pass", "fail"}
                    or scoring.get("result", row["result"]) != row["result"]):
                errors.append("formal_result_differs_from_official_checker")
        if local:
            first = min(call["started_at"] for call in local)
            if first > ended["finished_at"]:
                errors.append("inconsistent_overlap_timestamps")
            else:
                intervals.append((first, ended["finished_at"]))
        callbacks.append(ended)
        if row.get("session_id"):
            if row["session_id"] in sessions:
                errors.append("shared_runtime_session")
            sessions.add(row["session_id"])
        runs = row.get("runs", [])
        turns = sum(run.get("runtime_status") == "completed" for run in runs)
        completed_turns += turns
        completed = len(runs) == expected_turns[task_id] and turns == len(runs)
        completed_tasks += completed
        usage.extend(row.get("usage", []))
        task_details.append({"task_id": task_id, "category": _category(task_id),
            "formal_result": row.get("result"), "classification": classify(row),
            "main_score": float(row["result"] == "pass") if is_scorable(row) else None,
            "model_requests": len(local), "completed_user_turns": turns,
            "defined_user_turns": expected_turns[task_id], "runtime_completed": completed,
            "termination_kind": row.get("termination_kind"),
            "official_error_types": sorted({turn.get("error_type", "unspecified")
                for turn in row.get("scoring_verification", {}).get("turns", []) if turn.get("valid") is False})})
    if len(list((output / "dispatches").glob("*.started.json"))) != len(ids) or len(
            list((output / "dispatches").glob("*.finished.json"))) != len(ids):
        errors.append("callback_count_mismatch")
    verified_wire = sum(call.get("thinking_disabled_verified") is True
        and call.get("wire_model") == expected_model and call.get("wire_reasoning_effort") == "none"
        and call.get("wire_max_output_tokens") == 8192 for call in calls)
    if (verified_wire != len(calls) or report.get("model_api_calls") != len(calls)
            or any(call.get("case_id") not in ids for call in calls)):
        errors.append("wire_policy_or_global_accounting_mismatch")
    known = [item for item in usage if item.get("input_tokens") is not None and item.get("output_tokens") is not None]
    known_reasoning = [item for item in usage if item.get("reasoning_tokens") is not None]
    if len(known) > len(calls) or len(known_reasoning) > len(calls):
        errors.append("duplicate_usage_records")
    totals = {field: sum(item[field] for item in known if item.get(field) is not None)
        for field in ("input_tokens", "output_tokens", "cache_hit_tokens", "cache_miss_tokens")}
    totals["reasoning_tokens"] = sum(item["reasoning_tokens"] for item in known_reasoning)
    if any(item["reasoning_tokens"] != 0 for item in known_reasoning):
        errors.append("unexpected_reasoning_tokens")
    peak = _peak(intervals)
    if peak > plan["maximum_concurrency"]:
        errors.append("observed_task_overlap_exceeds_frozen_limit")
    if len(intervals) > 1 and plan["maximum_concurrency"] > 1 and peak <= 1:
        errors.append("no_observed_task_execution_overlap")
    valid = [row for row in rows if is_scorable(row)]
    passed = sum(row["result"] == "pass" for row in valid)
    by_category = {}
    for category in _dataset.CATEGORIES:
        category_rows = [row for row in rows if _category(row["id"]) == category]
        scorable = [row for row in category_rows if is_scorable(row)]
        good = sum(row["result"] == "pass" for row in scorable)
        quarantined = sum(row["id"] in conflicts for row in category_rows)
        classifications = dict(Counter(classify(row) for row in category_rows))
        by_category[category] = {"allocated_tasks": len(category_rows), "data_conflicts": quarantined,
            "admissible_tasks": len(category_rows) - quarantined, "scorable_tasks": len(scorable),
            "passed": good, "failed": len(scorable) - good, "classifications": classifications,
            "scorable_success_rate": _rate(good, len(scorable)),
            "delivery_success_rate": _rate(good, len(category_rows) - quarantined),
            "weight_within_multiturn": 0.25}
    macro = [group["scorable_success_rate"] for group in by_category.values()]
    unknown_requests = sum(call.get("status") == "outcome_unknown" for call in calls)
    admissible = len(ids) - len(conflicts)
    infra = sum(classify(row) in {"infrastructure_error", "not_run"} for row in rows)
    readiness_reasons = []
    if errors:
        readiness_reasons.append("evidence_validation_errors")
    if control.get("fatal"):
        readiness_reasons.append("shared_campaign_fatal")
    if infra:
        readiness_reasons.append("infrastructure_or_uninvoked_tasks")
    if len(valid) != admissible:
        readiness_reasons.append("not_all_admissible_tasks_scored")
    if unknown_requests:
        readiness_reasons.append("unknown_request_outcomes")
    if any(row["id"] not in conflicts and row.get("result") in {"pass", "fail"}
           and not row.get("session_id") for row in rows):
        readiness_reasons.append("missing_scored_task_session")
    capability_ready = not readiness_reasons
    unknown_usage = max(0, len(calls) - len(known))
    unknown_reasoning = max(0, len(calls) - len(known_reasoning))
    result = {"same_campaign": True, "backend_mode": BACKEND_MODE,
        "dataset_binding": control["dataset_binding"], "maximum_concurrency": plan["maximum_concurrency"],
        "concurrency_performance_policy": control.get("concurrency_performance_policy"),
        "backend_control_count": control_count, "http_ledger_count": ledger_count,
        "allocated_development_tasks": 40, "allocated_holdout_tasks": 60,
        "evaluation_split": "holdout", "development_inference_tasks": 0,
        "holdout_inference_tasks": len({call["case_id"] for call in calls}), "planned_tasks": len(ids),
        "completed_result_rows": len(rows), "native_provider_invocations": len(payload.get("results", {}).get("results", [])),
        "admissible_tasks": admissible, "data_conflict_tasks": len(conflicts), "data_conflict_ids": sorted(conflicts),
        "formal_task_results": dict(Counter(row.get("result") for row in rows)),
        "failure_classifications": dict(Counter(classify(row) for row in rows)),
        "scorable_tasks": len(valid), "passed_tasks": passed,
        "main_success_rate": _rate(passed, admissible) if capability_ready else None,
        "runtime_infrastructure_ids": [row["id"] for row in rows if runtime_failure(row)],
        "scorable_success_rate": _rate(passed, len(valid)),
        "multiturn_category_macro_success_rate": sum(macro) / 4 if all(rate is not None for rate in macro) else None,
        "admissible_delivery_success_rate": _rate(passed, admissible), "subcategories": by_category,
        "all_admissible_tasks_scored": len(valid) == admissible,
        "model_api_calls": len(calls), "http_transport_attempts": len(calls),
        "http_status_codes": dict(Counter(str(call.get("status_code")) for call in calls)),
        "unknown_request_outcomes": unknown_requests, "shared_fatal": control.get("fatal"),
        "provider_worker_pids": sorted({row["pid"] for row in callbacks}),
        "configured_python_workers": plan["maximum_concurrency"], "independent_session_count": len(sessions),
        "complete_runtime_tasks": completed_tasks, "completed_user_turns": completed_turns,
        "defined_user_turns": sum(expected_turns.values()),
        "admissible_defined_user_turns": sum(count for key, count in expected_turns.items() if key not in conflicts),
        "peak_overlapping_tasks_with_http": peak, "overlap_measurement": plan["overlap_measurement"],
        "wire_none_verified_calls": verified_wire, "known_usage_totals": totals,
        "unknown_core_usage_calls": unknown_usage, "unknown_reasoning_usage_calls": unknown_reasoning,
        "all_known_reasoning_tokens_zero": all(item["reasoning_tokens"] == 0 for item in known_reasoning),
        "evidence_errors": sorted(set(errors)), "judge_api_calls": 0,
        "official_score": False, "full_leaderboard_score": None, "measured_speedup": None,
        "score_note": "Frozen public held-out subset measurement; not the full BFCL leaderboard or a new development result",
        "baseline_assessment": {"kind": plan["configuration_label"] + "_frozen_holdout_agent_configuration",
            "capability_baseline_ready": capability_ready,
            "usage_baseline_ready": capability_ready and unknown_usage == unknown_reasoning == 0,
            "readiness_limitations": readiness_reasons,
            "budget_terminations": sum(classify(row) == "budget_termination" for row in rows),
            "scope": "Same sixty held-out tasks/model/thinking/safety/scoring/concurrency for paired frozen-policy comparison",
            "single_run_only": True, "holdout_validation_performed": capability_ready,
            "model_pretraining_exposure": "unknown; public benchmark, not proof of unseen model capability"},
        "tasks": task_details}
    return result
