"""Once-only native promptfoo dispatch of forty compact100-v2 development tasks.

Preflight reads local frozen data only. --execute admits new paid requests;
there is no campaign/whole-task API quota, retry, recovery, or automatic replay.
"""
from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import platform
import shutil
import subprocess
import sys
import time
from collections import Counter
from copy import deepcopy
from pathlib import Path

from aster.evaluation.bfcl_multiturn_concurrent import PERFORMANCE_POLICY

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[2]
DEFAULT_CAMPAIGN = PROJECT / "artifacts/bfcl/compact-dev40-c20-20261030"
BACKEND_MODE = "same_campaign_task_locks_v1"
PROVIDER_LABEL = "deepseek-flash-no-thinking-concurrent"
_SPEC = importlib.util.spec_from_file_location("aster_same_campaign_existing_helpers", HERE / "run_concurrency_probe.py")
_probe = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_probe)
_base, _helpers = _probe._base, _probe._helpers
child_environment = _base.child_environment


_DATA_SPEC = importlib.util.spec_from_file_location("aster_compact_development_dataset", HERE / "compact_development_dataset.py")
_dataset = importlib.util.module_from_spec(_DATA_SPEC)
_DATA_SPEC.loader.exec_module(_dataset)
load_development_cases = _dataset.load_development_cases


def _platform_metadata():
    # Python 3.12 platform.platform() queries WMI on Windows, which can block
    # indefinitely on this host. Capture local kernel version data directly.
    if sys.platform == "win32":
        version = sys.getwindowsversion()
        return {"platform": f"Windows-{version.major}.{version.minor}.{version.build}",
            "platform_capture": {"method": "sys.getwindowsversion", "sys_platform": sys.platform,
                "wmi": False}}
    return {"platform": platform.platform(), "platform_capture": {"method": "platform.platform"}}


def snapshot_sources(campaign):
    """Snapshot reused execution sources and both immutable split authorities."""
    destination = campaign / "as-run-source"
    destination.mkdir()
    split_root = PROJECT / "evals/bfcl_multiturn/splits"
    paths = _helpers.source_files() + [
        *(split_root / "compact100-v2" / name for name in (
            "split-manifest.json", "development-ids.txt", "holdout-ids.txt")),
        *(split_root / "frozen" / name for name in (
            "split-manifest.json", "development-ids.txt", "holdout-ids.txt", "regression20-ids.txt")),
        split_root / "create_compact_split_v2.py",
    ]
    records = []
    for path in paths:
        if path.is_symlink():
            raise ValueError("Source snapshot cannot follow symlinks")
        relative, payload = path.relative_to(PROJECT), path.read_bytes()
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(payload)
        records.append({"file": relative.as_posix(), "sha256": _helpers.digest(payload), "bytes": len(payload)})
    distributions = sorted({(item.metadata.get("Name", "unknown"), item.version)
                            for item in importlib.metadata.distributions()})
    manifest = {"schema_version": 1, "captured_at": _helpers.now(), "files": records,
        "python_version": sys.version, **_platform_metadata(),
        "installed_distributions": [{"name": name, "version": version} for name, version in distributions],
        "excluded": ["user Runtime", "credentials", "cache", "*.pyc"],
        "authority": "Exact copied source bytes and frozen compact/parent split authorities."}
    _helpers.new_json(campaign / "source-manifest.json", manifest)
    return manifest


def preflight(concurrency=20):
    if type(concurrency) is not int or not 1 <= concurrency <= 100:
        raise ValueError("Concurrency must be an integer from one to 100")
    _cases, metadata = _dataset.load_development_dataset()
    return {"schema_version": 1, "campaign": DEFAULT_CAMPAIGN.name,
        "variant": "aster_bfcl_v4_compact_development40_same_campaign",
        **metadata, "corpus": _dataset.CORPUS.name,
        "planned_entries": 40, "planned_assessments": 40, "planned_budget_groups": 40,
        "backend_mode": BACKEND_MODE, "maximum_concurrency": concurrency,
        "concurrency_performance_policy": dict(PERFORMANCE_POLICY),
        "concurrency": concurrency, "python_workers": concurrency,
        "model": "deepseek-flash", "reasoning_effort": "none", "thinking_enabled": False,
        "max_output_tokens": 8192, "authorized_unlimited_api": True, "api_unlimited": True,
        "authorized_api_call_limit": None, "per_case_api_call_cap": None,
        "reserve_for_each_later_admissible_task": 0, "official_max_steps_per_user_turn": 20,
        "case_deadline_seconds": 3600, "case_tool_safety_limit": 10000,
        "inherited_runtime_safety": {"max_total_tokens_per_user_run": 300000, "duplicate_limit": 3,
            "context_tokens": 96000, "reserve_tokens": 4096, "semantic_compaction": False, "model_retries": 0},
        "provider_task_timeout_ms": 3900000, "orchestration_timeout_seconds": 14400,
        "provider_retries": 0, "engine_retries": 0, "metadata_requests": 0, "cache": False,
        "official_score": False, "full_leaderboard_score": None,
        "orchestrator": "promptfoo 0.125.1 native CLI; one campaign with task-scoped backend locks",
        "oracle_policy": "Private raw answers remain evaluator-only; promptfoo receives task IDs only",
        "budget_policy": "No API-count quota; official per-user-turn steps and recorded Runtime safety remain",
        "replay_policy": "Never resume execution or replay unknown outcomes; zero retries",
        "scope_note": "Exactly compact100-v2 forty development tasks; sixty holdout tasks are excluded",
        "baseline_policy": "A0 development baseline only when all admissible tasks are scored with intact evidence; infrastructure is separate",
        "global_stop_policy": "Shared fatal blocks new admissions/dispatch; unknown outcomes never replay",
        "overlap_measurement": "Task intervals from first recorded HTTP start to callback completion, not HTTP stream concurrency"}


def build_config(plan, campaign):
    if (plan.get("backend_mode") != BACKEND_MODE
            or plan.get("maximum_concurrency") != plan.get("concurrency")):
        raise ValueError("Concurrent plan identity differs")
    config = _base.build_config(plan, campaign)
    config["description"] = "40 frozen compact100-v2 Aster development tasks in one concurrent campaign"
    config["providers"][0].update(id="file://" + (HERE / "concurrent_provider.py").as_posix(), label=PROVIDER_LABEL)
    config["providers"][0]["config"].update(workers=plan["maximum_concurrency"], timeout=3900000)
    config["evaluateOptions"]["maxConcurrency"] = plan["maximum_concurrency"]
    return config


def cli_command(output, concurrency):
    if type(concurrency) is not int or not 1 <= concurrency <= 100:
        raise ValueError("Concurrency must be an integer from one to 100")
    return _probe.cli_command(output, concurrency)


def validate_native(payload, rows):
    normalized = deepcopy(payload)
    errors = []
    for item in normalized.get("results", {}).get("results", []):
        if item.get("provider", {}).get("label") != PROVIDER_LABEL:
            errors.append("unexpected_concurrent_provider")
        try:
            compact = json.loads(item.get("response", {}).get("output", ""))
        except (ValueError, TypeError):
            errors.append("missing_concurrent_provider_output")
            continue
        if compact.get("backend_mode") != BACKEND_MODE:
            errors.append("unexpected_concurrent_backend")
        item["provider"]["label"] = _base.PROVIDER_LABEL
    return sorted(set(errors + _base.validate_native(normalized, rows)))


def _peak(intervals):
    active = peak = 0
    for _time, change in sorted([(start, 1) for start, _end in intervals]
                                + [(end, -1) for _start, end in intervals]):
        active += change
        peak = max(peak, active)
    return peak


def _category(task_id):
    return next((category for category in _dataset.CATEGORIES if task_id.startswith(category + "_")), None)


def _classification(row):
    result = row.get("result")
    if result == "pass":
        return "pass"
    if result == "blocked_dataset_conflict":
        return "data_conflict"
    if result == "not_run":
        return "not_run"
    if result in {"case_error", "unsupported"}:
        return "infrastructure_error"
    if result == "fail":
        if _runtime_infrastructure(row):
            return "infrastructure_error"
        if (any(run.get("runtime_status") == "budget_exhausted" for run in row.get("runs", []))
                or any(failure.get("category") == "budget" for failure in row.get("model_failures", []))):
            return "budget_termination"
        termination = row.get("termination_kind", "")
        return ("budget_termination" if termination == "repeated_no_progress" or any(word in termination for word in
            ("limit", "budget", "deadline", "duplicate")) else "task_failure")
    return "infrastructure_error"


def _runtime_infrastructure(row):
    """Keep typed Runtime exceptions separate from failed Agent decisions."""
    if row.get("error_type"):
        return True
    return any(run.get("runtime_status") == "failed"
        and (run.get("runtime_diagnostic") or {}).get("error_type")
        for run in row.get("runs", []))


def _scorable(row):
    return (row.get("result") in {"pass", "fail"}
        and _classification(row) in {"pass", "task_failure", "budget_termination"})


def _rate(passed, total):
    return passed / total if total else None


def collect_campaign(campaign, plan, payload, output, *, expected_model="deepseek-flash",
                     native_validator=None, receipt_reader=None, classification=None, runtime_infrastructure=None):
    """Check all forty native outcomes; data conflicts are null, never model zeros."""
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
    if (len(ids) != 40 or len(set(ids)) != 40 or [row["id"] for row in rows] != ids
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
        "allocated_development_tasks": 40, "allocated_holdout_tasks": 60, "holdout_inference_tasks": 0, "planned_tasks": len(ids),
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
        "score_note": "Frozen public development-set Aster baseline; not a full BFCL score or held-out optimization gain",
        "baseline_assessment": {"kind": "A0_frozen_development_agent_configuration",
            "capability_baseline_ready": capability_ready,
            "usage_baseline_ready": capability_ready and unknown_usage == unknown_reasoning == 0,
            "readiness_limitations": readiness_reasons,
            "budget_terminations": sum(classify(row) == "budget_termination" for row in rows),
            "scope": "Same frozen tasks/model/thinking/safety/scoring/concurrency for future paired development comparison",
            "single_run_only": True, "holdout_validation_performed": False,
            "model_pretraining_exposure": "unknown; public benchmark, not proof of unseen model capability"},
        "tasks": task_details}
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-dir", type=Path, default=DEFAULT_CAMPAIGN)
    parser.add_argument("--concurrency", type=int, choices=range(1, 101), default=20)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    campaign, plan = args.campaign_dir.resolve(), preflight(args.concurrency)
    plan["campaign"] = campaign.name
    campaign.mkdir(parents=True, exist_ok=True)
    if any((campaign / name).exists() for name in ("execution-started.json", "source-manifest.json", "results", "promptfoo")):
        parser.error("Campaign already started; resuming or replaying is forbidden")
    path = campaign / "campaign-plan.json"
    if path.exists():
        if json.loads(path.read_bytes()) != plan:
            parser.error("Saved concurrent plan differs")
    else:
        _helpers.new_json(path, plan)
    if not args.execute:
        print(json.dumps({"mode": "preflight_only", "tasks": 40, "same_campaign": True,
            "concurrency": args.concurrency, "model_api_calls": 0, "judge_api_calls": 0}))
        return 0
    from aster.evaluation.bfcl_current import capture_current_profile
    from aster.evaluation.bfcl_multiturn_concurrent import finalize_campaign, initialize_campaign
    from aster.paths import resolve_data_dir
    from aster.platform.filesystem import FileLock

    with FileLock(campaign / "launch.lock", timeout=.1):
        started = time.monotonic()
        _helpers.new_json(campaign / "execution-started.json", {"mode": plan["variant"],
            "started_at": _helpers.now(), "authorized_unlimited_api": True, "api_call_limit": None,
            "backend_mode": BACKEND_MODE, "maximum_concurrency": args.concurrency,
            "campaign_plan_sha256": _helpers.digest(_helpers.encoded(plan)),
            "launcher_sha256": _helpers.digest(Path(__file__).read_bytes())})
        try:
            snapshot_sources(campaign)
            cases = load_development_cases()
            if [case["id"] for case in cases] != [group["group"] for group in plan["groups"]]:
                raise ValueError("Development cases differ from the frozen execution order")
            profile = capture_current_profile(resolve_data_dir(), provider_id="deepseek",
                model="deepseek-flash", reasoning_effort="none")
            if profile.protocol != "deepseek_responses" or profile.model != "deepseek-flash" or profile.reasoning_effort != "none":
                raise ValueError("The requested native DeepSeek no-thinking profile is unavailable")
            public = profile.public_summary()
            public = {key: public[key] for key in ("provider_id", "provider_name", "base_url", "protocol", "model",
                "reasoning_effort", "identity_revision", "capabilities") if key in public}
            public["capabilities"] = {key: public.get("capabilities", {})[key] for key in (
                "id", "name", "context_window", "max_output_tokens", "input_modalities", "effort_levels")
                if key in public.get("capabilities", {})}
            _helpers.new_json(campaign / "model-selection.json", public)
            control = initialize_campaign(cases, campaign, profile=profile, maximum_concurrency=args.concurrency,
                max_api_calls=None, max_output_tokens=8192, authorized_unlimited_api=True)
            output = campaign / "promptfoo"
            (output / "dispatches").mkdir(parents=True)
            _helpers.new_json(output / "promptfooconfig.json", build_config(plan, campaign))
            shutil.copyfile(HERE / "no_thinking_assertion.py", output / "no_thinking_assertion.py")
            (output / "empty.env").write_text("", encoding="utf-8")
            cli = PROJECT / ".eval-tools/promptfoo/node_modules/promptfoo/dist/src/entrypoint.js"
            if json.loads((cli.parents[2] / "package.json").read_bytes())["version"] != "0.125.1":
                raise ValueError("This launcher requires pinned promptfoo 0.125.1")
            print(json.dumps({"mode": plan["variant"], "tasks": 40, "same_campaign": True,
                "concurrency": args.concurrency, "model": "deepseek-flash", "reasoning_effort": "none"}), flush=True)
            env = child_environment(output, control)
            env["PROMPTFOO_PYTHON_WORKERS"] = str(args.concurrency)
            completed = subprocess.run(cli_command(output, args.concurrency), cwd=output, env=env,
                text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=14400, check=False)
            (output / "cli.stdout.txt").write_text(completed.stdout, encoding="utf-8")
            (output / "cli.stderr.txt").write_text(completed.stderr, encoding="utf-8")
            finalize_campaign(campaign)
            collected = collect_campaign(campaign, plan, json.loads((output / "results.json").read_bytes()), output)
            guards = [json.loads(path.read_bytes()) for path in output.glob("*-guard.*.json")]
            blocked = sum(row.get("blocked_attempts", row.get("external_attempts", 0)) for row in guards)
            roles = {row.get("entry_script") for row in guards}
            if not any("external_attempts" in row for row in guards) or not {"persistent_wrapper.py", "wrapper.py"} <= roles:
                collected["evidence_errors"].append("missing_node_provider_or_assertion_guards")
            if blocked:
                collected["evidence_errors"].append("endpoint_or_process_guard_violation")
            collected.update(finished_at=_helpers.now(), elapsed_seconds=round(time.monotonic() - started, 4),
                native_exit_code=completed.returncode, guard_blocked_attempts=blocked,
                authorized_unlimited_api=True, api_call_limit=None, model="deepseek-flash", reasoning_effort="none")
            collected["evidence_integrity_verified"] = completed.returncode in {0, 100} and not collected["evidence_errors"]
            collected["integration_verified"] = (collected["evidence_integrity_verified"]
                and collected["baseline_assessment"]["capability_baseline_ready"])
            _helpers.new_json(campaign / "report.json", collected)
            _helpers.new_json(campaign / "execution-finished.json", collected)
            print(json.dumps(collected), flush=True)
            return 0 if collected["integration_verified"] else 2
        except BaseException as error:
            _helpers.new_json(campaign / "execution-failure.json", {"finished_at": _helpers.now(),
                "error_type": type(error).__name__, "elapsed_seconds": round(time.monotonic() - started, 4),
                "automatic_replay": False})
            raise


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - never expose credential/provider prose.
        print(json.dumps({"concurrent_launcher_error_type": type(error).__name__, "automatic_replay": False}), file=sys.stderr)
        raise SystemExit(2) from None
