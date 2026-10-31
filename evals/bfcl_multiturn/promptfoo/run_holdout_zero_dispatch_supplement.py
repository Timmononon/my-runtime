"""Once-only A3 supplement for one proven never-dispatched held-out task.

This independent evaluation adapter never changes the primary campaign or any
frozen Agent source. An incomplete or unknown prior external outcome is not an
eligible supplement. Default mode prepares and verifies; --execute admits the
new single-task campaign through native promptfoo.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[2]
TASK_ID = "multi_turn_miss_func_33"
SOURCE_COMMIT = "04d98823bec6d0222047ef1c04f27b49975fd74e"
ORIGINAL_RELATIVE = "artifacts/bfcl/compact-holdout60-a3-c20-20261030"
FROZEN_MANIFEST_RELATIVE = "artifacts/bfcl/holdout60-a3-zero-dispatch-forensic-20261031/original-a3-files.json"
FROZEN_MANIFEST_SHA256 = "ed8437bb08212148e7738a27f8e5937f071c3ea8c79ee9cf394c1aed7e1eed47"
CASE_SHA256 = "1fb299754620bf57c41cb792fa6085c0e4d28ce8ab03e2c0ba31efd93143192a"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read(path):
    return json.loads(Path(path).read_bytes())


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def case_digest(case):
    return hashlib.sha256(json.dumps(case, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def require_zero_dispatch(facts):
    """Fail closed on every known or unknown prior dispatch/effect boundary."""
    if (facts.get("task_id") != TASK_ID or facts.get("configuration_label") != "A3"
            or facts.get("source_commit") != SOURCE_COMMIT or facts.get("case_sha256") != CASE_SHA256):
        raise ValueError("Only the explicitly bound frozen A3 never-dispatched task is eligible")
    if (facts.get("original_sealed") is not True or facts.get("original_test_only") is not False
            or facts.get("formal_result") != "not_run" or facts.get("reason") != "task_not_invoked"
            or facts.get("callback_error_type") != "PermissionError"
            or facts.get("callback_outcome") != "provider_exception"
            or facts.get("callback_guard_blocked_attempts") != 0):
        raise ValueError("Original task is not the documented predispatch local infrastructure exception")
    if (facts.get("task_HTTP_attempts") != 0 or facts.get("unknown_HTTP_outcomes") != 0
            or facts.get("global_unknown_HTTP_outcomes") != 0
            or facts.get("task_run_count") != 0 or facts.get("intent_exists") is not False
            or facts.get("protocol_exists") is not False or facts.get("case_output_exists") is not False):
        raise ValueError("Prior intent, execution, HTTP dispatch or unknown outcomes prohibit a supplement")
    return True


def verify_original(asset_root, task_id=TASK_ID):
    """Read-only authority check; never reconcile/finalize/resume the original."""
    if task_id != TASK_ID:
        raise ValueError("This adapter only admits its predeclared uninvoked task")
    assets = Path(asset_root).resolve()
    original = assets / ORIGINAL_RELATIVE
    authority = assets / FROZEN_MANIFEST_RELATIVE
    if sha(authority) != FROZEN_MANIFEST_SHA256:
        raise ValueError("Original frozen file authority changed")
    frozen = read(authority)
    paths = {path.relative_to(original).as_posix() for path in original.rglob("*") if path.is_file()}
    if paths != set(frozen["files"]) or len(paths) != 1278:
        raise ValueError("Original campaign file membership changed")
    for relative, metadata in frozen["files"].items():
        path = original / relative
        if path.is_symlink() or sha(path) != metadata["sha256"]:
            raise ValueError("Original campaign bytes changed")
    plan, control = read(original / "campaign-plan.json"), read(original / "execution-control.json")
    report = read(original / "report.json")
    if report != read(original / "execution-finished.json"):
        raise ValueError("Original sealed report differs from completion marker")
    if (plan.get("configuration_label") != "A3" or plan.get("frozen_source_commit") != SOURCE_COMMIT
            or control.get("agent_policy") != "readiness_state_v1" or len(control["case_ids"]) != 60
            or TASK_ID not in control["case_ids"]):
        raise ValueError("Original campaign policy or membership is not the frozen A3 holdout measurement")
    key = hashlib.sha256(TASK_ID.encode()).hexdigest()[:24]
    start = read(original / "promptfoo/dispatches" / (key + ".started.json"))
    end = read(original / "promptfoo/dispatches" / (key + ".finished.json"))
    native = read(original / "promptfoo/results.json")["results"]["results"]
    selected = [item for item in native if item.get("vars", {}).get("task_id") == TASK_ID]
    if len(selected) != 1:
        raise ValueError("Original native callback identity is ambiguous")
    compact = json.loads(selected[0]["response"]["output"])
    rows = read(original / "results/results.json")["results"]
    selected_rows = [row for row in rows if row.get("id") == TASK_ID]
    cases = [case for case in read(original / "private-cases.json") if case.get("id") == TASK_ID]
    if len(selected_rows) != 1 or len(cases) != 1:
        raise ValueError("Original task or private case identity is ambiguous")
    row, case = selected_rows[0], cases[0]
    receipt = read(original / "results/receipts" / (key + ".json"))
    if receipt["result"] != row or receipt["dataset_binding"] != control["dataset_binding"]:
        raise ValueError("Original finalizer receipt differs")
    calls = read(original / "results/http-budget.json")["calls"]
    guard = read(original / "promptfoo" / ("python-guard." + str(end["pid"]) + ".json"))
    if (start["pid"] != end["pid"] or start["runtime_import_identity_sha256"] != sha(original / "runtime-import-identity.json")
            or compact.get("error_type") != end.get("error_type")
            or compact.get("result") != "concurrent_dispatch_error"):
        raise ValueError("Original callback and exception witnesses differ")
    facts = {"task_id": TASK_ID, "configuration_label": "A3", "source_commit": SOURCE_COMMIT,
        "case_sha256": case_digest(case), "original_sealed": control["concurrent_tasks"]["finalized"],
        "original_test_only": control["test_only"], "formal_result": row["result"], "reason": row.get("reason"),
        "callback_error_type": end.get("error_type"), "callback_outcome": end.get("outcome"),
        "callback_guard_blocked_attempts": guard.get("blocked_attempts"),
        "task_HTTP_attempts": sum(call.get("case_id") == TASK_ID for call in calls),
        "unknown_HTTP_outcomes": sum(call.get("case_id") == TASK_ID and call.get("status") == "outcome_unknown" for call in calls),
        "global_unknown_HTTP_outcomes": sum(call.get("status") == "outcome_unknown" for call in calls),
        "task_run_count": len(row.get("runs", [])), "intent_exists": (original / "results/intents" / (key + ".json")).exists(),
        "protocol_exists": (original / "results/protocol-budgets" / (key + ".json")).exists(),
        "case_output_exists": (original / "results/cases" / key).exists()}
    require_zero_dispatch(facts)
    proof = {"schema_version": 1, "kind": "single_previously_uninvoked_task_supplement",
        "original_campaign": str(original), "original_frozen_manifest_sha256": FROZEN_MANIFEST_SHA256,
        "original_files_verified": len(paths), "original_dataset_binding": control["dataset_binding"],
        "original_plan_sha256": sha(original / "campaign-plan.json"),
        "original_report_sha256": sha(original / "report.json"),
        "original_runtime_import_identity_sha256": sha(original / "runtime-import-identity.json"),
        "original_task_case_sha256": CASE_SHA256, "source_commit": SOURCE_COMMIT,
        "authority_holdout_tasks": 60, "authority_holdout_ids_sha256": plan["holdout_ids_sha256"],
        "supplement_task_ids": [TASK_ID], "eligibility": facts,
        "original_full_60_main_score": None, "task_or_model_replay": False,
        "new_model_api_calls_from_verification": 0, "new_judge_api_calls": 0}
    return proof, copy.deepcopy(case), copy.deepcopy(next(group for group in plan["groups"] if group["group"] == TASK_ID))


def bind_adapter(runner, proof, case, group):
    """Compose a new evaluation adapter around the untouched full-run harness.

    Only this fresh module instance's dataset selection and scope export are
    adapted. Frozen Agent code, transport, budget and scorer paths are unchanged.
    """
    original_preflight, original_load = runner.preflight, runner._load
    original_collect, original_build = runner.collect_campaign, runner.build_config

    def preflight(configuration, concurrency=20):
        if configuration != "A3":
            raise ValueError("Only the bound A3 policy is eligible")
        plan = original_preflight(configuration, concurrency)
        plan.update(groups=[copy.deepcopy(group)], evaluation_task_count=1, planned_entries=1,
            planned_assessments=1, planned_budget_groups=1, planned_holdout_tasks=1,
            defined_user_turns=len(case["question"]), admissible_task_count=1,
            per_category_allocation={case["category"]: 1},
            question_record_sha256={TASK_ID: plan["question_record_sha256"][TASK_ID]},
            reference_admission_issues={}, known_admission_conflicts=[], newly_detected_conflict_ids=[],
            normalized_cases_bytes=len(json.dumps([case], ensure_ascii=False, allow_nan=False).encode()),
            scope_note="One previously uninvoked task from the preserved sixty-ID holdout authority; original primary result remains unchanged",
            comparison_note="Independent one-task supplement, not a replacement or rerun of the primary sixty-task campaign",
            measurement_kind="single_previously_uninvoked_task_supplement", authority_holdout_tasks=60,
            supplement_of=proof["original_campaign"], supplement_eligibility=copy.deepcopy(proof),
            full_60_main_success_rate=None)
        return plan

    def selected_load(name, path):
        if name == "holdout_live_dataset":
            return SimpleNamespace(load_holdout_cases=lambda: [copy.deepcopy(case)])
        return original_load(name, path)

    def build_config(plan, campaign, **kwargs):
        config = original_build(plan, campaign, **kwargs)
        config["description"] = "A3: one proven never-dispatched held-out task in an independent campaign"
        config["tests"][0]["metadata"].update(measurement_kind="single_previously_uninvoked_task_supplement",
            supplement_of=proof["original_campaign"], authority_holdout_tasks=60)
        return config

    def collect(campaign, plan, payload, output):
        result = original_collect(campaign, plan, payload, output)
        result.update(evaluation_task_count=1, planned_holdout_tasks=1, authority_holdout_tasks=60,
            measurement_kind="single_previously_uninvoked_task_supplement", supplement_of=proof["original_campaign"],
            supplement_eligibility=copy.deepcopy(proof), full_60_main_success_rate=None,
            single_task_success_rate=result["main_success_rate"],
            score_note="Only this one new task is scored here; the original sixty-task campaign stays incomplete and unchanged")
        result["baseline_assessment"].update(kind="A3_single_never_dispatched_task_measurement",
            scope="One task only; a separate derived 59+1 cohort must keep both campaign provenances",
            full_60_capability_score_available=False)
        return result

    runner.preflight, runner._load = preflight, selected_load
    runner.build_config, runner.collect_campaign = build_config, collect
    return runner


def worker(args):
    builder = load("supplement_frozen_builder", HERE / "frozen_holdout_runtime.py")
    manifest = builder.validate_runtime(PROJECT)
    if manifest["configuration_label"] != "A3" or manifest["source_commit"] != SOURCE_COMMIT:
        raise ValueError("Supplement Agent runtime must be the exact frozen A3 policy")
    proof, case, group = verify_original(args.asset_root, args.task_id)
    proof_path = HERE / "zero-dispatch-eligibility.json"
    runner = load("supplement_untouched_holdout_harness", HERE / "run_compact_holdout.py")
    runner._new_or_same(proof_path, proof)
    runner._new_or_same(args.campaign_dir / "qualification-proof.json", proof)
    runner = bind_adapter(runner, proof, case, group)
    if not args.execute:
        identity = load("supplement_preflight_identity", HERE / "holdout_identity.py")
        runtime_proof = identity.capture_runtime_identity("A3", PROJECT)
        plan = runner.preflight("A3", args.concurrency)
        plan.update(campaign=args.campaign_dir.name, frozen_source_commit=SOURCE_COMMIT,
            runtime_workspace=str(PROJECT), frozen_runtime_manifest_sha256=sha(PROJECT / "frozen-runtime-manifest.json"))
        runner._new_or_same(args.campaign_dir / "runtime-import-identity.json", runtime_proof)
        plan["runtime_import_identity_sha256"] = sha(args.campaign_dir / "runtime-import-identity.json")
        runner._new_or_same(args.campaign_dir / "campaign-plan.json", plan)
        print(json.dumps({"mode": "preflight_only", "configuration_label": "A3", "tasks": 1,
            "task_ids": [TASK_ID], "defined_user_turns": len(case["question"]),
            "authority_holdout_tasks": 60, "maximum_concurrency": args.concurrency,
            "maximum_simultaneously_runnable_tasks": 1, "model_api_calls": 0, "judge_api_calls": 0}), flush=True)
        return 0
    return runner._worker(SimpleNamespace(configuration="A3", campaign_dir=args.campaign_dir,
        concurrency=args.concurrency, execute=args.execute, asset_root=args.asset_root))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-dir", type=Path, required=True)
    parser.add_argument("--task-id", default=TASK_ID)
    parser.add_argument("--concurrency", type=int, choices=range(1, 21), default=20)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--runtime-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--asset-root", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.runtime_worker:
        if args.asset_root is None or not args.asset_root.is_absolute():
            parser.error("An explicit original asset root is required")
        return worker(args)
    proof, _case, _group = verify_original(PROJECT, args.task_id)
    destination = args.campaign_dir.resolve()
    if destination.is_relative_to(Path(proof["original_campaign"])):
        raise ValueError("A supplement must not write inside the primary campaign")
    destination.mkdir(parents=True, exist_ok=True)
    if any((destination / name).exists() for name in ("execution-started.json", "source-manifest.json", "results", "promptfoo")):
        raise ValueError("Supplement already started; no recovery or replay is automatic")
    builder = load("supplement_parent_builder", HERE / "frozen_holdout_runtime.py")
    workspace = destination / "runtime-workspace"
    if workspace.exists():
        builder.validate_runtime(workspace)
    else:
        builder.materialize_runtime("A3", workspace)
    entry = workspace / "evals/bfcl_multiturn/promptfoo" / Path(__file__).name
    if sha(entry) != sha(Path(__file__)):
        raise ValueError("Prepared supplement adapter changed; use a new directory")
    env = dict(os.environ)
    env.update(PYTHONPATH=str(workspace / "src"), PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
    command = [str(PROJECT / ".venv/Scripts/python.exe"), "-u", str(entry), "--runtime-worker",
        "--asset-root", str(PROJECT), "--campaign-dir", str(destination), "--task-id", TASK_ID,
        "--concurrency", str(args.concurrency)]
    if args.execute:
        command.append("--execute")
    return subprocess.run(command, cwd=workspace, env=env, check=False).returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 - retain only safe exception type.
        print(json.dumps({"supplement_error_type": type(error).__name__, "automatic_replay": False}), file=sys.stderr)
        raise SystemExit(2) from None
