"""Local, deterministic projections of saved BFCL Multi-Turn evidence.

This module deliberately uses only the standard library. It imports no provider,
Runtime, judge, telemetry client or model loader. A projection preserves the
recorded checker decision; it is not a fresh model capability measurement.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path

SUBCATEGORIES = ("multi_turn_base", "multi_turn_miss_func", "multi_turn_miss_param",
                 "multi_turn_long_context")
OUTCOMES = ("pass", "task_failed", "budget_terminated", "dataset_conflict", "infrastructure_error", "not_run")
TOKEN_FIELDS = ("input_tokens", "output_tokens", "reasoning_tokens", "cache_hit_tokens", "cache_miss_tokens")
_SECRET_KEYS = re.compile(r"(?:password|secret|authorization|credential|api[_-]?key|access[_-]?token)", re.IGNORECASE)
_CODES = re.compile(r"[a-zA-Z0-9_:./-]{1,180}\Z")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _read(path):
    def invalid(_value):
        raise ValueError("Non-finite JSON value")
    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_pairs, parse_constant=invalid)
    json.dumps(value, allow_nan=False)
    return value


def _within(root, relative):
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("Evidence paths must be relative")
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("Evidence path leaves its root")
    return resolved


def _number(value, *, integral=False):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("Usage must be a non-negative finite number or null")
    if integral and not isinstance(value, int):
        raise ValueError("Request/token counts must be integers")
    return value


def _code(value):
    return value if isinstance(value, str) and _CODES.fullmatch(value) else "unspecified"


def _checks(value):
    if not isinstance(value, dict) or type(value.get("valid")) is not bool:
        raise ValueError("Saved checker result needs an explicit boolean valid field")
    return {key: value[key] for key in ("valid", "result", "error_type", "scoring_rule", "source_revision")
            if key in value and isinstance(value[key], (str, bool))}


def classify_outcome(row):
    """Keep stopping cause separate from the saved BFCL pass/fail decision."""
    reasons = []
    failures = row.get("model_failures", [])
    for failure in failures:
        reasons.append("model:" + _code(failure.get("category")))
    for check in row.get("scoring_verification", {}).get("turns", []):
        if check.get("error_type"):
            reasons.append(_code(check["error_type"]))
    status = row.get("status", row.get("runtime_admission_status"))
    if status == "blocked_dataset_conflict" or row.get("reference_admission_issues"):
        category = "dataset_conflict"
    elif status in {"case_error", "infrastructure_error"} or any(x.get("category") not in {"budget"}
                                                              for x in failures):
        category = "infrastructure_error"
    elif status == "not_run" or row.get("result") == "not_run":
        category = "not_run"
    elif any(x.get("category") == "budget" for x in failures) or any(
            run.get("runtime_diagnostic", {}).get("reason") in {"request_budget", "tool_budget", "deadline"}
            for run in row.get("runs", []) if isinstance(run.get("runtime_diagnostic"), dict)):
        category = "budget_terminated"
    elif row.get("result") == "pass":
        category = "pass"
    else:
        category = "task_failed"
    if row.get("forced_termination"):
        reasons.append("forced_termination")
    return {"category": category, "reasons": sorted(set(reasons))}


def _usage(row):
    entries = row.get("usage", [])
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        raise ValueError("Invalid saved usage")
    paid = [entry for entry in entries if any(entry.get(key) is not None for key in TOKEN_FIELDS)]
    requests = _number(row.get("model_api_calls"), integral=True)
    if requests is not None and len(paid) > requests:
        raise ValueError("Usage entries exceed recorded model requests")
    result = {"model_requests": requests, "elapsed_seconds": _number(row.get("elapsed_seconds")),
              "unknown_usage_entries": len(entries) - len(paid),
              "unknown_paid_response_usage": None if requests is None else requests - len(paid)}
    # Totals are known only when each recorded request has a value for the field;
    # missing usage entries or counters must not silently count as zero.
    for key in TOKEN_FIELDS:
        values = [_number(entry.get(key), integral=True) for entry in paid]
        result[key] = (sum(values) if values and all(value is not None for value in values)
                       and result["unknown_paid_response_usage"] == 0 else None)
    return result


def _secrets(value):
    found = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if (_SECRET_KEYS.search(key) or key.lower() == "token") and isinstance(item, str) and item:
                found.add(item)
            found.update(_secrets(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_secrets(item))
    return found


def _redact(value, secrets):
    if isinstance(value, str):
        for secret in sorted(secrets, key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, dict):
        return {key: ("[REDACTED]" if _SECRET_KEYS.search(key) or key.lower() == "token"
                      else _redact(item, secrets)) for key, item in value.items()}
    return value


def _message(message):
    if message.get("role") not in {"user", "assistant", "tool", "system"}:
        raise ValueError("Unknown message role")
    calls = [{key: call.get(key) for key in ("id", "name", "arguments")}
             for call in message.get("tool_calls", [])]
    return {"role": message["role"], "content": message.get("content", ""), "tool_calls": calls,
            "tool_call_id": message.get("tool_call_id")}


def _turns(row, expected_count, checks):
    groups = []
    for message in row.get("messages", []):
        if message.get("role") == "user":
            groups.append([])
        if not groups:
            raise ValueError("A saved transcript must begin at its first user turn")
        groups[-1].append(_message(message))
    runs = row.get("runs", [])
    if len(groups) != len(runs) or len(runs) > expected_count:
        raise ValueError("Saved turn/run counts disagree")
    results = []
    for index in range(expected_count):
        run = runs[index] if index < len(runs) else {}
        if run and run.get("turn_index") != index:
            raise ValueError("Turn indices are not ordered")
        tools = []
        for entry in row.get("tool_ledger", []):
            if entry.get("run_id") == run.get("run_id"):
                result = entry.get("result", {})
                tools.append({"tool_call_id": entry.get("tool_call_id"), "tool_name": entry.get("tool_name"),
                              "arguments": entry.get("arguments"), "state": entry.get("state"),
                              "result": {key: result.get(key) for key in ("content", "is_error", "error_category", "outcome")}})
        results.append({"turn_index": index, "messages": groups[index] if index < len(groups) else [],
                        "tool_executions": tools, "runtime_status": run.get("runtime_status", "not_run"),
                        "answer": run.get("answer"), "check": checks[index] if index < len(checks) else None})
    return results


def validate_record(record):
    """Validate a canonical projection, never reinterpret caller-supplied success."""
    if record.get("schema_version") != 1 or record.get("subcategory") not in SUBCATEGORIES:
        raise ValueError("Unsupported Multi-Turn record")
    task_id = record.get("task_id", "")
    if not isinstance(task_id, str) or not re.fullmatch(re.escape(record["subcategory"]) + r"_\d+", task_id):
        raise ValueError("Task identity does not match its subcategory")
    replay = record.get("replay", {})
    if replay != {"mode": "saved_result", "new_model_api_calls": 0, "new_judge_api_calls": 0,
                  "model_capability_measurement": False}:
        raise ValueError("Saved-result replay cannot claim new API activity or capability measurement")
    scoring = record["scoring"]
    passed = scoring["passed"]
    if passed is not None and type(passed) is not bool:
        raise ValueError("Passed must be boolean or null")
    category = record["outcome"]["category"]
    if category not in OUTCOMES:
        raise ValueError("Unknown outcome category")
    if category in {"dataset_conflict", "infrastructure_error", "not_run"}:
        if passed is not None:
            raise ValueError("Unscorable outcomes must retain unknown score")
    elif passed is not (scoring["formal_result"] == "pass"):
        raise ValueError("Formal result disagrees with saved checker decision")
    turns = record["turns"]
    if not isinstance(turns, list) or not turns:
        raise ValueError("One record must retain a complete task boundary")
    if passed and (len(scoring["turn_checks"]) != len(turns)
                   or any(check.get("valid") is not True for check in scoring["turn_checks"])
                   or any(turn.get("runtime_status") != "completed" for turn in turns)):
        raise ValueError("A pass requires every completed turn and checker result")
    if record.get("record_sha256") != _digest({key: value for key, value in record.items() if key != "record_sha256"}):
        raise ValueError("Record digest mismatch")
    return record


def _verify_sources(root, manifest):
    files = manifest.get("files", [])
    if not files:
        raise ValueError("As-run source manifest is missing")
    for entry in files:
        path = _within(root / "as-run-source", entry["file"])
        if _hash(path) != entry["sha256"] or path.stat().st_size != entry["bytes"]:
            raise ValueError("As-run source integrity mismatch")


def load_saved_campaign(campaign_dir, *, config_label=None):
    """Read one existing V4 live campaign; assert local receipt/source coherence.

    Source/receipt hashes establish local integrity, not a digital signature or
    evidence that a public benchmark was absent from model training data.
    """
    root = Path(campaign_dir).expanduser().resolve()
    source_manifest = _read(root / "source-manifest.json")
    _verify_sources(root, source_manifest)
    report = _read(root / "results/results.json")
    plan = _read(root / "campaign-plan.json")
    http = _read(root / "results/http-budget.json")
    if report.get("mode") != "live_current_model" or report.get("dataset_binding") != http.get("binding"):
        raise ValueError("Expected a bound saved live campaign")
    groups = {identifier: group for group in plan["groups"] for identifier in group["entry_ids"]}
    evidence = {name: _hash(root / name) for name in (
        "source-manifest.json", "campaign-plan.json", "results/results.json", "results/http-budget.json")}
    analysis_path = root / "analysis.json"
    analysis = _read(analysis_path) if analysis_path.is_file() else None
    if analysis is not None:
        if analysis.get("dataset_binding_recomputed") != report["dataset_binding"]:
            raise ValueError("Analysis campaign binding mismatch")
        for name, digest in evidence.items():
            if analysis.get("immutable_evidence_checks", {}).get("sha256", {}).get(name) != digest:
                raise ValueError("Saved campaign changed since independent analysis")
        evidence["analysis.json"] = _hash(analysis_path)
    profile = {key: report.get("profile", {}).get(key) for key in (
        "provider_id", "protocol", "model", "reasoning_effort", "identity_revision")}
    label = config_label or root.name
    if not isinstance(label, str) or not label.strip() or len(label) > 120 or any(ord(char) < 32 for char in label):
        raise ValueError("Configuration label must be a short nonempty display label")
    records = []
    seen = set()
    for row in report["results"]:
        identifier = row.get("id", "")
        category = next((name for name in SUBCATEGORIES if identifier.startswith(name + "_")), None)
        if category is None:
            continue
        if identifier in seen:
            raise ValueError("Duplicate complete task")
        seen.add(identifier)
        group = groups[identifier]
        if group["category"] != category or group.get("source_kind") != "official_fixed_revision_complete_task":
            raise ValueError("Unexpected task source/category")
        suffix = hashlib.sha256(identifier.encode()).hexdigest()[:24]
        receipt_path, case_path = root / f"results/receipts/{suffix}.json", root / f"results/cases/{suffix}/result.json"
        receipt = _read(receipt_path)
        case = _read(case_path) if case_path.is_file() else None
        if receipt.get("dataset_binding") != report["dataset_binding"] or receipt.get("result") != row:
            raise ValueError("Durable task receipt differs from campaign result")
        # The core result predates driver's HTTP/result fields; all core keys must match.
        if case is not None and any(row.get(key) != value for key, value in case.items()):
            raise ValueError("Core execution evidence differs from campaign result")
        if case is None and row.get("status") not in {"not_run", "case_error", "blocked_dataset_conflict"}:
            raise ValueError("Core execution evidence is missing")
        calls = [call for call in http["calls"] if call["case_id"] == identifier]
        if len(calls) != row.get("model_api_calls"):
            raise ValueError("Saved HTTP receipt and request usage disagree")
        outcome = classify_outcome(row)
        raw_scoring = row.get("scoring_verification", {})
        checks = [_checks(check) for check in raw_scoring.get("turns", [])]
        formal = row.get("result")
        scorable = outcome["category"] not in {"dataset_conflict", "infrastructure_error", "not_run"}
        passed = formal == "pass" if scorable else None
        if scorable and (type(raw_scoring.get("valid")) is not bool or raw_scoring["valid"] != passed
                         or raw_scoring.get("result") != formal):
            raise ValueError("Saved official checker and formal decision disagree")
        if passed and row.get("forced_termination"):
            raise ValueError("A forcibly stopped task cannot be passed")
        config = {"model": profile, "max_output_tokens": report.get("max_output_tokens"),
                  "max_api_calls": report.get("max_api_calls"),
                  "case_api_caps": report.get("case_api_caps"), "case_groups": report.get("case_groups"),
                  "logical_limits": next((item["budget"]["limits"] for item in report["results"]
                                           if isinstance(item.get("budget"), dict)), None)}
        source = {"source_manifest_sha256": evidence["source-manifest.json"],
                  "as_run_files_sha256": _digest(source_manifest["files"]),
                  "file_count": len(source_manifest["files"]), "integrity_verified": True,
                  "current_workspace_source_used": False}
        diagnostic = (analysis or {}).get("independent_offline_checks", {}).get(identifier)
        failures_observed = []
        for turn in (diagnostic or {}).get("turns", []):
            for key in ("irrelevance", "state_response"):
                check = turn.get(key, {})
                if check.get("valid") is False and (key == "irrelevance" or turn.get("reference_call_count", 0) > 0):
                    failures_observed.append(_code(check.get("error_type")))
        outcome["task_failure_observed"] = bool(failures_observed) if diagnostic else None
        outcome["reasons"] = sorted(set(outcome["reasons"] + ["diagnostic:" + code for code in failures_observed]))
        provenance = row.get("case_provenance", {})
        record = {"schema_version": 1, "task_id": identifier, "subcategory": category, "config_label": label,
                  "source_identity": source, "config_identity": {"sha256": _digest(config), "snapshot": config,
                       "label_is_display_metadata_only": True}, "model_identity": profile,
                  "turns": _turns(row, group["user_turns"], checks),
                  "scoring": {"passed": passed, "formal_result": formal, "turn_checks": checks,
                      "reference_admission_issues": row.get("reference_admission_issues", []),
                      "source_revision": provenance.get("revision"),
                      "scoring_rule": "bfcl_v4_pinned_official_checker",
                      "checks_are_saved_results": True, "official_score": False,
                      "diagnostic_turn_checks": diagnostic.get("turns", []) if diagnostic else [],
                      "diagnostics_do_not_replace_formal_score": True},
                  "outcome": outcome, "recorded_usage": _usage(row),
                  "replay": {"mode": "saved_result", "new_model_api_calls": 0, "new_judge_api_calls": 0,
                             "model_capability_measurement": False},
                  "provenance": {"campaign_name": root.name, "dataset_binding": report["dataset_binding"],
                      "question_sha256": provenance.get("question_sha256"), "source_kind": group["source_kind"],
                      "expected_user_turns": group["user_turns"], "executed_user_turns": len(row.get("runs", [])),
                      "evidence_sha256": {
                          **evidence, str(receipt_path.relative_to(root)): _hash(receipt_path),
                          **({str(case_path.relative_to(root)): _hash(case_path)} if case else {})},
                      "redaction": "credential argument values removed; provider items/reasoning/headers excluded"}}
        secrets = _secrets(row.get("tool_ledger", []))
        record["turns"] = _redact(record["turns"], secrets)
        record["scoring"]["reference_admission_issues"] = _redact(record["scoring"]["reference_admission_issues"], secrets)
        record["record_sha256"] = _digest(record)
        records.append(validate_record(record))
    if not records:
        raise ValueError("Campaign contains no supported Multi-Turn tasks")
    return records


def summarize_records(records):
    """Group original recorded measurements by actual config/source identity."""
    groups = defaultdict(list)
    for record in records:
        validate_record(record)
        groups[(record["config_label"], record["source_identity"]["as_run_files_sha256"],
                record["config_identity"]["sha256"])].append(record)
    summaries = []
    for (label, source, config), rows in groups.items():
        scorable = [row for row in rows if row["scoring"]["passed"] is not None]
        totals = {}
        for key in ("model_requests", *TOKEN_FIELDS, "elapsed_seconds"):
            values = [row["recorded_usage"].get(key) for row in rows]
            totals[key] = sum(values) if values and all(value is not None for value in values) else None
        summaries.append({"config_label": label, "source_sha256": source, "config_sha256": config,
                          "tasks": len(rows), "scorable_tasks": len(scorable),
                          "recorded_passed_tasks": sum(row["scoring"]["passed"] is True for row in scorable),
                          "recorded_success_rate": (sum(row["scoring"]["passed"] is True for row in scorable)
                                                    / len(scorable) if scorable else None),
                          "outcomes": dict(Counter(row["outcome"]["category"] for row in rows)),
                          "subcategories": dict(Counter(row["subcategory"] for row in rows)),
                          "failure_reasons": dict(Counter(reason for row in rows for reason in row["outcome"]["reasons"])),
                          "recorded_usage_totals": totals})
    return {"schema_version": 1, "mode": "offline_saved_result_projection", "groups": summaries,
            "new_model_api_calls": 0, "new_judge_api_calls": 0, "new_model_capability_score": None,
            "comparison_is_descriptive_only": True, "optimization_gain_computed": False,
            "note": "Different labels are not evidence that an optimization exists or was run. Different task sets/budgets are not paired controls."}


def write_bundle(records, output_dir):
    records = list(records)
    if not records:
        raise ValueError("A nonempty task bundle is required")
    identities = [(row["task_id"], row["config_label"], row["config_identity"]["sha256"]) for row in records]
    if len(identities) != len(set(identities)):
        raise ValueError("Duplicate task/configuration entry")
    summary = summarize_records(records)
    output = Path(output_dir).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / "records.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n"
                                                  for row in records), encoding="utf-8")
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest = {"schema_version": 1, "mode": "offline_saved_result_projection", "task_count": len(records),
                "files": {name: _hash(output / name) for name in ("records.jsonl", "summary.json")},
                "new_model_api_calls": 0, "new_judge_api_calls": 0}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def load_bundle(bundle_dir):
    root = Path(bundle_dir).expanduser().resolve()
    manifest = _read(root / "manifest.json")
    if manifest.get("schema_version") != 1 or manifest.get("mode") != "offline_saved_result_projection":
        raise ValueError("Unsupported saved-result bundle")
    if set(manifest.get("files", {})) != {"records.jsonl", "summary.json"}:
        raise ValueError("Bundle manifest has unexpected resources")
    for name, digest in manifest["files"].items():
        if _hash(_within(root, name)) != digest:
            raise ValueError("Bundle integrity mismatch")
    records = [json.loads(line, object_pairs_hook=_pairs) for line in (root / "records.jsonl").read_text(
        encoding="utf-8").splitlines() if line.strip()]
    if len(records) != manifest.get("task_count"):
        raise ValueError("Bundle task count mismatch")
    for row in records:
        validate_record(row)
    if _read(root / "summary.json") != summarize_records(records):
        raise ValueError("Bundle summary differs from task records")
    return records


def main(argv=None):
    parser = argparse.ArgumentParser(description="Export saved BFCL V4 Multi-Turn results; no model or judge calls")
    parser.add_argument("--campaign", action="append", required=True, help="Saved campaign directory; repeat for comparison")
    parser.add_argument("--label", action="append", help="Display label per campaign; never grants an optimized identity")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    if args.label and len(args.label) != len(args.campaign):
        parser.error("Provide one --label per --campaign, or omit all labels")
    records = []
    for index, campaign in enumerate(args.campaign):
        records.extend(load_saved_campaign(campaign, config_label=args.label[index] if args.label else None))
    print(json.dumps(write_bundle(records, args.output), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
