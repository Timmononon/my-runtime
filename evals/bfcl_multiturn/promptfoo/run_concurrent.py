"""Once-only native promptfoo dispatch of five tasks in one concurrent campaign.

Preflight reads local frozen data only. --execute admits new paid requests;
there is no campaign/whole-task API quota, retry, recovery, or automatic replay.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
import time
from collections import Counter
from copy import deepcopy
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[2]
DEFAULT_CAMPAIGN = PROJECT / "artifacts/bfcl/promptfoo-same-campaign5-live-20261030"
BACKEND_MODE = "same_campaign_task_locks_v1"
PROVIDER_LABEL = "deepseek-flash-no-thinking-concurrent"
_SPEC = importlib.util.spec_from_file_location("aster_same_campaign_existing_helpers", HERE / "run_concurrency_probe.py")
_probe = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_probe)
_base, _helpers = _probe._base, _probe._helpers
child_environment = _base.child_environment


def preflight(concurrency=5):
    plan = _probe.preflight(concurrency)
    plan.update(campaign=DEFAULT_CAMPAIGN.name, variant="promptfoo_same_campaign_concurrent_five_tasks",
        backend_mode=BACKEND_MODE, maximum_concurrency=concurrency,
        orchestrator="promptfoo 0.125.1 native CLI; one shared campaign with task-scoped backend locks",
        setting_comparison_note="Same five frozen development tasks and model settings; scheduling and backend locking change",
        scope_note="Five complete development tasks once; not holdout inference, a full leaderboard score or optimization gain",
        global_stop_policy="Shared fatal blocks new admissions/dispatch; unknown outcomes never replay",
        overlap_measurement="Task intervals from first recorded HTTP start to callback completion; not simultaneous HTTP stream measurement")
    return plan


def build_config(plan, campaign):
    if (plan.get("backend_mode") != BACKEND_MODE
            or plan.get("maximum_concurrency") != plan.get("concurrency")):
        raise ValueError("Concurrent plan identity differs")
    config = _base.build_config(plan, campaign)
    config["description"] = "Five complete Aster tasks in one task-locked concurrent campaign"
    config["providers"][0].update(id="file://" + (HERE / "concurrent_provider.py").as_posix(), label=PROVIDER_LABEL)
    config["providers"][0]["config"]["workers"] = plan["maximum_concurrency"]
    config["evaluateOptions"]["maxConcurrency"] = plan["maximum_concurrency"]
    return config


def cli_command(output, concurrency):
    if type(concurrency) is not int or not 1 <= concurrency <= 5:
        raise ValueError("Concurrency must be an integer from one to five")
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


def collect_campaign(campaign, plan, payload, output):
    """Check native output against one sealed backend campaign and its ledger."""
    from aster.evaluation.bfcl_multiturn_concurrent import read_task_receipt

    report = json.loads((campaign / "results/results.json").read_bytes())
    control = json.loads((campaign / "execution-control.json").read_bytes())
    ledger = json.loads((campaign / "results/http-budget.json").read_bytes())
    rows = report["results"]
    ids = [group["group"] for group in plan["groups"]]
    expected_turns = {group["group"]: group["user_turns"] for group in plan["groups"]}
    errors = validate_native(payload, rows)
    if ([row["id"] for row in rows] != ids or control["case_ids"] != ids
            or ledger["binding"] != control["dataset_binding"]
            or report["dataset_binding"] != control["dataset_binding"]):
        errors.append("shared_campaign_identity_mismatch")
    mode = control.get("concurrent_tasks", {})
    if (mode.get("mode") != BACKEND_MODE or mode.get("maximum") != plan["maximum_concurrency"]
            or mode.get("finalized") is not True or set(mode.get("states", {}).values()) != {"completed"}):
        errors.append("concurrent_campaign_not_sealed")
    if (control["test_only"] or ledger.get("maximum") is not None
            or report.get("max_api_calls") is not None or control.get("fatal")):
        errors.append("unexpected_test_budget_or_fatal_state")
    if (campaign / "children").exists():
        errors.append("unexpected_child_campaigns")
    control_count = len(list(campaign.rglob("execution-control.json")))
    ledger_count = len(list(campaign.rglob("http-budget.json")))
    if control_count != 1 or ledger_count != 1:
        errors.append("multiple_backend_controls_or_ledgers")
    callbacks, intervals, calls, usage, sessions = [], [], ledger["calls"], [], set()
    completed_tasks = completed_turns = 0
    classifications = Counter()
    for row in rows:
        task_id = row["id"]
        if read_task_receipt(campaign, task_id) != row:
            errors.append("durable_task_receipt_mismatch")
        key = _helpers.digest(task_id.encode())[:24]
        start_path, end_path = (output / "dispatches" / (key + suffix) for suffix in (".started.json", ".finished.json"))
        if not start_path.is_file() or not end_path.is_file():
            errors.append("missing_task_callback_evidence")
            continue
        started, ended = json.loads(start_path.read_bytes()), json.loads(end_path.read_bytes())
        local = [call for call in calls if call["case_id"] == task_id]
        if (started.get("task_id") != task_id or ended.get("task_id") != task_id
                or started.get("pid") != ended.get("pid")
                or ended.get("campaign_dir") != str(campaign.resolve())
                or ended.get("backend_mode") != BACKEND_MODE
                or ended.get("outcome") != "backend_returned" or ended.get("error_type") is not None
                or ended.get("http_attempts_for_task") != len(local)
                or row.get("http_attempts") != len(local) or row.get("model_api_calls") != len(local)):
            errors.append("task_callback_or_accounting_mismatch")
        if not (campaign / "results/intents" / (key + ".json")).is_file():
            errors.append("missing_task_intent")
        if not local or row.get("result") in {"case_error", "not_run", "unsupported", "blocked_dataset_conflict"}:
            errors.append("planned_task_not_formally_executed")
        if local:
            first = min(call["started_at"] for call in local)
            if first > ended["finished_at"]:
                errors.append("inconsistent_overlap_timestamps")
            else:
                intervals.append((first, ended["finished_at"]))
        callbacks.append(ended)
        if row.get("session_id"):
            sessions.add(row["session_id"])
        runs = row.get("runs", [])
        turns = sum(run.get("runtime_status") == "completed" for run in runs)
        completed_turns += turns
        completed_tasks += len(runs) == expected_turns[task_id] and turns == len(runs)
        usage.extend(row.get("usage", []))
        result, termination = row.get("result"), row.get("termination_kind", "")
        if result == "pass":
            classifications["pass"] += 1
        elif result == "blocked_dataset_conflict":
            classifications["data_conflict"] += 1
        elif result in {"case_error", "not_run", "unsupported"}:
            classifications["infrastructure_error"] += 1
        elif any(word in termination for word in ("limit", "budget", "deadline", "duplicate")):
            classifications["budget_termination"] += 1
        else:
            classifications["task_failure"] += 1
    if len(sessions) != len(ids):
        errors.append("missing_or_shared_runtime_session")
    if len(list((output / "dispatches").glob("*.started.json"))) != len(ids) or len(
            list((output / "dispatches").glob("*.finished.json"))) != len(ids):
        errors.append("callback_count_mismatch")
    verified_wire = sum(call.get("thinking_disabled_verified") is True
        and call.get("wire_model") == "deepseek-flash" and call.get("wire_reasoning_effort") == "none"
        and call.get("wire_max_output_tokens") == 8192 for call in calls)
    if (verified_wire != len(calls) or report.get("model_api_calls") != len(calls)
            or any(call.get("status") == "outcome_unknown" for call in calls)
            or any(call.get("case_id") not in ids for call in calls)):
        errors.append("wire_policy_or_global_accounting_mismatch")
    known = [entry for entry in usage if entry.get("input_tokens") is not None and entry.get("output_tokens") is not None]
    known_reasoning = [entry for entry in usage if entry.get("reasoning_tokens") is not None]
    if len(known) > len(calls) or len(known_reasoning) > len(calls):
        errors.append("duplicate_usage_records")
    usage_totals = {field: sum(entry[field] for entry in known if entry.get(field) is not None)
        for field in ("input_tokens", "output_tokens", "cache_hit_tokens", "cache_miss_tokens")}
    usage_totals["reasoning_tokens"] = sum(entry["reasoning_tokens"] for entry in known_reasoning)
    if any(entry["reasoning_tokens"] != 0 for entry in known_reasoning):
        errors.append("unexpected_reasoning_tokens")
    peak = _peak(intervals)
    if plan["maximum_concurrency"] > 1 and peak <= 1:
        errors.append("no_observed_task_execution_overlap")
    valid = [row for row in rows if row.get("result") in {"pass", "fail"}]
    passed = sum(row["result"] == "pass" for row in valid)
    return {"same_campaign": True, "backend_mode": BACKEND_MODE,
        "dataset_binding": control["dataset_binding"], "maximum_concurrency": plan["maximum_concurrency"],
        "backend_control_count": control_count, "http_ledger_count": ledger_count,
        "planned_tasks": len(ids), "formal_task_results": dict(Counter(row.get("result") for row in rows)),
        "completed_result_rows": len(rows), "all_five_tasks_executed": len(rows) == 5 and all(
            row.get("result") in {"pass", "fail"} and row.get("http_attempts", 0) > 0 for row in rows),
        "failure_classifications": dict(classifications), "scorable_tasks": len(valid), "passed_tasks": passed,
        "subset_success_rate": passed / len(valid) if valid else None,
        "model_api_calls": len(calls), "http_transport_attempts": len(calls),
        "http_status_codes": dict(Counter(str(call.get("status_code")) for call in calls)),
        "provider_worker_pids": sorted({row["pid"] for row in callbacks}),
        "independent_session_count": len(sessions), "complete_runtime_tasks": completed_tasks,
        "completed_user_turns": completed_turns, "defined_user_turns": sum(expected_turns.values()),
        "peak_overlapping_tasks_with_http": peak,
        "overlap_measurement": plan["overlap_measurement"], "wire_none_verified_calls": verified_wire,
        "known_usage_totals": usage_totals, "unknown_core_usage_calls": max(0, len(calls) - len(known)),
        "unknown_reasoning_usage_calls": max(0, len(calls) - len(known_reasoning)),
        "all_known_reasoning_tokens_zero": all(entry["reasoning_tokens"] == 0 for entry in known_reasoning),
        "evidence_errors": sorted(set(errors)), "judge_api_calls": 0, "official_score": False,
        "full_leaderboard_score": None, "measured_speedup": None,
        "score_note": "Actual fixed development subset, not a held-out optimization result or full BFCL score"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-dir", type=Path, default=DEFAULT_CAMPAIGN)
    parser.add_argument("--concurrency", type=int, choices=range(1, 6), default=5)
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
        print(json.dumps({"mode": "preflight_only", "tasks": 5, "same_campaign": True,
            "concurrency": args.concurrency, "model_api_calls": 0, "judge_api_calls": 0}))
        return 0
    from aster.evaluation.bfcl_current import capture_current_profile
    from aster.evaluation.bfcl_multiturn_concurrent import finalize_campaign, initialize_campaign
    from aster.evaluation.bfcl_v4_data import load_corpus
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
            _helpers.snapshot_sources(campaign)
            by_id = {case["id"]: case for case in load_corpus(_helpers.CORPUS, tracks=list(_helpers.CATEGORIES))}
            cases = [by_id[group["group"]] for group in plan["groups"]]
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
            print(json.dumps({"mode": plan["variant"], "tasks": 5, "same_campaign": True,
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
            collected["integration_verified"] = completed.returncode in {0, 100} and not collected["evidence_errors"]
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
