"""Once-only promptfoo run: the frozen twenty tasks, deepseek-flash/none.

The user explicitly authorized unlimited API count for this finite cohort.
Official per-user-turn step limits and request/deadline safety still apply.
This module's default preflight does not read credentials or dispatch requests.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
import time
from copy import deepcopy
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[2]
DEFAULT_CAMPAIGN = PROJECT / "artifacts/bfcl/multiturn20-no-thinking-20261030"
PROVIDER_LABEL = "deepseek-flash-no-thinking"
MAX_OUTPUT_TOKENS = 8192

# The fixed local helpers import only stdlib and have no run-on-import effects.
_SPEC = importlib.util.spec_from_file_location("aster_no_thinking_existing_helpers", HERE / "run_live.py")
_helpers = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_helpers)


def preflight():
    plan = _helpers.preflight()
    for group in plan["groups"]:
        group["api_call_cap"] = None
        group["official_model_step_bound"] = 20 * group["user_turns"]
    plan.update(
        campaign=DEFAULT_CAMPAIGN.name, variant="deepseek_flash_no_thinking_no_api_quota",
        authorized_api_call_limit=None, authorized_unlimited_api=True, api_unlimited=True,
        model="deepseek-flash", reasoning_effort="none", thinking_enabled=False,
        max_output_tokens=MAX_OUTPUT_TOKENS, per_case_api_call_cap=None,
        reserve_for_each_later_admissible_task=0, official_max_steps_per_user_turn=20,
        per_case_deadline_seconds=3600, max_tool_calls_per_case=10000,
        case_deadline_seconds=3600, case_tool_safety_limit=10000,
        inherited_runtime_safety={"max_total_tokens_per_user_run": 300000, "duplicate_limit": 3,
            "context_tokens": 96000, "reserve_tokens": 4096, "semantic_compaction": False, "model_retries": 0},
        provider_task_timeout_ms=3720000, orchestration_timeout_seconds=14400,
        budget_policy="No campaign or whole-task API-count quota. Official maximum 20 model steps per user turn remains.",
        request_policy="Every target HTTP request is durably recorded before dispatch, including failures; retries remain zero.",
        setting_comparison_note="Compared with the prior run, thinking, API quotas and output-token allowance change; this is not a single-factor optimization experiment.",
        scope_note="Same twenty fixed tasks rerun once in a new isolated campaign, without an extra API-count quota or previous-result reuse.")
    return plan


def build_config(plan, campaign):
    return {"description": "LIVE Aster BFCL V4 twenty tasks; deepseek-flash thinking disabled; no API-count quota",
        "prompts": ["Execute the authoritative complete task {{task_id}}"], "sharing": False,
        "providers": [{"id": "file://" + (HERE / "no_thinking_provider.py").as_posix(),
            "label": PROVIDER_LABEL, "config": {"campaign_dir": campaign.as_posix(),
                "pythonExecutable": str(PROJECT / ".venv/Scripts/python.exe"), "workers": 1, "timeout": 3720000}}],
        "tests": [{"description": group["group"], "vars": {"task_id": group["group"]},
            "metadata": {"subcategory": group["category"], "campaign_dir": campaign.as_posix(),
                         "model": "deepseek-flash", "reasoning_effort": "none"}}
            for group in plan["groups"]],
        "defaultTest": {"assert": [{"type": "python", "metric": "live_bfcl_complete_task",
            "value": "file://no_thinking_assertion.py"}]},
        "evaluateOptions": {"maxConcurrency": 1, "showProgressBar": False, "repeat": 1}}


def validate_native(payload, rows):
    # Keep the established strict matrix/receipt/official-result validation.
    # Normalize only after verifying this campaign's explicit identity fields.
    checked = deepcopy(payload)
    errors = []
    for row in checked.get("results", {}).get("results", []):
        if row.get("provider", {}).get("label") != PROVIDER_LABEL:
            errors.append("unexpected_no_thinking_provider_identity")
        try:
            compact = json.loads(row.get("response", {}).get("output", ""))
        except (TypeError, ValueError):
            errors.append("missing_no_thinking_provider_output")
            continue
        if (compact.get("model") != "deepseek-flash" or compact.get("reasoning_effort") != "none"
                or "api_count_limit" not in compact or compact["api_count_limit"] is not None):
            errors.append("unexpected_no_thinking_execution_settings")
        row["provider"]["label"] = "current-aster-multiturn20"
    return sorted(set(errors + _helpers.validate_native(checked, rows)))


def child_environment(output, control):
    # The existing exact-endpoint Python guard and blocked Node preload remain.
    # Locate its stdlib child helper explicitly so imported tests need no cwd.
    spec = importlib.util.spec_from_file_location("run_offline", HERE / "run_offline.py")
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get("run_offline")
    sys.modules["run_offline"] = module
    try:
        spec.loader.exec_module(module)
        return _helpers.child_environment(output, control)
    finally:
        if previous is None:
            sys.modules.pop("run_offline", None)
        else:
            sys.modules["run_offline"] = previous


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-dir", type=Path, default=DEFAULT_CAMPAIGN)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    campaign = args.campaign_dir.resolve()
    plan = preflight()
    plan["campaign"] = campaign.name
    campaign.mkdir(parents=True, exist_ok=True)
    if any((campaign / name).exists() for name in ("execution-started.json", "source-manifest.json", "results", "promptfoo")):
        parser.error("Campaign execution already exists; resuming or replaying is forbidden")
    plan_path = campaign / "campaign-plan.json"
    if plan_path.exists():
        if json.loads(plan_path.read_text(encoding="utf-8")) != plan:
            parser.error("Saved no-thinking plan differs from the pinned task/settings selection")
    else:
        _helpers.new_json(plan_path, plan)
    if not args.execute:
        print(json.dumps({"mode": "preflight_only", "tasks": 20, "model": "deepseek-flash",
            "reasoning_effort": "none", "thinking_enabled": False, "api_call_limit": None,
            "authorized_unlimited_api": True, "official_model_steps_per_user_turn": 20,
            "model_api_calls": 0, "judge_api_calls": 0}))
        return 0
    from aster.evaluation.bfcl_current import capture_current_profile
    from aster.evaluation.bfcl_multiturn_unmetered import finalize_campaign, initialize_campaign
    from aster.evaluation.bfcl_v4_data import load_corpus
    from aster.paths import resolve_data_dir
    from aster.platform.filesystem import FileLock

    with FileLock(campaign / "launch.lock", timeout=.1):
        started = time.monotonic()
        _helpers.new_json(campaign / "execution-started.json", {"mode": "promptfoo_orchestrated_live_no_thinking",
            "started_at": _helpers.now(), "authorized_api_call_limit": None, "authorized_unlimited_api": True,
            "campaign_plan_sha256": _helpers.digest(_helpers.encoded(plan)),
            "launcher_sha256": _helpers.digest(Path(__file__).read_bytes())})
        try:
            _helpers.snapshot_sources(campaign)
            cases_by_id = {case["id"]: case for case in load_corpus(_helpers.CORPUS, tracks=list(_helpers.CATEGORIES))}
            cases = [cases_by_id[group["group"]] for group in plan["groups"]]
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
            control = initialize_campaign(cases, campaign, profile=profile, max_api_calls=None,
                max_output_tokens=MAX_OUTPUT_TOKENS, authorized_unlimited_api=True)
            output = campaign / "promptfoo"
            output.mkdir()
            _helpers.new_json(output / "promptfooconfig.json", build_config(plan, campaign))
            shutil.copyfile(HERE / "no_thinking_assertion.py", output / "no_thinking_assertion.py")
            (output / "empty.env").write_text("", encoding="utf-8")
            cli = PROJECT / ".eval-tools/promptfoo/node_modules/promptfoo/dist/src/entrypoint.js"
            package = json.loads((cli.parents[2] / "package.json").read_text(encoding="utf-8"))
            if package["version"] != "0.125.1":
                raise ValueError("This launcher requires pinned promptfoo 0.125.1")
            command = [shutil.which("node"), str(cli), "eval", "--config", str(output / "promptfooconfig.json"),
                "--env-file", str(output / "empty.env"), "--output", str(output / "results.json"), "--no-cache",
                "--no-share", "--no-write", "--no-progress-bar", "--no-table", "--max-concurrency", "1"]
            print(json.dumps({"mode": "promptfoo_orchestrated_live_no_thinking", "tasks": 20,
                "api_call_limit": None, "authorized_unlimited_api": True,
                "model": public["model"], "effort": public["reasoning_effort"]}), flush=True)
            completed = subprocess.run(command, cwd=output, env=child_environment(output, control),
                text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=14400, check=False)
            (output / "cli.stdout.txt").write_text(completed.stdout, encoding="utf-8")
            (output / "cli.stderr.txt").write_text(completed.stderr, encoding="utf-8")
            report = finalize_campaign(campaign)
            validation = validate_native(json.loads((output / "results.json").read_text(encoding="utf-8")), report["results"])
            guard_files = list(output.glob("*-guard.*.json"))
            guards = [json.loads(path.read_text(encoding="utf-8")) for path in guard_files]
            blocked = sum(row.get("blocked_attempts", row.get("external_attempts", 0)) for row in guards)
            roles = {row.get("entry_script") for row in guards}
            if (not any(path.name.startswith("node-guard.") for path in guard_files)
                    or not {"persistent_wrapper.py", "wrapper.py"} <= roles):
                validation.append("missing_expected_guard_receipts")
            if blocked:
                validation.append("endpoint_or_process_guard_violation")
            receipt = {"finished_at": _helpers.now(), "elapsed_seconds": round(time.monotonic() - started, 4),
                "orchestrator": "promptfoo", "native_exit_code": completed.returncode,
                "planned_entries": 20, "completed_result_rows": len(report["results"]),
                "model": "deepseek-flash", "reasoning_effort": "none", "thinking_enabled": False,
                "authorized_unlimited_api": True, "api_call_limit": None,
                "model_api_calls": report["model_api_calls"], "http_transport_attempts": report["http_transport_attempts"],
                "totals": report["totals"], "framework_validation_errors": validation,
                "guard_blocked_attempts": blocked, "judge_api_calls": 0,
                "official_score": False, "full_leaderboard_score": None,
                "native_errors_note": "Recognized unscorable rows are excluded from the BFCL denominator, not model failures."}
            _helpers.new_json(campaign / "execution-finished.json", receipt)
            print(json.dumps(receipt), flush=True)
            return 0 if completed.returncode in {0, 100} and not validation else 2
        except BaseException as error:
            _helpers.new_json(campaign / "execution-failure.json", {"finished_at": _helpers.now(),
                "error_type": type(error).__name__, "elapsed_seconds": round(time.monotonic() - started, 4),
                "automatic_replay": False})
            raise


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - never print raw provider errors or credentials.
        print(json.dumps({"launcher_error_type": type(error).__name__, "automatic_replay": False}), file=sys.stderr)
        raise SystemExit(2) from None
