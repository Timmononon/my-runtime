"""Once-only A4.1 prompt-only candidate over the frozen forty development tasks.

The native tool loop keeps ownership of state, tools and budgets. Default CLI
only writes an offline plan; --execute admits one fresh model campaign.
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

from aster.engine.readiness_prompt import POLICY, SYSTEM_PREAMBLE, policy_identity

HERE = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location("aster_compact_a41_core", HERE / "run_compact_development.py")
_core = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_core)
PROJECT, _helpers = _core.PROJECT, _core._helpers
BACKEND_MODE = _core.BACKEND_MODE
AGENT_POLICY = POLICY
CONFIGURATION_LABEL = "A4.1"
DEFAULT_CAMPAIGN = PROJECT / "artifacts/bfcl/compact-dev40-a41-c20-20261030"
load_development_cases = _core.load_development_cases
snapshot_sources = _core.snapshot_sources
child_environment = _core.child_environment
cli_command = _core.cli_command


def preflight(concurrency=20):
    plan = _core.preflight(concurrency)
    plan.update(campaign=DEFAULT_CAMPAIGN.name,
        variant="aster_bfcl_v4_compact_development40_A41_same_campaign",
        configuration_label=CONFIGURATION_LABEL, agent_policy=AGENT_POLICY,
        agent_policy_identity=policy_identity(),
        agent_changes=["native_tool_readiness_prompt_only"],
        runtime_task_control="off", runtime_prompt_policy=POLICY,
        additional_planner_requests=False, extra_judge_model=False,
        comparison_references=[
            "evals/bfcl_multiturn/baselines/A0-compact-dev40-deepseek-flash-none-c20-20261030.json",
            "evals/bfcl_multiturn/baselines/A3-compact-dev40-deepseek-flash-none-c20-20261030.json"],
        comparison_note="One prompt-only A4.1 candidate; gains are unknown until measured; A4.2/A4.3 are not enabled",
        baseline_policy="A4.1 development candidate only after task identity, native requests and official scoring are verified")
    return plan


def build_config(plan, campaign):
    if (plan.get("agent_policy") != AGENT_POLICY or plan.get("configuration_label") != CONFIGURATION_LABEL
            or plan.get("agent_policy_identity") != policy_identity()):
        raise ValueError("A4.1 prompt policy must be explicitly bound")
    config = _core.build_config(plan, campaign)
    config["description"] = "A4.1 native readiness prompt; frozen dev40; DeepSeek Flash/none"
    for test in config["tests"]:
        test["metadata"].update(configuration_label=CONFIGURATION_LABEL, agent_policy=AGENT_POLICY,
            agent_policy_identity=policy_identity())
    return config


def collect_campaign(campaign, plan, payload, output):
    result = _core.collect_campaign(campaign, plan, payload, output)
    root, identity = Path(campaign), policy_identity()
    control = json.loads((root / "execution-control.json").read_bytes())
    rows = json.loads((root / "results/results.json").read_bytes())["results"]
    errors, purposes, per_turn = [], Counter(), []
    if any(item.get("agent_policy") != AGENT_POLICY or item.get("agent_policy_identity") != identity
           for item in (plan, control, *rows)):
        errors.append("A41_prompt_policy_identity_mismatch")
    for row in rows:
        observations = row.get("step_observations", [])
        if (not isinstance(observations, list) or any(not isinstance(item, dict)
                or not isinstance(item.get("system"), str) or not item["system"].startswith(SYSTEM_PREAMBLE)
                for item in observations)):
            errors.append("A41_observed_system_prefix_mismatch")
            observations = []
        task_total = 0
        for run in row.get("runs", []):
            counts, receipts = run.get("model_request_purposes"), run.get("model_request_receipts")
            if (not isinstance(counts, dict) or not isinstance(receipts, list)
                    or any(type(value) is not int or value < 0 for value in counts.values())):
                errors.append("A41_invalid_model_accounting")
                continue
            total = sum(counts.values())
            task_total += total
            purposes.update(counts)
            if (set(counts) - {"main"} or any(item.get("purpose") != "main" for item in receipts)
                    or total != len(receipts) or total > 20
                    or run.get("runtime_budget", {}).get("model_requests") != total
                    or run.get("runtime_status") == "completed" and total < 1):
                errors.append("A41_native_request_or_step_mismatch")
            if (run.get("runtime_prompt_policy") != AGENT_POLICY or run.get("runtime_task_control") != "off"
                    or run.get("agent_policy_identity") != identity or run.get("task_control") is not None
                    or run.get("agent_control_failure") is True):
                errors.append("A41_runtime_prompt_only_mismatch")
            if (run.get("runtime_status") == "completed"
                    and sum(item.get("turn_index") == run["turn_index"] for item in observations) != total):
                errors.append("A41_completed_turn_observation_count_mismatch")
            per_turn.append({"task_id": row["id"], "turn_index": run["turn_index"],
                "runtime_status": run.get("runtime_status"), "model_request_purposes": counts,
                "total_model_request_reservations": total})
        if (row.get("runs") and all(run.get("runtime_status") == "completed" for run in row["runs"])
                and task_total != row.get("http_attempts")):
            errors.append("A41_completed_task_http_accounting_mismatch")
    if (result["complete_runtime_tasks"] == result["admissible_tasks"]
            and sum(purposes.values()) != result["model_api_calls"]):
        errors.append("A41_complete_run_http_and_model_request_counts_differ")
    result["evidence_errors"] = sorted(set(result["evidence_errors"] + errors))
    result.update(configuration_label=CONFIGURATION_LABEL, agent_policy=AGENT_POLICY,
        agent_policy_identity=identity, runtime_task_control="off", runtime_prompt_policy=POLICY,
        comparison_references=plan["comparison_references"], comparison_note=plan["comparison_note"],
        model_request_purposes=dict(purposes), logical_model_request_reservations=sum(purposes.values()),
        controller_model_requests=purposes["task_control"], action_model_requests=purposes["main"],
        per_turn_model_accounting=per_turn)
    assessment = result["baseline_assessment"]
    assessment["kind"] = "A41_frozen_development_prompt_configuration"
    if errors:
        assessment["readiness_limitations"].append("A41_prompt_or_accounting_validation_errors")
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
            "configuration_label": CONFIGURATION_LABEL,
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
