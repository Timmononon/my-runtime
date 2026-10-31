"""Run five development tasks with promptfoo-owned single-task campaigns.

The Aster backend, runner, tool state, scoring, and per-turn limits are reused
unchanged. Preflight is offline; only --execute authorizes new model requests.
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
DEFAULT_CAMPAIGN = PROJECT / "artifacts/bfcl/promptfoo-task-isolated5-parallel-20261030"


def _load(name):
    spec = importlib.util.spec_from_file_location("aster_task_isolated_" + name, HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_probe, _router = (_load(name) for name in ("run_concurrency_probe", "task_isolated_provider"))
_base, _helpers = _probe._base, _probe._helpers


def preflight(concurrency=5):
    plan = _probe.preflight(concurrency)
    plan.update(campaign=DEFAULT_CAMPAIGN.name, variant="promptfoo_task_isolated_five_task_experiment",
        planned_backend_campaigns=5,
        task_routes={group["group"]: _router.child_relative(group["group"]) for group in plan["groups"]},
        orchestrator="One promptfoo provider routes each whole task to its own unchanged Aster campaign",
        scope_note="Independent five-task experiment; no phase-one result recovery, retry, or holdout inference",
        setting_comparison_note="Serial/parallel use identical five complete tasks and backend settings; only scheduling differs",
        global_stop_policy="A child fatal or unknown callback outcome halts later admissions; admitted tasks drain normally")
    return plan


def initialize_children(batch, plan, cases, profile):
    from aster.evaluation.bfcl_multiturn_unmetered import initialize_campaign

    ids = [group["group"] for group in plan["groups"]]
    if [case["id"] for case in cases] != ids:
        raise ValueError("Child inputs must match the frozen parent task order")
    parent_plan_hash = _helpers.digest((batch / "campaign-plan.json").read_bytes())
    source_payload = (batch / "source-manifest.json").read_bytes()
    source_hash = _helpers.digest(source_payload)
    routes, states = {}, []
    for group, case in zip(plan["groups"], cases, strict=True):
        task_id = case["id"]
        relative = _router.child_relative(task_id)
        if plan["task_routes"][task_id] != relative:
            raise ValueError("Child route differs from the frozen batch plan")
        child = batch / relative
        child.mkdir(parents=True, exist_ok=False)
        child_plan = deepcopy(plan)
        child_plan.update(campaign=child.name, groups=[group], planned_entries=1, planned_assessments=1,
            planned_budget_groups=1, parent_batch_plan_sha256=parent_plan_hash, task_routes={task_id: relative})
        _helpers.new_json(child / "campaign-plan.json", child_plan)
        (child / "source-manifest.json").write_bytes(source_payload)
        shutil.copytree(batch / "as-run-source", child / "as-run-source")
        state = initialize_campaign([case], child, profile=profile, max_api_calls=None,
            max_output_tokens=8192, authorized_unlimited_api=True)
        states.append(state)
        routes[task_id] = {"relative_path": relative, "dataset_binding": state["dataset_binding"],
            "plan_sha256": state["campaign-plan.json_sha256"], "source_manifest_sha256": source_hash}
    if len({state["profile_guard"] for state in states}) != 1:
        raise ValueError("All child campaigns must share one captured provider/model identity")
    control = {"schema_version": 1, "task_ids": ids, "routes": routes,
        "plan_sha256": parent_plan_hash, "source_manifest_sha256": source_hash,
        "profile_guard": states[0]["profile_guard"], "model": "deepseek-flash", "reasoning_effort": "none",
        "authorized_unlimited_api": True, "api_call_limit": None, "automatic_replay": False}
    (batch / "callback-intents").mkdir()
    (batch / "promptfoo/dispatches").mkdir(parents=True)
    _router.write_new(batch / "batch-control.json", control)
    return control, states[0]


def build_config(plan, batch, control_hash):
    config = _base.build_config(plan, batch)
    config["description"] = "Five complete BFCL tasks, promptfoo task-isolated campaign routing"
    provider = config["providers"][0]
    provider["id"] = "file://" + (HERE / "task_isolated_provider.py").as_posix()
    provider["config"].pop("campaign_dir")
    provider["config"].update(batch_dir=batch.as_posix(), batch_control_sha256=control_hash,
        workers=plan["concurrency"])
    config["evaluateOptions"]["maxConcurrency"] = plan["concurrency"]
    for test in config["tests"]:
        task_id = test["vars"]["task_id"]
        test["metadata"]["campaign_dir"] = (batch / plan["task_routes"][task_id]).as_posix()
    return config


def _source_identity_errors(batch, child, manifest):
    for entry in manifest["files"]:
        relative = Path(entry["file"])
        if relative.is_absolute() or ".." in relative.parts:
            return ["unsafe_source_snapshot_path"]
        for base in (PROJECT, batch / "as-run-source", child / "as-run-source"):
            if _helpers.digest((base / relative).read_bytes()) != entry["sha256"]:
                return ["source_snapshot_identity_changed"]
    return []


def collect_batch(batch, plan, payload, control_hash):
    from aster.evaluation.bfcl_multiturn_unmetered import finalize_campaign

    control = _router.read_control(batch, control_hash)
    source_manifest = json.loads((batch / "source-manifest.json").read_bytes())
    rows, all_calls, child_summaries, errors, usage = [], [], [], [], []
    expected_turns = {group["group"]: group["user_turns"] for group in plan["groups"]}
    for task_id in control["task_ids"]:
        child, _ = _router.route_child(batch, control, task_id)
        report = finalize_campaign(child)
        state = json.loads((child / "execution-control.json").read_bytes())
        ledger = json.loads((child / "results/http-budget.json").read_bytes())
        if (len(report["results"]) != 1 or report["results"][0]["id"] != task_id
                or ledger["binding"] != control["routes"][task_id]["dataset_binding"]
                or ledger["maximum"] is not None or any(call["case_id"] != task_id for call in ledger["calls"])):
            errors.append("child_result_or_ledger_identity_changed")
        errors.extend(_source_identity_errors(batch, child, source_manifest))
        row = report["results"][0]
        receipt_count = len(list((child / "results/receipts").glob("*.json")))
        intent_count = len(list((child / "results/intents").glob("*.json")))
        if (row.get("model_api_calls", 0) != len(ledger["calls"])
                or row["result"] in {"pass", "fail"} and (receipt_count != 1 or intent_count != 1)):
            errors.append("child_request_or_execution_count_mismatch")
        rows.append(row)
        all_calls.extend(ledger["calls"])
        usage.extend(row.get("usage", []))
        runs = row.get("runs", [])
        child_summaries.append({"task_id": task_id, "child_relative_path": control["routes"][task_id]["relative_path"],
            "dataset_binding": report["dataset_binding"], "result": row["result"],
            "termination_kind": row.get("termination_kind"), "session_id": row.get("session_id"),
            "expected_user_turns": expected_turns[task_id], "entered_user_turns": len(runs),
            "completed_user_turns": sum(run["runtime_status"] == "completed" for run in runs),
            "all_user_turns_completed": len(runs) == expected_turns[task_id] and all(
                run["runtime_status"] == "completed" for run in runs),
            "model_api_calls": report["model_api_calls"], "fatal": state.get("fatal"),
            "receipt_count": receipt_count, "execution_intent_count": intent_count,
            "source_files_verified": len(source_manifest["files"])})
    analysis = _probe.analyze_native(payload, {"results": rows, "http_calls": all_calls}, plan, batch / "promptfoo")
    errors.extend(analysis["evidence_errors"])
    finished = [json.loads(path.read_bytes()) for path in (batch / "promptfoo/dispatches").glob("*.finished.json")]
    by_finished = {item["task_id"]: item for item in finished}
    intervals = []
    for task_id in control["task_ids"]:
        calls = [call for call in all_calls if call["case_id"] == task_id]
        if calls and task_id in by_finished:
            intervals.append({"task_id": task_id, "started_at": min(call["started_at"] for call in calls),
                              "finished_at": by_finished[task_id]["finished_at"]})
    sessions = [row.get("session_id") for row in rows if row.get("session_id")]
    if len(sessions) != len(set(sessions)):
        errors.append("session_identity_reused_across_tasks")
    if len({entry["dataset_binding"] for entry in child_summaries}) != 5:
        errors.append("child_campaign_binding_reused")
    fields = ("input_tokens", "output_tokens", "reasoning_tokens", "cache_hit_tokens", "cache_miss_tokens")
    known_usage = [entry for entry in usage if all(type(entry.get(key)) is int for key in ("input_tokens", "output_tokens"))]
    unknown_core = len(all_calls) - len(known_usage)
    unknown_reasoning = len(all_calls) - sum(type(entry.get("reasoning_tokens")) is int for entry in usage)
    if unknown_core < 0 or unknown_reasoning < 0:
        errors.append("usage_rows_exceed_http_attempts")
    return {**analysis, "evidence_errors": sorted(set(errors)), "results": rows, "children": child_summaries,
        "model_api_calls": len(all_calls), "http_transport_attempts": len(all_calls),
        "http_status_counts": dict(Counter(str(call.get("status_code")) for call in all_calls)),
        "unknown_http_outcomes": sum(call.get("status") == "outcome_unknown" for call in all_calls),
        "wire_none_verified_calls": sum(call.get("thinking_disabled_verified") is True
            and call.get("wire_model") == "deepseek-flash" and call.get("wire_reasoning_effort") == "none"
            and call.get("wire_max_output_tokens") == 8192 for call in all_calls),
        "known_usage_totals": {field: sum(entry[field] for entry in usage if type(entry.get(field)) is int) for field in fields},
        "unknown_core_usage_calls": unknown_core, "unknown_reasoning_usage_calls": unknown_reasoning,
        "all_known_reasoning_tokens_zero": bool(all_calls) and unknown_reasoning == 0 and all(
            entry.get("reasoning_tokens") == 0 for entry in usage),
        "complete_runtime_tasks": sum(entry["all_user_turns_completed"] for entry in child_summaries),
        "independent_session_count": len(sessions),
        "task_execution_intervals_with_http": intervals,
        "peak_overlapping_tasks_with_http": _probe.peak_callbacks(intervals),
        "overlap_definition": "Task lifetime from first HTTP dispatch to provider return; not simultaneous HTTP request count",
        "runtime_isolation": "Unchanged core creates a fresh temporary Runtime and simulator per task; unique sessions verified",
        "global_halt_recorded": (batch / "batch-halt.json").exists(),
        "capability_score": None, "official_score": False, "full_leaderboard_score": None,
        "judge_api_calls": 0, "metadata_requests": 0, "model_retries": 0}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-dir", type=Path, default=DEFAULT_CAMPAIGN)
    parser.add_argument("--concurrency", type=int, choices=range(1, 6), default=5)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    batch = args.campaign_dir.resolve()
    plan = preflight(args.concurrency)
    plan["campaign"] = batch.name
    batch.mkdir(parents=True, exist_ok=True)
    if any((batch / name).exists() for name in ("execution-started.json", "source-manifest.json", "children", "promptfoo")):
        parser.error("Batch execution already exists; recovery/replay is forbidden")
    plan_path = batch / "campaign-plan.json"
    if plan_path.exists():
        if json.loads(plan_path.read_bytes()) != plan:
            parser.error("Saved isolated-task plan differs")
    else:
        _helpers.new_json(plan_path, plan)
    if not args.execute:
        print(json.dumps({"mode": "preflight_only", "tasks": 5, "independent_campaigns": 5,
            "concurrency": args.concurrency, "model_api_calls": 0, "judge_api_calls": 0}))
        return 0
    from aster.evaluation.bfcl_current import capture_current_profile
    from aster.evaluation.bfcl_v4_data import load_corpus
    from aster.paths import resolve_data_dir
    from aster.platform.filesystem import FileLock

    with FileLock(batch / "launch.lock", timeout=.1):
        started = time.monotonic()
        _helpers.new_json(batch / "execution-started.json", {"started_at": _helpers.now(),
            "authorized_unlimited_api": True, "campaign_plan_sha256": _helpers.digest(_helpers.encoded(plan)),
            "launcher_sha256": _helpers.digest(Path(__file__).read_bytes()), "automatic_replay": False})
        try:
            _helpers.snapshot_sources(batch)
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
            _helpers.new_json(batch / "model-selection.json", public)
            _control, first_child_state = initialize_children(batch, plan, cases, profile)
            control_hash = _helpers.digest((batch / "batch-control.json").read_bytes())
            output = batch / "promptfoo"
            _helpers.new_json(output / "promptfooconfig.json", build_config(plan, batch, control_hash))
            shutil.copyfile(HERE / "no_thinking_assertion.py", output / "no_thinking_assertion.py")
            (output / "empty.env").write_text("", encoding="utf-8")
            command = _probe.cli_command(output, args.concurrency)
            package = PROJECT / ".eval-tools/promptfoo/node_modules/promptfoo/package.json"
            if json.loads(package.read_bytes())["version"] != "0.125.1":
                raise ValueError("This launcher requires pinned promptfoo 0.125.1")
            _helpers.new_json(batch / "promptfoo-command.json", {"argv": command})
            env = _base.child_environment(output, first_child_state)
            env["PROMPTFOO_PYTHON_WORKERS"] = str(args.concurrency)
            prepared = time.monotonic()
            print(json.dumps({"mode": plan["variant"], "tasks": 5, "independent_campaigns": 5,
                "concurrency": args.concurrency, "model": "deepseek-flash", "reasoning_effort": "none"}), flush=True)
            completed = subprocess.run(command, cwd=output, env=env, text=True, encoding="utf-8",
                errors="replace", capture_output=True, timeout=14400, check=False)
            cli_finished = time.monotonic()
            (output / "cli.stdout.txt").write_text(completed.stdout, encoding="utf-8")
            (output / "cli.stderr.txt").write_text(completed.stderr, encoding="utf-8")
            report = collect_batch(batch, plan, json.loads((output / "results.json").read_bytes()), control_hash)
            guards = [json.loads(path.read_bytes()) for path in output.glob("*-guard.*.json")]
            blocked = sum(row.get("blocked_attempts", row.get("external_attempts", 0)) for row in guards)
            roles = {row.get("entry_script") for row in guards}
            if not any("external_attempts" in guard for guard in guards) or "persistent_wrapper.py" not in roles:
                report["evidence_errors"].append("missing_node_or_provider_guards")
            if any(row["result"] in {"pass", "fail"} for row in report["results"]) and "wrapper.py" not in roles:
                report["evidence_errors"].append("missing_assertion_guard")
            if blocked:
                report["evidence_errors"].append("endpoint_or_process_guard_violation")
            report.update(finished_at=_helpers.now(), orchestrator="promptfoo", native_exit_code=completed.returncode,
                planned_entries=5, requested_concurrency=args.concurrency, backend_source_unchanged=True,
                model="deepseek-flash", reasoning_effort="none", api_call_limit=None, authorized_unlimited_api=True,
                elapsed_seconds=round(time.monotonic() - started, 4),
                preparation_elapsed_seconds=round(prepared - started, 4),
                native_promptfoo_elapsed_seconds=round(cli_finished - prepared, 4),
                postprocessing_elapsed_seconds=round(time.monotonic() - cli_finished, 4), guard_blocked_attempts=blocked)
            report["experiment_evidence_valid"] = completed.returncode in {0, 100} and not report["evidence_errors"]
            report["all_five_tasks_completed_normally"] = (report["all_five_tasks_executed"]
                and report["complete_runtime_tasks"] == 5 and report["independent_session_count"] == 5)
            report["requested_scheduling_succeeded"] = (report["experiment_evidence_valid"]
                and report["all_five_tasks_completed_normally"] and not report["global_halt_recorded"]
                and (args.concurrency == 1 or report["peak_overlapping_tasks_with_http"] > 1))
            _helpers.new_json(batch / "report.json", report)
            receipt = {key: value for key, value in report.items() if key not in {"results"}}
            _helpers.new_json(batch / "execution-finished.json", receipt)
            print(json.dumps(receipt), flush=True)
            return 0 if report["experiment_evidence_valid"] else 2
        except BaseException as error:
            _helpers.new_json(batch / "execution-failure.json", {"finished_at": _helpers.now(),
                "error_type": type(error).__name__, "elapsed_seconds": round(time.monotonic() - started, 4),
                "automatic_replay": False})
            raise


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - no raw provider/credential exception text.
        print(json.dumps({"isolated_launcher_error_type": type(error).__name__, "automatic_replay": False}), file=sys.stderr)
        raise SystemExit(2) from None
