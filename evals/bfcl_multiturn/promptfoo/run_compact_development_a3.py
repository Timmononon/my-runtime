"""One new A3 campaign: three Agent controls, same frozen dev40 and native wire.

No models are selected on import. Default CLI is offline preflight; --execute
admits a fresh paid run once and never replays prior task outcomes.
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
from pathlib import Path

HERE = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location("aster_compact_a3_core", HERE / "run_compact_development.py")
_core = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_core)
PROJECT, _helpers = _core.PROJECT, _core._helpers
BACKEND_MODE = _core.BACKEND_MODE
AGENT_POLICY = "readiness_state_v1"
DEFAULT_CAMPAIGN = PROJECT / "artifacts/bfcl/compact-dev40-a3-c20-20261030"
load_development_cases = _core.load_development_cases
snapshot_sources = _core.snapshot_sources
child_environment = _core.child_environment
cli_command = _core.cli_command


def preflight(concurrency=20):
    plan = _core.preflight(concurrency)
    plan.update(campaign=DEFAULT_CAMPAIGN.name,
        variant="aster_bfcl_v4_compact_development40_A3_same_campaign",
        configuration_label="A3", agent_policy=AGENT_POLICY,
        agent_changes=["effective_tool_readiness_and_clarification", "pending_task_and_parameter_provenance",
            "goal_completion_and_minimal_side_effects"],
        control_model_policy={"model": "deepseek-flash", "reasoning_effort": "none", "tools": [],
            "purpose": "task_control", "shares_existing_request_token_deadline_and_twenty_step_budget": True,
            "extra_judge_model": False},
        comparison_reference="evals/bfcl_multiturn/baselines/A0-compact-dev40-deepseek-flash-none-c20-20261030.json",
        comparison_note="One combined A3 measurement against frozen A0; A1 and A2 are not independently measured",
        baseline_policy="A3 development candidate only after all admissible tasks and control accounting are verified")
    return plan


def build_config(plan, campaign):
    if plan.get("agent_policy") != AGENT_POLICY or plan.get("configuration_label") != "A3":
        raise ValueError("A3 control policy must be explicit")
    config = _core.build_config(plan, campaign)
    config["description"] = "A3 readiness/state/completion; frozen dev40; native DeepSeek Flash/none"
    for test in config["tests"]:
        test["metadata"].update(configuration_label="A3", agent_policy=AGENT_POLICY)
    return config


def _agent_control_failure(row):
    return row.get("agent_control_failure") is True and any(
        run.get("agent_control_failure") is True and run.get("runtime_status") == "failed"
        and (run.get("runtime_diagnostic") or {}).get("error_type") == "TaskControlError"
        for run in row.get("runs", []))


def _classification(row):
    if row.get("result") == "fail" and _agent_control_failure(row) and not _runtime_infrastructure(row):
        return "agent_control_failure"
    return _core._classification(row)


def _runtime_infrastructure(row):
    if not _agent_control_failure(row):
        return _core._runtime_infrastructure(row)
    return bool(row.get("error_type")) or any(
        run.get("runtime_status") == "failed"
        and (run.get("runtime_diagnostic") or {}).get("error_type") not in {None, "TaskControlError"}
        for run in row.get("runs", []))


def collect_campaign(campaign, plan, payload, output):
    result = _core.collect_campaign(campaign, plan, payload, output,
        classification=_classification, runtime_infrastructure=_runtime_infrastructure)
    root = Path(campaign)
    control = json.loads((root / "execution-control.json").read_bytes())
    rows = json.loads((root / "results/results.json").read_bytes())["results"]
    errors = []
    if plan.get("agent_policy") != AGENT_POLICY or control.get("agent_policy") != AGENT_POLICY:
        errors.append("A3_agent_policy_binding_mismatch")
    purposes, per_turn = Counter(), []
    for row in rows:
        if row.get("agent_policy") != AGENT_POLICY:
            errors.append("A3_task_policy_not_recorded")
        for run in row.get("runs", []):
            counts = run.get("model_request_purposes", {})
            if not isinstance(counts, dict) or any(type(value) is not int or value < 0 for value in counts.values()):
                errors.append("A3_invalid_request_purpose_counts")
                continue
            total = sum(counts.values())
            purposes.update(counts)
            budget = run.get("runtime_budget", {})
            if total > 20 or budget.get("model_requests") != total:
                errors.append("A3_official_per_turn_model_step_mismatch")
            trace = run.get("task_control")
            completed = run.get("runtime_status") == "completed"
            if completed and (not isinstance(trace, dict) or trace.get("policy") != AGENT_POLICY
                    or trace.get("run_id") != run["run_id"] or trace.get("session_id") != row.get("session_id")
                    or trace.get("controller_requests") != counts.get("task_control", 0)
                    or trace.get("main_requests") != counts.get("main", 0)
                    or counts.get("task_control", 0) < 1
                    or not trace.get("decisions")):
                errors.append("A3_completed_turn_control_trace_mismatch")
            per_turn.append({"task_id": row["id"], "turn_index": run["turn_index"],
                "runtime_status": run.get("runtime_status"), "model_request_purposes": counts,
                "total_model_request_reservations": total})
    if (result["complete_runtime_tasks"] == result["admissible_tasks"]
            and sum(purposes.values()) != result["model_api_calls"]):
        errors.append("A3_complete_run_http_and_model_request_counts_differ")
    result["evidence_errors"] = sorted(set(result["evidence_errors"] + errors))
    result.update(configuration_label="A3", agent_policy=AGENT_POLICY,
        comparison_reference=plan["comparison_reference"], comparison_note=plan["comparison_note"],
        model_request_purposes=dict(purposes), logical_model_request_reservations=sum(purposes.values()),
        controller_model_requests=purposes["task_control"], action_model_requests=purposes["main"],
        per_turn_model_accounting=per_turn)
    assessment = result["baseline_assessment"]
    assessment["kind"] = "A3_frozen_development_agent_configuration"
    if errors:
        assessment["readiness_limitations"].append("A3_control_accounting_validation_errors")
        assessment["capability_baseline_ready"] = assessment["usage_baseline_ready"] = False
        result["main_success_rate"] = None
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
                max_api_calls=None, max_output_tokens=8192, authorized_unlimited_api=True, agent_policy=AGENT_POLICY)
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
