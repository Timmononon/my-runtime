"""Strict offline projection of a no-API-quota, thinking-disabled BFCL campaign.

This adapter reads existing evidence only. It never starts providers, agents,
review models, model loading, or telemetry. Historical adapters stay unchanged.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from aster.evaluation.bfcl_multiturn_export import (
    SUBCATEGORIES,
    TOKEN_FIELDS,
    _checks,
    _digest,
    _hash,
    _read,
    _redact,
    _secrets,
    _turns,
    _usage,
    _verify_sources,
    classify_outcome,
    validate_record,
    write_bundle,
)
from aster.evaluation.bfcl_multiturn_live_export import _checked_overrides, _label, _suffix


def load_unmetered_campaign(campaign_dir, *, config_label=None):
    """Read saved live evidence only; never instantiate providers or simulators."""
    root = Path(campaign_dir).expanduser().resolve()
    source, plan, report, ledger = [_read(root / name) for name in (
        "source-manifest.json", "campaign-plan.json", "results/results.json", "results/http-budget.json")]
    _verify_sources(root, source)
    if report.get("mode") != "live_current_model" or report.get("dataset_binding") != ledger.get("binding"):
        raise ValueError("Only a bound saved live campaign is supported")
    control = _read(root / "execution-control.json")
    if control.get("dataset_binding") != report["dataset_binding"]:
        raise ValueError("Control and saved report bindings differ")
    if ("max_api_calls" not in report or report["max_api_calls"] is not None
            or "maximum" not in ledger or ledger["maximum"] is not None
            or report.get("authorized_unlimited_api") is not True or ledger.get("authorized_unlimited_api") is not True
            or control.get("authorized_unlimited_api") is not True or "max_api_calls" not in control
            or control["max_api_calls"] is not None or report.get("api_unlimited") is not True
            or ledger.get("api_unlimited") is not True or control.get("api_unlimited") is not True
            or plan.get("authorized_unlimited_api") is not True
            or "authorized_api_call_limit" not in plan or plan["authorized_api_call_limit"] is not None
            or control.get("per_case_maximum") is not None or control.get("future_valid_case_reserve") != 0):
        raise ValueError("This loader requires explicit unmetered HTTP accounting")
    if (report.get("profile", {}).get("model") != "deepseek-flash"
            or report.get("profile", {}).get("protocol") != "deepseek_responses"
            or report.get("profile", {}).get("reasoning_effort") != "none"
            or plan.get("model") != "deepseek-flash" or plan.get("reasoning_effort") != "none"
            or report.get("max_output_tokens") != 8192 or ledger.get("wire_max_output_tokens") != 8192):
        raise ValueError("Saved model/thinking/output policy differs from this evaluation")
    expected_bounds = {identifier: 20 * group["user_turns"] for group in plan["groups"]
                       for identifier in group["entry_ids"]}
    if (ledger.get("case_caps") != dict.fromkeys(expected_bounds)
            or ledger.get("case_official_step_bounds") != expected_bounds
            or report.get("case_official_step_bounds") != expected_bounds
            or control.get("case_official_step_bounds") != expected_bounds
            or ledger.get("official_max_steps_per_turn") != 20 or control.get("official_max_steps_per_turn") != 20):
        raise ValueError("Official per-user-turn bounds or null task quotas differ")
    for call in ledger["calls"]:
        if (call.get("case_id") not in expected_bounds or call.get("thinking_disabled_verified") is not True
                or call.get("wire_model") != "deepseek-flash" or call.get("wire_reasoning_effort") != "none"
                or call.get("wire_max_output_tokens") != 8192):
            raise ValueError("Missing or changed thinking-disabled HTTP witness")
    if report.get("model_api_calls") != len(ledger["calls"]):
        raise ValueError("Campaign total differs from its durable HTTP ledger")
    groups = {identifier: group for group in plan["groups"] for identifier in group["entry_ids"]}
    label = _label(config_label, root)
    evidence = {name: _hash(root / name) for name in (
        "source-manifest.json", "campaign-plan.json", "execution-control.json", "results/results.json", "results/http-budget.json")}
    analysis_file = root / "independent-analysis.json"
    analysis = _read(analysis_file) if analysis_file.is_file() else None
    if analysis:
        if analysis.get("dataset_binding_recomputed") != report["dataset_binding"]:
            raise ValueError("Independent analysis has a different binding")
        for name, digest in evidence.items():
            if analysis.get("immutable_evidence_checks", {}).get("sha256", {}).get(name) != digest:
                raise ValueError("Source campaign changed after its independent analysis")
        evidence["independent-analysis.json"] = _hash(analysis_file)
    model = {key: report.get("profile", {}).get(key) for key in (
        "provider_id", "protocol", "model", "reasoning_effort", "identity_revision")}
    config = {"model": model, "max_api_calls": report["max_api_calls"],
        "authorized_unlimited_api": True, "inherited_runtime_safety": control["inherited_runtime_safety"],
        "max_output_tokens": report.get("max_output_tokens"), "case_api_caps": report.get("case_api_caps"),
        "case_groups": report.get("case_groups"), "task_budgets_by_id": {
            row["id"]: row.get("budget", {}).get("limits") for row in report["results"]}}
    records, seen = [], set()
    for row in report["results"]:
        identifier = row.get("id", "")
        category = next((name for name in SUBCATEGORIES if identifier.startswith(name + "_")), None)
        if category is None or identifier not in groups or identifier in seen:
            raise ValueError("Unexpected or duplicate Multi-Turn task")
        seen.add(identifier)
        group = groups[identifier]
        if group["category"] != category or group.get("source_kind") != "official_fixed_revision_complete_task":
            raise ValueError("Task source/category does not match the frozen plan")
        receipt_path = root / f"results/receipts/{_suffix(identifier)}.json"
        receipt = _read(receipt_path)
        if receipt.get("dataset_binding") != report["dataset_binding"] or receipt.get("result") != row:
            raise ValueError("Driver receipt differs from campaign result")
        case_path = root / f"results/cases/{_suffix(identifier)}/result.json"
        overrides = _checked_overrides(_read(case_path), row) if case_path.is_file() else []
        if not case_path.is_file() and row.get("status") not in {"blocked_dataset_conflict", "not_run", "case_error"}:
            raise ValueError("Core execution evidence is missing")
        calls = [call for call in ledger["calls"] if call["case_id"] == identifier]
        if len(calls) != row.get("model_api_calls"):
            raise ValueError("Per-task HTTP count differs from recorded usage")
        if len(calls) > expected_bounds[identifier] or row.get("allocated_api_call_cap") is not None:
            raise ValueError("Task request history contradicts the official step bound or null quota")
        outcome = classify_outcome(row)
        termination = row.get("termination_kind")
        if termination:
            outcome["reasons"] = sorted(set(outcome["reasons"] + ["termination:" + termination]))
        scorable = outcome["category"] not in {"dataset_conflict", "infrastructure_error", "not_run"}
        scoring = row.get("scoring_verification", {})
        checks = [_checks(check) for check in scoring.get("turns", [])]
        passed = row.get("result") == "pass" if scorable else None
        if scorable and (type(scoring.get("valid")) is not bool or scoring["valid"] != passed
                         or scoring.get("result") != row.get("result")):
            raise ValueError("Formal result differs from the saved checker")
        if passed and row.get("forced_termination"):
            raise ValueError("A forced termination cannot pass")
        diagnostic = (analysis or {}).get("independent_offline_checks", {}).get(identifier, {}).get("turns", [])
        behavior_codes = [check.get("error_type", "unspecified") for turn in diagnostic
            for key in ("irrelevance", "state_response") if isinstance(check := turn.get(key), dict)
            and check.get("valid") is False and (key == "irrelevance" or turn.get("runtime_completed")
                                                 and turn.get("reference_call_count", 0) > 0)]
        outcome["task_failure_observed"] = bool(behavior_codes) if diagnostic else None
        outcome["reasons"] = sorted(set(outcome["reasons"] + ["diagnostic:" + code for code in behavior_codes]))
        provenance = row.get("case_provenance", {})
        record = {"schema_version": 1, "task_id": identifier, "subcategory": category, "config_label": label,
            "source_identity": {"source_manifest_sha256": evidence["source-manifest.json"],
                "as_run_files_sha256": _digest(source["files"]), "file_count": len(source["files"]),
                "integrity_verified": True, "current_workspace_source_used": False},
            "config_identity": {"sha256": _digest(config), "snapshot": config, "label_is_display_metadata_only": True},
            "model_identity": model, "turns": _turns(row, group["user_turns"], checks),
            "scoring": {"passed": passed, "formal_result": row.get("result"), "turn_checks": checks,
                "reference_admission_issues": row.get("reference_admission_issues", []),
                "source_revision": provenance.get("revision"), "scoring_rule": "bfcl_v4_pinned_official_checker",
                "checks_are_saved_results": True, "official_score": False, "diagnostic_turn_checks": diagnostic,
                "diagnostics_do_not_replace_formal_score": True}, "outcome": outcome, "recorded_usage": _usage(row),
            "replay": {"mode": "saved_result", "new_model_api_calls": 0, "new_judge_api_calls": 0,
                       "model_capability_measurement": False},
            "provenance": {"campaign_name": root.name, "dataset_binding": report["dataset_binding"],
                "question_sha256": provenance.get("question_sha256"), "source_kind": group["source_kind"],
                "expected_user_turns": group["user_turns"], "executed_user_turns": len(row.get("runs", [])),
                "allocated_api_call_cap": None, "official_model_step_bound": expected_bounds[identifier],
                "termination_kind": row.get("termination_kind"),
                "thinking_disabled_verified_http_attempts": len(calls),
                "core_driver_overrides": overrides, "projection_adapter": "bfcl_multiturn_unmetered_export",
                "evidence_sha256": {**evidence, receipt_path.relative_to(root).as_posix(): _hash(receipt_path),
                    **({case_path.relative_to(root).as_posix(): _hash(case_path)} if case_path.is_file() else {})}}}
        secrets = _secrets(row.get("tool_ledger", []))
        record["turns"] = _redact(record["turns"], secrets)
        record["scoring"]["reference_admission_issues"] = _redact(record["scoring"]["reference_admission_issues"], secrets)
        record["record_sha256"] = _digest(record)
        records.append(validate_record(record))
    if seen != set(groups):
        raise ValueError("Campaign omitted planned task records")
    return records


def campaign_summary(campaign_dir, records=None):
    root = Path(campaign_dir).expanduser().resolve()
    records = load_unmetered_campaign(root) if records is None else records
    report, ledger = _read(root / "results/results.json"), _read(root / "results/http-budget.json")
    for record in records:
        validate_record(record)
    usage, unknown = Counter(), 0
    for row in report["results"]:
        known = [entry for entry in row.get("usage", []) if entry.get("input_tokens") is not None]
        unknown += row.get("model_api_calls", 0) - len(known)
        for entry in known:
            for key in TOKEN_FIELDS:
                if entry.get(key) is not None:
                    usage[key] += entry[key]
    scorable = [record for record in records if record["scoring"]["passed"] is not None]
    passed = sum(record["scoring"]["passed"] is True for record in records)
    by_category = {}
    for category in SUBCATEGORIES:
        group = [record for record in records if record["subcategory"] == category]
        valid = [record for record in group if record["scoring"]["passed"] is not None]
        successes = sum(record["scoring"]["passed"] is True for record in group)
        by_category[category] = {"tasks": len(group), "scorable_tasks": len(valid), "passed": successes,
            "failed": sum(record["scoring"]["passed"] is False for record in group),
            "excluded": len(group) - len(valid), "outcomes": dict(Counter(record["outcome"]["category"] for record in group)),
            "termination_kinds": dict(Counter(record["provenance"].get("termination_kind") for record in group)),
            "success_rate": successes / len(valid) if valid else None}
    return {"schema_version": 1, "kind": "saved_no_thinking_unmetered_campaign", "unique_tasks": len(records),
        "native_promptfoo_provider_invocations": len(report["results"]),
        "model_invoked_unique_tasks": sum(record["recorded_usage"]["model_requests"] > 0 for record in records),
        "actual_model_http_attempts": len(ledger["calls"]), "api_call_limit": None,
        "authorized_unlimited_api": True, "artificial_whole_task_api_cap": None,
        "official_max_steps_per_user_turn": 20, "wire_reasoning_effort": "none", "wire_max_output_tokens": 8192,
        "thinking_disabled_wire_verified_calls": len(ledger["calls"]),
        "subcategories": by_category,
        "tasks_over_twenty_requests": [{"task_id": record["task_id"], "model_requests": record["recorded_usage"]["model_requests"]}
            for record in records if record["recorded_usage"]["model_requests"] > 20],
        "recorded_formal_results": dict(Counter(record["scoring"]["formal_result"] for record in records)),
        "outcomes": dict(Counter(record["outcome"]["category"] for record in records)),
        "termination_kinds": dict(Counter(record["provenance"].get("termination_kind") for record in records)),
        "scorable_tasks": len(scorable), "recorded_passed_tasks": passed,
        "recorded_scorable_subset_success_rate": passed / len(scorable) if scorable else None,
        "known_token_subtotal": dict(usage), "unknown_paid_response_usage": unknown,
        "token_subtotal_is_complete": unknown == 0, "new_model_api_calls_during_export": 0,
        "new_judge_api_calls": 0, "official_score": False, "full_leaderboard_score": None,
        "comparison_note": "Thinking, API quotas and output size changed; no single-factor optimization gain is claimed."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--label")
    args = parser.parse_args(argv)
    records = load_unmetered_campaign(args.campaign, config_label=args.label)
    report = campaign_summary(args.campaign, records)
    output = Path(args.output).resolve()
    write_bundle(records, output)
    with (output / "run-report.json").open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
