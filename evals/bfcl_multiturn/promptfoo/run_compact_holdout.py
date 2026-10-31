"""Native promptfoo measurement of sixty held-out tasks with frozen runtimes.

The public entrypoint is standard-library-only until a separate materialized
worker starts. --execute is the sole live admission; default preparation never
selects credentials or dispatches a model request.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[2]
CONFIGURATIONS = {"A0": None, "A3": "readiness_state_v1", "MF-Gate-v1": "capability_check_v1"}
BACKEND_MODE = "same_campaign_task_locks_v1"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read(path):
    return json.loads(Path(path).read_bytes())


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _new_or_same(path, value):
    if path.exists():
        if _read(path) != value:
            raise ValueError("Immutable prepared evidence differs")
    else:
        with path.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")


def preflight(configuration, concurrency=20):
    if configuration not in CONFIGURATIONS or type(concurrency) is not int or not 1 <= concurrency <= 100:
        raise ValueError("An explicit supported configuration and concurrency are required")
    core = _load("holdout_preflight_common", HERE / "run_compact_development.py")
    dataset = _load("holdout_frozen_dataset", HERE / "compact_holdout_dataset.py")
    _cases, metadata = dataset.load_holdout_dataset()
    plan = core.preflight(concurrency)
    plan.update(metadata)
    plan.update(
        variant="aster_bfcl_v4_compact_holdout60_frozen_same_campaign",
        configuration_label=configuration, agent_policy=CONFIGURATIONS[configuration],
        evaluation_split="holdout", evaluation_task_count=60,
        planned_entries=60, planned_assessments=60, planned_budget_groups=60,
        planned_holdout_tasks=60, development_inference_tasks=0,
        runtime_task_control="readiness_state_v1" if configuration == "A3" else "off",
        runtime_prompt_policy="readiness_prompt_v1" if configuration == "MF-Gate-v1" else "off",
        runtime_capability_gate="capability_check_v1" if configuration == "MF-Gate-v1" else "off",
        comparison_reference="evals/bfcl_multiturn/baselines/A0-compact-dev40-deepseek-flash-none-c20-20261030.json",
        comparison_references=["A0", "A3", "MF-Gate-v1"],
        comparison_note="Three frozen Agent versions measured once on the same untouched sixty-task subset; no tuning during this measurement",
        scope_note="Exactly compact100-v2 sixty holdout IDs; forty development tasks are excluded from inference",
        membership_policy="Exact frozen holdout membership and source order; no task replacement or reclassification as development",
        baseline_policy="Held-out measurement only after all admissible tasks and immutable source evidence are verified",
        extra_judge_model=False,
    )
    if configuration == "MF-Gate-v1":
        from aster.engine.capability_gate import policy_identity

        plan["agent_policy_identity"] = policy_identity()
    else:
        plan["agent_policy_identity"] = None
    return plan


def build_config(plan, campaign, *, asset_root=PROJECT):
    core = _load("holdout_config_common", HERE / "run_compact_development.py")
    config = core.build_config(plan, campaign)
    label = plan["configuration_label"]
    config["description"] = f"{label}: frozen Aster runtime, sixty complete held-out tasks"
    config["providers"][0].update(id="file://" + (HERE / "holdout_provider.py").as_posix(),
                                    label=f"{label}-holdout60-flash-none")
    config["providers"][0]["config"].update(configuration_label=label,
        pythonExecutable=str(Path(asset_root) / ".venv/Scripts/python.exe"))
    for test in config["tests"]:
        test["metadata"].update(configuration_label=label, evaluation_split="holdout",
                                source_commit=plan.get("frozen_source_commit"))
    return config


def child_environment(output, control, *, asset_root=PROJECT):
    core = _load("holdout_environment_common", HERE / "run_compact_development.py")
    env = core.child_environment(output, control)
    env["PROMPTFOO_PYTHON"] = str(Path(asset_root) / ".venv/Scripts/python.exe")
    env["PROMPTFOO_PYTHON_WORKERS"] = str(control["concurrent_tasks"]["maximum"])
    return env


def cli_command(output, concurrency, *, asset_root=PROJECT):
    cli = Path(asset_root) / ".eval-tools/promptfoo/node_modules/promptfoo/dist/src/entrypoint.js"
    node = shutil.which("node")
    if not node or not cli.is_file() or _read(cli.parents[2] / "package.json")["version"] != "0.125.1":
        raise ValueError("The pinned project-local native promptfoo installation is required")
    return [node, str(cli), "eval", "--config", str(output / "promptfooconfig.json"),
        "--env-file", str(output / "empty.env"), "--output", str(output / "results.json"),
        "--no-cache", "--no-share", "--no-write", "--no-progress-bar", "--no-table",
        "--max-concurrency", str(concurrency)]


def _snapshot(campaign):
    core = _load("holdout_snapshot_common", HERE / "run_compact_development.py")
    helpers = core._helpers
    destination = campaign / "as-run-source"
    destination.mkdir()
    split = PROJECT / "evals/bfcl_multiturn/splits"
    paths = helpers.source_files() + [
        *(split / "compact100-v2" / name for name in ("split-manifest.json", "development-ids.txt", "holdout-ids.txt")),
        *(split / "frozen" / name for name in ("split-manifest.json", "development-ids.txt", "holdout-ids.txt", "regression20-ids.txt")),
        split / "create_compact_split_v2.py",
        *(PROJECT / name for name in ("frozen-runtime-manifest.json", "frozen-origin-source-manifest.json",
                                      "shared-infrastructure-source-manifest.json")),
    ]
    records = []
    for path in paths:
        if path.is_symlink():
            raise ValueError("Source snapshots cannot follow symlinks")
        relative, payload = path.relative_to(PROJECT), path.read_bytes()
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(payload)
        records.append({"file": relative.as_posix(), "sha256": helpers.digest(payload), "bytes": len(payload)})
    import importlib.metadata

    distributions = sorted({(item.metadata.get("Name", "unknown"), item.version)
                            for item in importlib.metadata.distributions()})
    manifest = {"schema_version": 1, "captured_at": helpers.now(), "files": records,
        "python_version": sys.version, **core._platform_metadata(),
        "installed_distributions": [{"name": name, "version": version} for name, version in distributions],
        "excluded": ["user Runtime", "credentials", "cache", "*.pyc"],
        "authority": "Exact materialized frozen Agent sources, two declared shared backends, current immutable evaluation harness and split authorities"}
    helpers.new_json(campaign / "source-manifest.json", manifest)
    return manifest


def collect_campaign(campaign, plan, payload, output):
    collector = _load("holdout_base_collector", HERE / "holdout_collector.py")
    configuration = plan["configuration_label"]
    if configuration == "A0":
        result = collector.collect_base_campaign(campaign, plan, payload, output)
    else:
        name = "run_compact_development_a3.py" if configuration == "A3" else "run_compact_development_mf_gate.py"
        specialized = _load("holdout_specialized_collector", HERE / name)
        specialized._core.collect_campaign = collector.collect_base_campaign
        result = specialized.collect_campaign(campaign, plan, payload, output)
    identity = _load("holdout_collector_identity", HERE / "holdout_identity.py")
    root = Path(campaign)
    errors = []
    try:
        proof, proof_hash = identity.verify_callback_identity(root, configuration)
        if proof_hash != plan["runtime_import_identity_sha256"]:
            errors.append("launcher_import_identity_changed")
        for group in plan["groups"]:
            task_id = group["group"]
            key = hashlib.sha256(task_id.encode()).hexdigest()[:24]
            for suffix in (".started.json", ".finished.json"):
                dispatch = _read(Path(output) / "dispatches" / (key + suffix))
                if (dispatch.get("configuration_label") != configuration
                        or dispatch.get("runtime_import_identity_sha256") != proof_hash
                        or dispatch.get("runtime_import_identity") != proof):
                    errors.append("provider_import_identity_mismatch")
        for item in payload.get("results", {}).get("results", []):
            compact = json.loads(item.get("response", {}).get("output", "{}"))
            if (compact.get("configuration_label") != configuration
                    or compact.get("runtime_import_identity_sha256") != proof_hash
                    or compact.get("evaluation_split") != "holdout"):
                errors.append("native_holdout_configuration_identity_mismatch")
    except (ValueError, KeyError, OSError, TypeError):
        errors.append("runtime_import_identity_verification_failed")
        proof_hash = None
    result.update(configuration_label=configuration, evaluation_split="holdout",
        evaluation_task_count=60, planned_holdout_tasks=60, development_inference_tasks=0,
        holdout_tasks_scored=result["scorable_tasks"],
        frozen_source_commit=plan["frozen_source_commit"], runtime_workspace=plan["runtime_workspace"],
        runtime_import_identity_sha256=proof_hash,
        frozen_runtime_manifest_sha256=plan["frozen_runtime_manifest_sha256"],
        score_note="Single held-out subset measurement of frozen policies; not a full BFCL leaderboard score or evidence of unseen model training data",
        comparison_note=plan["comparison_note"])
    result["evidence_errors"] = sorted(set(result["evidence_errors"] + errors))
    assessment = result["baseline_assessment"]
    assessment.update(kind=configuration + "_frozen_holdout_agent_configuration",
        scope="Same sixty frozen held-out tasks and captured model/budget/scoring/concurrency",
        holdout_validation_performed=not errors and assessment["capability_baseline_ready"])
    if errors:
        assessment["readiness_limitations"].append("holdout_import_identity_errors")
        assessment["capability_baseline_ready"] = assessment["usage_baseline_ready"] = False
        result["main_success_rate"] = None
    return result


def _worker(args):
    builder = _load("holdout_worker_frozen_builder", HERE / "frozen_holdout_runtime.py")
    manifest = builder.validate_runtime(PROJECT)
    if manifest["configuration_label"] != args.configuration:
        raise ValueError("The selected configuration differs from the frozen workspace")
    identity = _load("holdout_worker_identity", HERE / "holdout_identity.py")
    proof = identity.capture_runtime_identity(args.configuration, PROJECT)
    campaign = args.campaign_dir.resolve()
    plan = preflight(args.configuration, args.concurrency)
    plan.update(campaign=campaign.name, frozen_source_commit=proof["source_commit"], runtime_workspace=str(PROJECT),
                frozen_runtime_manifest_sha256=_sha(PROJECT / "frozen-runtime-manifest.json"))
    for name in ("execution-started.json", "source-manifest.json", "results", "promptfoo"):
        if (campaign / name).exists():
            raise ValueError("Campaign already started; resuming or replaying is forbidden")
    _new_or_same(campaign / "runtime-import-identity.json", proof)
    plan["runtime_import_identity_sha256"] = _sha(campaign / "runtime-import-identity.json")
    _new_or_same(campaign / "campaign-plan.json", plan)
    if not args.execute:
        print(json.dumps({"mode": "preflight_only", "configuration_label": args.configuration,
            "tasks": 60, "defined_user_turns": plan["defined_user_turns"], "concurrency": args.concurrency,
            "source_commit": proof["source_commit"], "runtime_modules_verified": len(proof["modules"]),
            "model_api_calls": 0, "judge_api_calls": 0}), flush=True)
        return 0
    from aster.evaluation.bfcl_current import capture_current_profile
    from aster.evaluation.bfcl_multiturn_concurrent import finalize_campaign, initialize_campaign
    from aster.paths import resolve_data_dir
    from aster.platform.filesystem import FileLock

    helpers = _load("holdout_live_common", HERE / "run_compact_development.py")._helpers
    dataset = _load("holdout_live_dataset", HERE / "compact_holdout_dataset.py")
    with FileLock(campaign / "launch.lock", timeout=.1):
        start = time.monotonic()
        helpers.new_json(campaign / "execution-started.json", {
            "mode": plan["variant"], "configuration_label": args.configuration,
            "started_at": helpers.now(), "authorized_unlimited_api": True, "api_call_limit": None,
            "backend_mode": BACKEND_MODE, "maximum_concurrency": args.concurrency,
            "campaign_plan_sha256": _sha(campaign / "campaign-plan.json"),
            "runtime_import_identity_sha256": plan["runtime_import_identity_sha256"],
            "launcher_sha256": _sha(Path(__file__)), "frozen_source_commit": proof["source_commit"]})
        try:
            _snapshot(campaign)
            cases = dataset.load_holdout_cases()
            if [case["id"] for case in cases] != [group["group"] for group in plan["groups"]]:
                raise ValueError("Held-out task order differs from the frozen plan")
            profile = capture_current_profile(resolve_data_dir(), provider_id="deepseek",
                model="deepseek-flash", reasoning_effort="none")
            if (profile.protocol != "deepseek_responses" or profile.model != "deepseek-flash"
                    or profile.reasoning_effort != "none"):
                raise ValueError("The requested native DeepSeek no-thinking profile is unavailable")
            public = profile.public_summary()
            public = {key: public[key] for key in ("provider_id", "provider_name", "base_url", "protocol", "model",
                "reasoning_effort", "identity_revision", "capabilities") if key in public}
            public["capabilities"] = {key: public.get("capabilities", {})[key] for key in (
                "id", "name", "context_window", "max_output_tokens", "input_modalities", "effort_levels")
                if key in public.get("capabilities", {})}
            helpers.new_json(campaign / "model-selection.json", public)
            control = initialize_campaign(cases, campaign, profile=profile, maximum_concurrency=args.concurrency,
                max_api_calls=None, max_output_tokens=8192, authorized_unlimited_api=True,
                agent_policy=CONFIGURATIONS[args.configuration])
            output = campaign / "promptfoo"
            (output / "dispatches").mkdir(parents=True)
            helpers.new_json(output / "promptfooconfig.json", build_config(plan, campaign, asset_root=args.asset_root))
            shutil.copyfile(HERE / "no_thinking_assertion.py", output / "no_thinking_assertion.py")
            (output / "empty.env").write_text("", encoding="utf-8")
            env = child_environment(output, control, asset_root=args.asset_root)
            completed = subprocess.run(cli_command(output, args.concurrency, asset_root=args.asset_root),
                cwd=output, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=14400, check=False)
            (output / "cli.stdout.txt").write_text(completed.stdout, encoding="utf-8")
            (output / "cli.stderr.txt").write_text(completed.stderr, encoding="utf-8")
            finalize_campaign(campaign)
            collected = collect_campaign(campaign, plan, _read(output / "results.json"), output)
            guards = [_read(path) for path in output.glob("*-guard.*.json")]
            blocked = sum(row.get("blocked_attempts", row.get("external_attempts", 0)) for row in guards)
            roles = {row.get("entry_script") for row in guards}
            if not any("external_attempts" in row for row in guards) or not {"persistent_wrapper.py", "wrapper.py"} <= roles:
                collected["evidence_errors"].append("missing_node_provider_or_assertion_guards")
            if blocked:
                collected["evidence_errors"].append("endpoint_or_process_guard_violation")
            collected.update(finished_at=helpers.now(), elapsed_seconds=round(time.monotonic() - start, 4),
                native_exit_code=completed.returncode, guard_blocked_attempts=blocked,
                authorized_unlimited_api=True, api_call_limit=None, model="deepseek-flash", reasoning_effort="none")
            collected["evidence_integrity_verified"] = completed.returncode in {0, 100} and not collected["evidence_errors"]
            collected["integration_verified"] = (collected["evidence_integrity_verified"]
                and collected["baseline_assessment"]["capability_baseline_ready"])
            helpers.new_json(campaign / "report.json", collected)
            helpers.new_json(campaign / "execution-finished.json", collected)
            print(json.dumps({key: collected[key] for key in (
                "configuration_label", "planned_tasks", "passed_tasks", "scorable_tasks", "main_success_rate",
                "model_api_calls", "elapsed_seconds", "integration_verified", "evidence_errors")}), flush=True)
            return 0 if collected["integration_verified"] else 2
        except BaseException as error:
            helpers.new_json(campaign / "execution-failure.json", {
                "finished_at": helpers.now(), "error_type": type(error).__name__,
                "elapsed_seconds": round(time.monotonic() - start, 4), "automatic_replay": False})
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--configuration", choices=CONFIGURATIONS, required=True)
    parser.add_argument("--campaign-dir", type=Path, required=True)
    parser.add_argument("--concurrency", type=int, choices=range(1, 101), default=20)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--runtime-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--asset-root", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.runtime_worker:
        if args.asset_root is None or not args.asset_root.is_absolute():
            parser.error("An explicit immutable dependency asset root is required")
        return _worker(args)
    campaign = args.campaign_dir.resolve()
    campaign.mkdir(parents=True, exist_ok=True)
    for name in ("execution-started.json", "source-manifest.json", "results", "promptfoo"):
        if (campaign / name).exists():
            parser.error("Campaign already started; resuming or replaying is forbidden")
    builder = _load("holdout_parent_frozen_builder", HERE / "frozen_holdout_runtime.py")
    workspace = campaign / "runtime-workspace"
    if workspace.exists():
        manifest = builder.validate_runtime(workspace)
        if manifest["configuration_label"] != args.configuration:
            raise ValueError("Prepared workspace belongs to another frozen configuration")
    else:
        builder.materialize_runtime(args.configuration, workspace)
    entry = workspace / "evals/bfcl_multiturn/promptfoo/run_compact_holdout.py"
    if _sha(entry) != _sha(Path(__file__)):
        raise ValueError("Prepared holdout launcher differs; use a new campaign after source changes")
    env = dict(os.environ)
    env.update(PYTHONPATH=str(workspace / "src"), PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    command = [str(PROJECT / ".venv/Scripts/python.exe"), "-u", str(entry), "--runtime-worker",
        "--asset-root", str(PROJECT), "--configuration", args.configuration,
        "--campaign-dir", str(campaign), "--concurrency", str(args.concurrency)]
    if args.execute:
        command.append("--execute")
    return subprocess.run(command, cwd=workspace, env=env, check=False).returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - never expose credential/provider prose.
        print(json.dumps({"holdout_launcher_error_type": type(error).__name__, "automatic_replay": False}), file=sys.stderr)
        raise SystemExit(2) from None
