"""Fresh-process native promptfoo/MockTransport bridge for a frozen Agent."""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from textwrap import dedent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--main-project", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--configuration", required=True)
    args = parser.parse_args()
    root, project, workspace = args.root.resolve(), args.main_project.resolve(), args.workspace.resolve()
    sys.path.insert(0, str(project / "tests"))
    import aster.engine.loop

    assert Path(aster.engine.loop.__file__).resolve().is_relative_to(workspace / "src")
    from test_bfcl_current import source_at
    from test_bfcl_multiturn_concurrent import case_at
    from test_bfcl_promptfoo_concurrent_native_windows import FIXTURE_PROVIDER

    from aster.evaluation import bfcl_multiturn_concurrent as backend
    from aster.evaluation.bfcl_current import capture_current_profile

    adapter = workspace / "evals/bfcl_multiturn/promptfoo"
    launcher = load("frozen_holdout_test_launcher", adapter / "run_compact_holdout.py")
    identity = load("frozen_holdout_test_identity", adapter / "holdout_identity.py")
    source = source_at(root)
    source_before = {p.relative_to(source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in source.rglob("*") if p.is_file()}
    profile = capture_current_profile(source, model="deepseek-flash", reasoning_effort="none")
    campaign = root / "campaign"
    campaign.mkdir()
    cases = [case_at(i) for i in range(20)]
    if args.configuration == "MF-Gate-v1":
        for case in cases[:10]:
            case.update(id=case["id"].replace("base", "miss_func"), category="multi_turn_miss_func",
                        missed_function={"1": ["touch", "echo"]})
            first, read = copy.deepcopy(case["question"])
            expected = copy.deepcopy(case["expected"])
            case["question"] = [first, [{"role": "user", "content": "Tools have been updated."}], read]
            case["expected"] = [[], *expected]
    plan = launcher.preflight(args.configuration, 20)
    proof = identity.capture_runtime_identity(args.configuration, workspace)
    write(campaign / "runtime-import-identity.json", proof)
    plan.update(campaign=campaign.name, groups=[{"group": c["id"], "entry_ids": [c["id"]],
        "category": c["category"], "user_turns": len(c["question"])} for c in cases],
        evaluation_task_count=20, planned_entries=20, planned_assessments=20,
        test_scope="Twenty synthetic fixtures in a frozen runtime; no held-out inference or capability score",
        runtime_workspace=str(workspace), frozen_source_commit=proof["source_commit"],
        runtime_import_identity_sha256=identity.digest(campaign / "runtime-import-identity.json"))
    write(campaign / "campaign-plan.json", plan)
    launcher._snapshot(campaign)
    control = backend.initialize_campaign(cases, campaign, profile=profile, maximum_concurrency=20,
        _test_only=True, agent_policy=launcher.CONFIGURATIONS[args.configuration])
    output = campaign / "promptfoo"
    (output / "dispatches").mkdir(parents=True)
    (campaign / "mock-arrivals").mkdir()
    (campaign / "mock-requests").mkdir()
    fixture = FIXTURE_PROVIDER.replace("__PROJECT__", repr(str(workspace))).replace(
        "__DEFAULT_RUNTIME__", repr(str(Path.home() / ".aster")))
    # The guard is a shared test asset, not an Agent source or corpus file.
    # Keep the exact existing guard and bind its actual main-project path.
    fixture = fixture.replace(
        'guard_path = PROJECT / "artifacts/bfcl/v4-integration-20261029/offline_gate.py"',
        "guard_path = Path(" + repr(str(project / "artifacts/bfcl/v4-integration-20261029/offline_gate.py")) + ")")
    fixture = fixture.replace("/concurrent_provider.py", "/holdout_provider.py")
    fixture = fixture.replace('state["concurrent_tasks"]["maximum"] == 5',
        'state["concurrent_tasks"]["maximum"] == 20')
    fixture = fixture.replace('glob("*.json"))) != 5', 'glob("*.json"))) != 20')
    fixture = fixture.replace("Five native workers", "Twenty native workers").replace("time.monotonic() + 35", "time.monotonic() + 90")
    start, stop = fixture.index("    phases = ["), fixture.index("    calls = 0")
    if args.configuration == "MF-Gate-v1":
        phases = '''    native = [
        {"calls": [("touch", {"file_name": "memo.txt"}), ("echo", {"content": "alpha-" + str(index), "file_name": "memo.txt"})]},
        {"text": "Created."}, {"calls": [("cat", {"file_name": "memo.txt"})]}, {"text": "alpha-" + str(index)},
    ]
    ready = {"text": json.dumps({"status": "ready", "reply": ""})}
    phases = [ready, *native[:2], ready, *native[2:]]
    if index < 10:
        phases.insert(0, {"text": json.dumps({"status": "missing_capability", "reply": "File creation is not available yet."})})
'''
    elif args.configuration == "A3":
        phases = '''    def decision(kind, reply="", next_tools=()):
        return {"text": json.dumps({"decision": kind, "goal": "Complete the requested file operation", "pending_fields": [],
            "parameter_sources": [], "completed_steps": ["File operation finished"] if kind == "complete" else [],
            "remaining_steps": ["Execute the requested file operation"] if kind == "execute" else [],
            "next_tools": list(next_tools), "reply": reply})}
    phases = [decision("execute", next_tools=["touch", "echo"]),
        {"calls": [("touch", {"file_name": "memo.txt"}), ("echo", {"content": "alpha-" + str(index), "file_name": "memo.txt"})]},
        decision("complete", reply="Created."), decision("execute", next_tools=["cat"]),
        {"calls": [("cat", {"file_name": "memo.txt"})]}, decision("complete", reply="alpha-" + str(index))]
    if index == 19:
        phases = [{"text": "not-json"}]
'''
    else:
        phases = fixture[start:stop]
    fixture = fixture[:start] + phases + fixture[stop:]
    # The fixture does not alter Agent code; only the transport factory supplies
    # scripted complete HTTP responses, and its own audit hook denies external IO.
    (output / "fixture_provider.py").write_text(fixture, encoding="utf-8")
    assertion = dedent('''
        import hashlib
        import json
        from pathlib import Path

        def get_assert(output, context):
            compact = json.loads(output)
            task_id = context["vars"]["task_id"]
            root = Path(context["test"]["metadata"]["campaign_dir"])
            label = context["test"]["metadata"]["configuration_label"]
            from aster.evaluation.bfcl_multiturn_concurrent import read_task_receipt
            row = read_task_receipt(root, task_id)
            digest = hashlib.sha256(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
            assert compact["backend_result_sha256"] == digest and compact["id"] == row["id"] == task_id
            assert compact["main_score"] is None and compact["result"] == row["result"] == "unsupported"
            assert compact["model_api_calls"] == row["model_api_calls"] == 0
            assert compact["configuration_label"] == label
            if label == "A3" and task_id.endswith("_19"):
                assert row["verification_result"] == "fail" and row["agent_control_failure"] is True
                assert row["http_attempts"] == 1
                assert row["runs"][0]["runtime_diagnostic"]["error_type"] == "TaskControlError"
            else:
                assert row["verification_result"] == row["protocol_result"] == "pass"
                assert row["scoring_verification"]["valid"] is True
                expected = 4 if label == "A0" else 6 if label == "A3" else 7 if "miss_func" in task_id else 6
                assert row["http_attempts"] == expected
            return {"pass": True, "score": 1, "reason": "Frozen-runtime structural MockTransport check only"}
    ''')
    (output / "fixture_assertion.py").write_text(assertion, encoding="utf-8")
    config = launcher.build_config(plan, campaign, asset_root=project)
    config["providers"][0]["id"] = "file://" + (output / "fixture_provider.py").as_posix()
    config["defaultTest"] = {"assert": [{"type": "python", "metric": "offline_structure_only",
        "value": "file://fixture_assertion.py"}]}
    write(output / "promptfooconfig.json", config)
    (output / "empty.env").write_text("", encoding="utf-8")
    env = launcher.child_environment(output, control, asset_root=project)
    command = launcher.cli_command(output, 20, asset_root=project)
    process = subprocess.Popen(command, cwd=output, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
    try:
        stdout, stderr = process.communicate(timeout=210)
    except subprocess.TimeoutExpired:
        if process.poll() is None:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, timeout=15)
        process.communicate(timeout=10)
        raise
    (output / "native.stdout.txt").write_text(stdout, encoding="utf-8")
    (output / "native.stderr.txt").write_text(stderr, encoding="utf-8")
    if process.returncode != 0:
        failure = {"configuration_label": args.configuration, "native_exit_code": process.returncode,
                   "model_api_calls": 0, "test_only": True}
        if (output / "results.json").exists():
            failed_native = json.loads((output / "results.json").read_bytes())["results"]
            failure["stats"] = failed_native.get("stats")
            failure["rows"] = [{"task_id": item.get("vars", {}).get("task_id"),
                "error": item.get("error"), "grading": item.get("gradingResult"),
                "output": item.get("response", {}).get("output")}
                for item in failed_native.get("results", [])]
        write(project / "artifacts/bfcl/holdout60-promptfoo-validation-20261030" /
              ("native-failure-" + args.configuration + "-" + root.name + ".json"), failure)
    assert process.returncode == 0, f"Native fixture returned {process.returncode}; inspect local evidence"
    native = json.loads((output / "results.json").read_bytes())["results"]
    assert native["stats"]["successes"] == len(native["results"]) == 20
    assert native["stats"]["failures"] == native["stats"]["errors"] == 0
    report = backend.finalize_campaign(campaign)
    expected_calls = {"A0": 80, "A3": 115, "MF-Gate-v1": 130}[args.configuration]
    assert report["http_transport_attempts"] == expected_calls and report["model_api_calls"] == 0
    assert report["fatal"] is None and len(report["results"]) == 20
    assert len({row["session_id"] for row in report["results"]}) == 20
    arrivals = [json.loads(p.read_bytes()) for p in (campaign / "mock-arrivals").glob("*.json")]
    assert len(arrivals) == len({item["pid"] for item in arrivals}) == 20
    starts = [json.loads(p.read_bytes()) for p in (output / "dispatches").glob("*.started.json")]
    ends = [json.loads(p.read_bytes()) for p in (output / "dispatches").glob("*.finished.json")]
    assert len(starts) == len(ends) == 20
    active = peak = 0
    for _time, change in sorted([(p["started_monotonic"], 1) for p in starts]
                                + [(p["finished_monotonic"], -1) for p in ends]):
        active += change
        peak = max(peak, active)
    assert peak == 20
    proof_hash = identity.digest(campaign / "runtime-import-identity.json")
    assert all(p["runtime_import_identity_sha256"] == proof_hash and p["runtime_import_identity"] == proof for p in starts + ends)
    mocks = [json.loads(p.read_bytes()) for p in output.glob("mock-guard.*.json")]
    guards = [json.loads(p.read_bytes()) for p in output.glob("*-guard.*.json")]
    assert len(mocks) == 20 and all(p["offline_guards_passed"] for p in mocks)
    assert all(p.get("blocked_attempts", p.get("external_attempts", 0)) == 0 for p in guards)
    assert source_before == {p.relative_to(source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                             for p in source.rglob("*") if p.is_file()}
    evidence = {"configuration_label": args.configuration, "status": "passed", "native_promptfoo_version": "0.125.1",
        "same_campaign": True, "python_workers": 20, "independent_sessions": 20, "peak_overlapping_provider_tasks": peak,
        "mock_transport_requests": expected_calls, "model_api_calls": 0, "judge_api_calls": 0,
        "actual_frozen_runtime_imports": proof, "all_callback_import_proofs_match": True,
        "explicit_A3_control_failure_fixture": args.configuration == "A3", "capability_score": None,
        "new_holdout_inference_tasks": 0, "all_mock_worker_guards_passed": True,
        "native_guard_blocked_attempts": 0, "source_runtime_unchanged": True}
    write(root / "native-evidence.json", evidence)
    print(json.dumps({key: evidence[key] for key in ("configuration_label", "status", "python_workers",
        "mock_transport_requests", "model_api_calls", "explicit_A3_control_failure_fixture")}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
