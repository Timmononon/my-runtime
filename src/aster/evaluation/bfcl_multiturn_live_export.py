"""Offline projection of durable live-task receipts, including driver overrides.

The original saved-campaign loader remains unchanged. This narrowly compatible
loader retains raw core and driver hashes instead of rewriting an old result to
hide the driver's explicit infrastructure classification. No model is imported.
"""
from __future__ import annotations

import argparse
import hashlib
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


def _suffix(identifier):
    return hashlib.sha256(identifier.encode()).hexdigest()[:24]


def _checked_overrides(core, row):
    changed = [key for key, value in core.items() if row.get(key) != value]
    if not changed:
        return []
    typed_failure = any(failure.get("type") == "ProviderError" and failure.get("category") not in {None, "budget"}
                        for failure in row.get("model_failures", []))
    if (changed != ["model_quality"] or core.get("model_quality") != "evaluated"
            or row.get("model_quality") != "unsupported" or row.get("status") != "case_error" or not typed_failure):
        raise ValueError("Unexpected core/driver difference; raw evidence cannot be normalized")
    return [{"field": "model_quality", "core": "evaluated", "driver": "unsupported",
             "reason": "typed_nonbudget_provider_failure", "raw_core_preserved": True}]


def _label(value, root):
    value = value or root.name
    if not isinstance(value, str) or not value.strip() or len(value) > 120 or any(ord(char) < 32 for char in value):
        raise ValueError("A short nonempty phase label is required")
    return value


def load_live_campaign(campaign_dir, *, config_label=None):
    """Read saved live evidence only; never instantiate providers or simulators."""
    root = Path(campaign_dir).expanduser().resolve()
    source, plan, report, ledger = [_read(root / name) for name in (
        "source-manifest.json", "campaign-plan.json", "results/results.json", "results/http-budget.json")]
    _verify_sources(root, source)
    if report.get("mode") != "live_current_model" or report.get("dataset_binding") != ledger.get("binding"):
        raise ValueError("Only a bound saved live campaign is supported")
    if not 0 <= len(ledger["calls"]) <= report.get("max_api_calls", -1) <= 200:
        raise ValueError("Saved HTTP count exceeds the authorized campaign cap")
    if report.get("model_api_calls") != len(ledger["calls"]):
        raise ValueError("Campaign total differs from its durable HTTP ledger")
    groups = {identifier: group for group in plan["groups"] for identifier in group["entry_ids"]}
    label = _label(config_label, root)
    evidence = {name: _hash(root / name) for name in (
        "source-manifest.json", "campaign-plan.json", "results/results.json", "results/http-budget.json")}
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
        outcome = classify_outcome(row)
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
                "allocated_api_call_cap": row.get("allocated_api_call_cap"),
                "core_driver_overrides": overrides, "projection_adapter": "bfcl_multiturn_live_export",
                "evidence_sha256": {**evidence, receipt_path.relative_to(root).as_posix(): _hash(receipt_path),
                    **({case_path.relative_to(root).as_posix(): _hash(case_path)} if case_path.is_file() else {})}}}
        secrets = _secrets(row.get("tool_ledger", []))
        record["turns"] = _redact(record["turns"], secrets)
        record["scoring"]["reference_admission_issues"] = _redact(record["scoring"]["reference_admission_issues"], secrets)
        record["record_sha256"] = _digest(record)
        records.append(validate_record(record))
    return records


def merge_uninvoked_campaigns(original_dir, supplement_dir):
    """Replace only parent-scoped never-dispatched rows; preserve both phases."""
    original, supplement = Path(original_dir).resolve(), Path(supplement_dir).resolve()
    original_records = load_live_campaign(original, config_label="initial-as-run")
    new_records = load_live_campaign(supplement, config_label="supplement-as-run")
    plan = _read(supplement / "campaign-plan.json")
    proof = plan["supplement"]
    first_report, second_report = [_read(root / "results/results.json") for root in (original, supplement)]
    first_http, second_http = [_read(root / "results/http-budget.json") for root in (original, supplement)]
    if Path(proof["original_campaign"]).resolve() != original or proof["original_dataset_binding"] != first_report["dataset_binding"]:
        raise ValueError("Supplement has a different parent campaign")
    for name, digest in proof["original_evidence_sha256"].items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or not (original / relative).resolve().is_relative_to(original):
            raise ValueError("Unsafe original proof path")
        if _hash(original / relative) != digest:
            raise ValueError("Original evidence changed after supplement admission")
    first_calls, second_calls = len(first_http["calls"]), len(second_http["calls"])
    if (first_calls != proof["original_actual_http_attempts"] or proof["remaining_api_call_limit"] != 200 - first_calls
            or second_http["maximum"] != 200 - first_calls or first_calls + second_calls > 200):
        raise ValueError("Combined HTTP attempts exceed the original shared 200 authorization")
    original_rows = {row["id"]: row for row in first_report["results"]}
    eligible = {identifier for identifier, row in original_rows.items() if row.get("result") == "not_run"
                and row.get("model_api_calls") == 0 and not row.get("runs") and not row.get("messages")
                and not any(call["case_id"] == identifier for call in first_http["calls"])}
    replacements = {record["task_id"]: record for record in new_records}
    if set(replacements) != set(proof["selected_task_ids"]) or not set(replacements) <= eligible:
        raise ValueError("Supplement cannot replace a previously entered, failed or dispatched task")
    old = {record["task_id"]: record for record in original_records}
    combined = []
    for record in original_records:
        identifier = record["task_id"]
        if identifier in replacements:
            replacement = replacements[identifier]
            if (replacement["model_identity"] != record["model_identity"]
                    or replacement["provenance"]["question_sha256"] != record["provenance"]["question_sha256"]
                    or replacement["subcategory"] != record["subcategory"]):
                raise ValueError("Supplement model/question/category differs from its original task")
            replacement["provenance"]["replaced_uninvoked_parent_record_sha256"] = record["record_sha256"]
            replacement["record_sha256"] = _digest({key: value for key, value in replacement.items() if key != "record_sha256"})
            combined.append(validate_record(replacement))
        else:
            combined.append(record)
    if len(combined) != len(old) or len({record["task_id"] for record in combined}) != len(combined):
        raise ValueError("Combined cohort must retain its original unique task count")
    usage, unknown = Counter(), 0
    for report in (first_report, second_report):
        for row in report["results"]:
            known = [entry for entry in row.get("usage", []) if entry.get("input_tokens") is not None]
            unknown += row.get("model_api_calls", 0) - len(known)
            for entry in known:
                for key in TOKEN_FIELDS:
                    if entry.get(key) is not None:
                        usage[key] += entry[key]
    scorable = [record for record in combined if record["scoring"]["passed"] is not None]
    successful = sum(record["scoring"]["passed"] is True for record in combined)
    admissible = [record for record in combined if record["outcome"]["category"] != "dataset_conflict"]
    summary = {"schema_version": 1, "kind": "descriptive_two_phase_same_cohort", "unique_tasks": len(combined),
        "source_campaigns": [original.name, supplement.name], "source_versions_are_distinct":
            original_records[0]["source_identity"] != new_records[0]["source_identity"],
        "not_an_optimization_comparison": True, "optimization_gain_computed": False,
        "original_model_http_attempts": first_calls, "supplement_model_http_attempts": second_calls,
        "total_model_http_attempts": first_calls + second_calls, "original_authorized_api_cap": 200,
        "native_promptfoo_provider_invocations": len(first_report["results"]) + len(second_report["results"]),
        "model_invoked_unique_tasks": sum(record["recorded_usage"]["model_requests"] > 0 for record in combined),
        "recorded_formal_results": dict(Counter(record["scoring"]["formal_result"] for record in combined)),
        "outcomes": dict(Counter(record["outcome"]["category"] for record in combined)),
        "scorable_tasks": len(scorable), "recorded_passed_tasks": successful,
        "recorded_scorable_subset_success_rate": successful / len(scorable) if scorable else None,
        "admissible_tasks": len(admissible),
        "recorded_end_to_end_completion_rate_among_admissible": successful / len(admissible) if admissible else None,
        "known_token_subtotal": dict(usage), "unknown_paid_response_usage": unknown,
        "token_subtotal_is_complete": unknown == 0, "new_model_api_calls_during_export": 0,
        "new_judge_api_calls": 0, "official_score": False, "full_leaderboard_score": None}
    return combined, summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original", required=True)
    parser.add_argument("--supplement", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    records, summary = merge_uninvoked_campaigns(args.original, args.supplement)
    output = Path(args.output).resolve()
    write_bundle(records, output)
    with (output / "combined-report.json").open("x", encoding="utf-8") as stream:
        json.dump(summary, stream, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
