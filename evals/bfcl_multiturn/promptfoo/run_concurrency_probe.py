"""Try promptfoo-only concurrency against one unchanged five-task campaign.

Default preflight is offline. Only --execute captures credentials and makes
authorized model calls; callback/backend errors are observations, not scores.
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
DEFAULT_CAMPAIGN = PROJECT / "artifacts/bfcl/promptfoo-concurrency5-probe-20261030"
FROZEN = PROJECT / "evals/bfcl_multiturn/splits/frozen"
_SPEC = importlib.util.spec_from_file_location("aster_probe_no_thinking_helpers", HERE / "run_no_thinking.py")
_base = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_base)
_helpers = _base._helpers


def preflight(concurrency=5):
    if type(concurrency) is not int or not 1 <= concurrency <= 5:
        raise ValueError("Probe concurrency must be an integer from one to five")
    plan = _base.preflight()
    split = json.loads((FROZEN / "split-manifest.json").read_bytes())
    payloads = {name: (FROZEN / name).read_bytes() for name in ("development-ids.txt", "regression20-ids.txt")}
    for name, payload in payloads.items():
        if _helpers.digest(payload) != split["id_files"][name]["sha256"]:
            raise ValueError("Frozen development/regression membership changed")
    regression = payloads["regression20-ids.txt"].decode().splitlines()
    development = set(payloads["development-ids.txt"].decode().splitlines())
    if (split["seed"] != 20261030 or split["source_revision"] != plan["source_revision"]
            or regression != [group["group"] for group in plan["groups"]]
            or not set(regression) <= development):
        raise ValueError("The probe must use the previously run frozen development tasks")
    conflicts = set(plan["known_admission_conflicts"])
    groups = [group for group in plan["groups"] if group["group"] not in conflicts][:5]
    if len(groups) != 5 or {group["category"] for group in groups} != set(_helpers.CATEGORIES):
        raise ValueError("The five-task probe must retain all four Multi-Turn categories")
    plan.update(campaign=DEFAULT_CAMPAIGN.name, variant="promptfoo_only_same_campaign_concurrency_probe",
        groups=groups, planned_entries=5, planned_assessments=5, planned_budget_groups=5,
        concurrency=concurrency, python_workers=concurrency,
        known_admission_conflicts=[], excluded_known_conflicts=sorted(conflicts),
        split_manifest_sha256=_helpers.digest((FROZEN / "split-manifest.json").read_bytes()),
        development_ids_sha256=_helpers.digest(payloads["development-ids.txt"]),
        regression20_ids_sha256=_helpers.digest(payloads["regression20-ids.txt"]),
        order_policy="First five non-conflict tasks in frozen regression20 order; no outcome-based selection",
        orchestrator=f"promptfoo 0.125.1; same unchanged backend campaign; requested concurrency {concurrency}",
        setting_comparison_note="Only promptfoo scheduling changes; backend locks and serial admission are unchanged",
        scope_note="Five development tasks, each dispatched once; no retries or automatic replay; this is a concurrency experiment",
        capability_score=None)
    return plan


def build_config(plan, campaign):
    config = _base.build_config(plan, campaign)
    config["description"] = "Aster five-task promptfoo-only same-campaign concurrency probe"
    provider = config["providers"][0]
    provider["id"] = "file://" + (HERE / "probe_provider.py").as_posix()
    provider["config"].update(workers=plan["concurrency"],
        probe_telemetry_dir=(campaign / "promptfoo/dispatches").as_posix())
    config["evaluateOptions"]["maxConcurrency"] = plan["concurrency"]
    return config


def cli_command(output, concurrency):
    cli = PROJECT / ".eval-tools/promptfoo/node_modules/promptfoo/dist/src/entrypoint.js"
    return [shutil.which("node"), str(cli), "eval", "--config", str(output / "promptfooconfig.json"),
        "--env-file", str(output / "empty.env"), "--output", str(output / "results.json"),
        "--no-cache", "--no-share", "--no-write", "--no-progress-bar", "--no-table",
        "--max-concurrency", str(concurrency)]


def peak_callbacks(finished):
    events = [(row["started_at"], 1) for row in finished] + [(row["finished_at"], -1) for row in finished]
    active = peak = 0
    for _timestamp, change in sorted(events):
        active += change
        peak = max(peak, active)
    return peak


def analyze_native(payload, report, plan, output):
    """Separate observed dispatch errors from missing/corrupted evidence."""
    expected = {group["group"] for group in plan["groups"]}
    native = payload.get("results", {}).get("results", [])
    rows = {row["id"]: row for row in report["results"]}
    errors, seen, observations, normal = [], set(), [], []
    finished = [json.loads(path.read_bytes()) for path in (output / "dispatches").glob("*.finished.json")]
    started = [json.loads(path.read_bytes()) for path in (output / "dispatches").glob("*.started.json")]
    if (len(native) != 5 or len(rows) != 5 or set(rows) != expected
            or len(started) != 5 or len(finished) != 5
            or {row["task_id"] for row in started} != expected
            or {row["task_id"] for row in finished} != expected):
        errors.append("missing_or_duplicate_native_or_telemetry_rows")
    by_finished = {row["task_id"]: row for row in finished}
    for item in native:
        task_id = item.get("vars", {}).get("task_id")
        if task_id not in expected or task_id in seen:
            errors.append("duplicate_or_unknown_native_task")
            continue
        seen.add(task_id)
        if item.get("provider", {}).get("label") != _base.PROVIDER_LABEL:
            errors.append("unexpected_provider_label")
        try:
            compact = json.loads(item.get("response", {}).get("output", ""))
        except (ValueError, TypeError):
            errors.append("missing_compact_provider_output")
            continue
        telemetry = by_finished.get(task_id, {})
        if compact.get("result") == "probe_dispatch_error":
            kind = compact.get("probe_error_kind")
            if (compact.get("id") != task_id or compact.get("main_score") is not None
                    or compact.get("model") != "deepseek-flash" or compact.get("reasoning_effort") != "none"
                    or compact.get("api_count_limit", "missing") is not None
                    or telemetry.get("probe_error_kind") != kind
                    or telemetry.get("error_type") != compact.get("error_type")
                    or not item.get("error")):
                errors.append("probe_exception_evidence_mismatch")
            attempts = sum(call["case_id"] == task_id for call in report["http_calls"])
            if kind in {"backend_campaign_lock_busy", "backend_serial_order_rejected"} and attempts:
                errors.append("admission_rejection_has_http_attempts")
            observations.append({"task_id": task_id, "kind": kind, "error_type": compact.get("error_type"),
                                 "http_attempts": attempts, "main_score": None})
        else:
            normal.append(item)
            if telemetry.get("outcome") != "backend_returned":
                errors.append("normal_response_has_exception_telemetry")
    if seen != expected:
        errors.append("missing_native_tasks")
    normal_ids = {item["vars"]["task_id"] for item in normal}
    filtered = {"results": {"results": deepcopy(normal), "stats": {
        "errors": sum(rows[task_id].get("result") not in {"pass", "fail"} for task_id in normal_ids)}}}
    errors += _base.validate_native(filtered, [rows[task_id] for task_id in normal_ids])
    native_error_count = sum(bool(item.get("error")) for item in native)
    if payload.get("results", {}).get("stats", {}).get("errors") != native_error_count:
        errors.append("native_error_count_mismatch")
    return {"evidence_errors": sorted(set(errors)), "observed_dispatch_errors": observations,
        "provider_callbacks": len(started), "provider_callbacks_finished": len(finished),
        "provider_worker_pids": sorted({row["pid"] for row in started}),
        "peak_overlapping_provider_callbacks": peak_callbacks(finished),
        "backend_returned_task_count": len(normal),
        "tasks_with_http": sum(any(call["case_id"] == task_id for call in report["http_calls"]) for task_id in expected),
        "formal_task_results": dict(Counter(row.get("result") for row in report["results"])),
        "all_five_tasks_executed": all(rows[task_id].get("result") in {"pass", "fail"} for task_id in expected),
        "capability_score": None, "http_concurrency_measurement": "Ledger has start times only; no HTTP overlap claim"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-dir", type=Path, default=DEFAULT_CAMPAIGN)
    parser.add_argument("--concurrency", type=int, choices=range(1, 6), default=5)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    campaign = args.campaign_dir.resolve()
    plan = preflight(args.concurrency)
    plan["campaign"] = campaign.name
    campaign.mkdir(parents=True, exist_ok=True)
    if any((campaign / name).exists() for name in ("execution-started.json", "source-manifest.json", "results", "promptfoo")):
        parser.error("Campaign execution already exists; replay is forbidden")
    plan_path = campaign / "campaign-plan.json"
    if plan_path.exists():
        if json.loads(plan_path.read_bytes()) != plan:
            parser.error("Saved probe plan differs")
    else:
        _helpers.new_json(plan_path, plan)
    if not args.execute:
        print(json.dumps({"mode": "preflight_only", "tasks": 5, "concurrency": args.concurrency,
            "model": "deepseek-flash", "reasoning_effort": "none", "model_api_calls": 0, "judge_api_calls": 0}))
        return 0
    from aster.evaluation.bfcl_current import capture_current_profile
    from aster.evaluation.bfcl_multiturn_unmetered import finalize_campaign, initialize_campaign
    from aster.evaluation.bfcl_v4_data import load_corpus
    from aster.paths import resolve_data_dir
    from aster.platform.filesystem import FileLock

    with FileLock(campaign / "launch.lock", timeout=.1):
        started = time.monotonic()
        _helpers.new_json(campaign / "execution-started.json", {"mode": plan["variant"],
            "started_at": _helpers.now(), "authorized_unlimited_api": True,
            "campaign_plan_sha256": _helpers.digest(_helpers.encoded(plan)),
            "launcher_sha256": _helpers.digest(Path(__file__).read_bytes())})
        try:
            _helpers.snapshot_sources(campaign)
            by_id = {case["id"]: case for case in load_corpus(_helpers.CORPUS, tracks=list(_helpers.CATEGORIES))}
            cases = [by_id[group["group"]] for group in plan["groups"]]
            profile = capture_current_profile(resolve_data_dir(), provider_id="deepseek",
                model="deepseek-flash", reasoning_effort="none")
            public = profile.public_summary()
            public = {key: public[key] for key in ("provider_id", "provider_name", "base_url", "protocol", "model",
                "reasoning_effort", "identity_revision", "capabilities") if key in public}
            public["capabilities"] = {key: public.get("capabilities", {})[key] for key in (
                "id", "name", "context_window", "max_output_tokens", "input_modalities", "effort_levels")
                if key in public.get("capabilities", {})}
            _helpers.new_json(campaign / "model-selection.json", public)
            control = initialize_campaign(cases, campaign, profile=profile, max_api_calls=None,
                max_output_tokens=8192, authorized_unlimited_api=True)
            output = campaign / "promptfoo"
            (output / "dispatches").mkdir(parents=True)
            _helpers.new_json(output / "promptfooconfig.json", build_config(plan, campaign))
            shutil.copyfile(HERE / "no_thinking_assertion.py", output / "no_thinking_assertion.py")
            (output / "empty.env").write_text("", encoding="utf-8")
            command = cli_command(output, args.concurrency)
            package = PROJECT / ".eval-tools/promptfoo/node_modules/promptfoo/package.json"
            if json.loads(package.read_bytes())["version"] != "0.125.1":
                raise ValueError("The probe requires pinned promptfoo 0.125.1")
            _helpers.new_json(campaign / "promptfoo-command.json", {"argv": command})
            print(json.dumps({"mode": plan["variant"], "tasks": 5, "concurrency": args.concurrency,
                "model": "deepseek-flash", "reasoning_effort": "none", "api_call_limit": None}), flush=True)
            completed = subprocess.run(command, cwd=output, env=_base.child_environment(output, control),
                text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=14400, check=False)
            (output / "cli.stdout.txt").write_text(completed.stdout, encoding="utf-8")
            (output / "cli.stderr.txt").write_text(completed.stderr, encoding="utf-8")
            report = finalize_campaign(campaign)
            ledger = json.loads((campaign / "results/http-budget.json").read_bytes())
            analysis = analyze_native(json.loads((output / "results.json").read_bytes()),
                {**report, "http_calls": ledger["calls"]}, plan, output)
            guard_files = list(output.glob("*-guard.*.json"))
            guards = [json.loads(path.read_bytes()) for path in guard_files]
            blocked = sum(row.get("blocked_attempts", row.get("external_attempts", 0)) for row in guards)
            if not any(path.name.startswith("node-guard.") for path in guard_files) or not any(
                    row.get("entry_script") == "persistent_wrapper.py" for row in guards):
                analysis["evidence_errors"].append("missing_node_or_provider_guard")
            if blocked:
                analysis["evidence_errors"].append("endpoint_or_process_guard_violation")
            receipt = {"finished_at": _helpers.now(), "elapsed_seconds": round(time.monotonic() - started, 4),
                "orchestrator": "promptfoo", "native_exit_code": completed.returncode,
                "planned_entries": 5, "requested_concurrency": args.concurrency,
                "same_campaign_backend_unchanged": True, "model": "deepseek-flash", "reasoning_effort": "none",
                "authorized_unlimited_api": True, "api_call_limit": None,
                "model_api_calls": report["model_api_calls"], "http_transport_attempts": report["http_transport_attempts"],
                "guard_blocked_attempts": blocked, "judge_api_calls": 0,
                "official_score": False, "full_leaderboard_score": None, **analysis}
            receipt["probe_evidence_valid"] = completed.returncode in {0, 100} and not analysis["evidence_errors"]
            receipt["promptfoo_only_same_campaign_parallelism_succeeded"] = (
                receipt["probe_evidence_valid"] and analysis["all_five_tasks_executed"]
                and analysis["peak_overlapping_provider_callbacks"] > 1)
            _helpers.new_json(campaign / "execution-finished.json", receipt)
            print(json.dumps(receipt), flush=True)
            return 0 if receipt["probe_evidence_valid"] else 2
        except BaseException as error:
            _helpers.new_json(campaign / "execution-failure.json", {"finished_at": _helpers.now(),
                "error_type": type(error).__name__, "elapsed_seconds": round(time.monotonic() - started, 4),
                "automatic_replay": False})
            raise


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - never emit raw provider exceptions.
        print(json.dumps({"probe_launcher_error_type": type(error).__name__, "automatic_replay": False}), file=sys.stderr)
        raise SystemExit(2) from None
