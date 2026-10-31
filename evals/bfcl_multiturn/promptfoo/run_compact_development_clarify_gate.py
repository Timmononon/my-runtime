"""Once-only readiness-check candidate on the frozen forty development tasks.

Native promptfoo dispatches complete Aster tasks; both the tool-free capability
check and subsequent native requests share the existing twenty-step budget.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

from aster.engine.readiness_gate_v2 import CHECK_SYSTEM, POLICY, policy_identity
from aster.engine.readiness_prompt import POLICY as NATIVE_POLICY
from aster.engine.readiness_prompt import SYSTEM_PREAMBLE as NATIVE_PREAMBLE

HERE = Path(__file__).resolve().parent
_SPEC = importlib.util.spec_from_file_location("aster_compact_clarify_gate_core", HERE / "run_compact_development.py")
_core = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_core)
PROJECT, _helpers = _core.PROJECT, _core._helpers
BACKEND_MODE = _core.BACKEND_MODE
AGENT_POLICY = POLICY
CONFIGURATION_LABEL = "Readiness-Gate-v2"
DEFAULT_CAMPAIGN = PROJECT / "artifacts/bfcl/compact-dev40-clarify-gate-c20-20261030"
load_development_cases = _core.load_development_cases
snapshot_sources = _core.snapshot_sources
child_environment = _core.child_environment
cli_command = _core.cli_command


def preflight(concurrency=20):
    plan = _core.preflight(concurrency)
    plan.update(campaign=DEFAULT_CAMPAIGN.name,
        variant="aster_bfcl_v4_compact_development40_READINESS_GATE_V2_same_campaign",
        configuration_label=CONFIGURATION_LABEL, agent_policy=AGENT_POLICY,
        agent_policy_identity=policy_identity(), agent_changes=["once_per_user_turn_three_state_readiness_check"],
        runtime_task_control="off", runtime_prompt_policy=NATIVE_POLICY, runtime_capability_gate=POLICY,
        capability_check_model_policy={"purpose": "capability_check", "tools": [],
            "shares_existing_twenty_step_request_token_deadline_budget": True,
            "parse_failure_policy": "fallback_to_native"},
        extra_judge_model=False,
        comparison_references=[
            "evals/bfcl_multiturn/baselines/A0-compact-dev40-deepseek-flash-none-c20-20261030.json",
            "evals/bfcl_multiturn/baselines/MF-Gate-v1-compact-dev40-deepseek-flash-none-c20-20261030.json"],
        comparison_note="One readiness-check candidate; adds needs_clarification only; all model checks are Agent cost; no held-out claims",
        baseline_policy="Readiness-Gate-v2 development candidate after exact task, prompt, gate and budget evidence verification")
    return plan


def build_config(plan, campaign):
    if (plan.get("agent_policy") != AGENT_POLICY or plan.get("configuration_label") != CONFIGURATION_LABEL
            or plan.get("agent_policy_identity") != policy_identity()):
        raise ValueError("Capability-check policy must be explicitly bound")
    config = _core.build_config(plan, campaign)
    config["description"] = "Readiness check then native Aster; frozen dev40; DeepSeek Flash/none"
    for test in config["tests"]:
        test["metadata"].update(configuration_label=CONFIGURATION_LABEL, agent_policy=AGENT_POLICY,
            agent_policy_identity=policy_identity())
    return config


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _gate_observation(observation, receipt, trace):
    """Validate actual captured input, independently of the policy label."""
    errors = []
    system = observation.get("system")
    purpose = receipt.get("purpose")
    if (not isinstance(system, str) or observation.get("request_purpose") != purpose
            or observation.get("request_number") != receipt.get("request_number")
            or observation.get("system_sha256") != _sha(system)):
        return ["gate_observation_receipt_or_system_hash_mismatch"]
    if purpose == "main":
        if not system.startswith(NATIVE_PREAMBLE) or system.endswith("\n\n" + CHECK_SYSTEM):
            errors.append("native_model_input_prefix_mismatch")
        return errors
    if purpose != "capability_check":
        return ["unexpected_request_purpose"]
    if (observation.get("offered_tools") != [] or not system.startswith(NATIVE_PREAMBLE)
            or not system.endswith("\n\n" + CHECK_SYSTEM)):
        errors.append("capability_check_was_not_tool_free_or_pinned")
    messages = observation.get("messages", [])
    if len(messages) != 1 or messages[0].get("role") != "user" or not isinstance(messages[0].get("content"), str):
        return errors + ["capability_check_input_envelope_malformed"]
    try:
        content = messages[0]["content"]
        payload = json.loads(content)
        if (set(payload) != {"policy", "conversation", "tools", "tools_sha256"}
                or payload["policy"] != POLICY or not isinstance(payload["conversation"], list)
                or not isinstance(payload["tools"], list) or _canonical(payload) != content
                or any(not isinstance(tool, dict) or set(tool) != {"name", "description", "parameters"}
                       for tool in payload["tools"])
                or payload["tools_sha256"] != _sha(_canonical(payload["tools"]))
                or trace.get("input_sha256") != _sha(content)
                or trace.get("tools_sha256") != payload["tools_sha256"]
                or trace.get("request_number") != receipt.get("request_number")):
            errors.append("capability_check_input_or_trace_identity_mismatch")
    except (ValueError, TypeError, AttributeError):
        errors.append("capability_check_input_envelope_malformed")
    return errors


def collect_campaign(campaign, plan, payload, output):
    result = _core.collect_campaign(campaign, plan, payload, output)
    root, identity = Path(campaign), policy_identity()
    control = json.loads((root / "execution-control.json").read_bytes())
    rows = json.loads((root / "results/results.json").read_bytes())["results"]
    errors, purposes, statuses, parse_statuses, per_turn = [], Counter(), Counter(), Counter(), []
    if any(item.get("agent_policy") != AGENT_POLICY or item.get("agent_policy_identity") != identity
           for item in (plan, control, *rows)):
        errors.append("capability_policy_identity_mismatch")
    for row in rows:
        observations = row.get("step_observations", [])
        if not isinstance(observations, list) or any(not isinstance(item, dict) for item in observations):
            errors.append("invalid_captured_observations")
            observations = []
        task_total = 0
        for run in row.get("runs", []):
            counts, receipts = run.get("model_request_purposes"), run.get("model_request_receipts")
            completed = run.get("runtime_status") == "completed"
            if (not isinstance(counts, dict) or not isinstance(receipts, list)
                    or any(type(value) is not int or value < 0 for value in counts.values())):
                errors.append("invalid_model_accounting")
                continue
            total = sum(counts.values())
            task_total += total
            purposes.update(counts)
            if (set(counts) - {"main", "capability_check"} or total != len(receipts) or total > 20
                    or run.get("runtime_budget", {}).get("model_requests") != total
                    or dict(Counter(item.get("purpose") for item in receipts)) != counts):
                errors.append("shared_model_request_or_twenty_step_mismatch")
            trace = run.get("capability_gate")
            if (run.get("runtime_prompt_policy") != NATIVE_POLICY or run.get("runtime_task_control") != "off"
                    or run.get("runtime_capability_gate") != POLICY or run.get("agent_policy_identity") != identity
                    or run.get("task_control") is not None or run.get("agent_control_failure") is True):
                errors.append("runtime_gate_configuration_mismatch")
            if not isinstance(trace, dict):
                errors.append("missing_capability_check_trace")
                continue
            checks = trace.get("checks", [])
            if (trace.get("policy") != POLICY or trace.get("policy_identity") != identity
                    or trace.get("session_id") != row.get("session_id") or trace.get("run_id") != run.get("run_id")
                    or trace.get("check_requests") != counts.get("capability_check", 0)
                    or trace.get("main_requests") != counts.get("main", 0)
                    or not isinstance(checks, list) or completed and len(checks) != 1
                    or completed and counts.get("capability_check") != 1):
                errors.append("once_per_turn_gate_trace_or_accounting_mismatch")
            if completed and checks:
                check = checks[0]
                status = check.get("status")
                statuses.update([status])
                parse_statuses.update([check.get("parse_status")])
                if status not in {"ready", "missing_capability", "needs_clarification", "fallback_to_native"}:
                    errors.append("completed_turn_without_final_gate_status")
                if (status in {"ready", "missing_capability", "needs_clarification"} and check.get("parse_status") != "ok"
                        or status == "fallback_to_native" and check.get("parse_status") == "ok"):
                    errors.append("gate_status_parse_contract_mismatch")
                if (status in {"missing_capability", "needs_clarification"} and (counts.get("main", 0) or total != 1)
                        or status in {"ready", "fallback_to_native"} and counts.get("main", 0) < 1):
                    errors.append("gate_branch_was_not_executed_as_recorded")
                if status in {"missing_capability", "needs_clarification"} and any(
                        item.get("run_id") == run.get("run_id") for item in row.get("tool_ledger", [])):
                    errors.append("missing_capability_branch_executed_business_tool")
            observed = [item for item in observations if item.get("turn_index") == run.get("turn_index")]
            if completed and len(observed) != total:
                errors.append("completed_turn_observation_count_mismatch")
            check_number = 0
            native_system = None
            for observation, receipt in zip(observed, receipts):
                check = checks[check_number] if receipt.get("purpose") == "capability_check" and check_number < len(checks) else {}
                errors.extend(_gate_observation(observation, receipt, check))
                if receipt.get("purpose") == "capability_check":
                    check_number += 1
                    system = observation.get("system", "")
                    if isinstance(system, str) and system.endswith("\n\n" + CHECK_SYSTEM):
                        native_system = system[:-len("\n\n" + CHECK_SYSTEM)]
                elif native_system is not None and observation.get("system") != native_system:
                    errors.append("native_system_changed_after_capability_check")
            per_turn.append({"task_id": row["id"], "turn_index": run["turn_index"],
                "runtime_status": run.get("runtime_status"), "model_request_purposes": counts,
                "gate_status": (checks[-1].get("status") if checks else None),
                "total_model_request_reservations": total})
        if (row.get("runs") and all(run.get("runtime_status") == "completed" for run in row["runs"])
                and task_total != row.get("http_attempts")):
            errors.append("completed_task_http_accounting_mismatch")
    if result["complete_runtime_tasks"] == result["admissible_tasks"] and sum(purposes.values()) != result["model_api_calls"]:
        errors.append("complete_run_http_and_model_request_counts_differ")
    result["evidence_errors"] = sorted(set(result["evidence_errors"] + errors))
    result.update(configuration_label=CONFIGURATION_LABEL, agent_policy=POLICY, agent_policy_identity=identity,
        runtime_task_control="off", runtime_prompt_policy=NATIVE_POLICY, runtime_capability_gate=POLICY,
        comparison_references=plan["comparison_references"], comparison_note=plan["comparison_note"],
        model_request_purposes=dict(purposes), logical_model_request_reservations=sum(purposes.values()),
        capability_check_model_requests=purposes["capability_check"], action_model_requests=purposes["main"],
        controller_model_requests=purposes["task_control"], gate_status_counts=dict(statuses),
        gate_parse_status_counts=dict(parse_statuses), needs_clarification_turns=statuses["needs_clarification"],
        missing_capability_turns=statuses["missing_capability"], per_turn_model_accounting=per_turn)
    assessment = result["baseline_assessment"]
    assessment["kind"] = "READINESS_GATE_V2_frozen_development_configuration"
    if errors:
        assessment["readiness_limitations"].append("capability_check_identity_or_accounting_errors")
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
