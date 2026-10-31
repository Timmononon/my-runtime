"""Native single-task supplement bridge; all six responses are MockTransport."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from textwrap import dedent

import pytest
from bfcl_test_prerequisites import require_native

PROJECT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.bfcl_native


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def native_worker(root):
    workspace = root / "runtime-workspace"
    sys.path.insert(0, str(PROJECT / "tests"))
    from test_bfcl_current import source_at

    from aster.evaluation import bfcl_multiturn_concurrent as backend
    from aster.evaluation.bfcl_current import capture_current_profile

    assert Path(backend.__file__).resolve().is_relative_to(workspace / "src")
    adapter = workspace / "evals/bfcl_multiturn/promptfoo"
    supplement = load("native_supplement_adapter", adapter / "run_holdout_zero_dispatch_supplement.py")
    proof, case, group = supplement.verify_original(PROJECT)
    launcher = load("native_supplement_parent_harness", adapter / "run_compact_holdout.py")
    launcher = supplement.bind_adapter(launcher, proof, case, group)
    identity = load("native_supplement_identity", adapter / "holdout_identity.py")
    source = source_at(root)
    profile = capture_current_profile(source, model="deepseek-flash", reasoning_effort="none")
    campaign = root / "campaign"
    campaign.mkdir()
    write(adapter / "zero-dispatch-eligibility.json", proof)
    write(campaign / "qualification-proof.json", proof)
    runtime_proof = identity.capture_runtime_identity("A3", workspace)
    write(campaign / "runtime-import-identity.json", runtime_proof)
    plan = launcher.preflight("A3", 20)
    plan.update(campaign=campaign.name, frozen_source_commit=supplement.SOURCE_COMMIT,
        runtime_workspace=str(workspace), frozen_runtime_manifest_sha256=identity.digest(workspace / "frozen-runtime-manifest.json"),
        runtime_import_identity_sha256=identity.digest(campaign / "runtime-import-identity.json"),
        test_scope="One real task membership and frozen runtime, scripted clarify decisions, no model ability measurement")
    write(campaign / "campaign-plan.json", plan)
    launcher._snapshot(campaign)
    control = backend.initialize_campaign([case], campaign, profile=profile, maximum_concurrency=20,
        _test_only=True, agent_policy="readiness_state_v1")
    output = campaign / "promptfoo"
    (output / "dispatches").mkdir(parents=True)
    fixture = dedent('''
        import asyncio, atexit, importlib.util, json, os, sys, types
        from pathlib import Path
        import httpx
        WORKSPACE = Path(__WORKSPACE__)
        SOURCE = Path(__SOURCE__)
        CAMPAIGN = Path(__CAMPAIGN__)
        sys.dont_write_bytecode = True
        guard_path = Path(__GUARD__)
        guard_module = types.ModuleType("supplement_mock_offline_guard")
        guard_module.__file__ = str(guard_path)
        exec(compile(guard_path.read_bytes(), str(guard_path), "exec"), guard_module.__dict__)
        guard = guard_module.OfflineGuard(protected_root=__DEFAULT_RUNTIME__)
        guard.install()
        def finish():
            (CAMPAIGN / "promptfoo/mock-guard.json").write_text(json.dumps(guard.summary()), encoding="utf-8")
        atexit.register(finish)
        spec = importlib.util.spec_from_file_location("supplement_real_provider", WORKSPACE / "evals/bfcl_multiturn/promptfoo/holdout_provider.py")
        provider = importlib.util.module_from_spec(spec); spec.loader.exec_module(provider)
        from aster.evaluation import bfcl_multiturn_concurrent as backend
        from aster.evaluation.bfcl_current import capture_current_profile
        original_run = backend.run_one_current_multiturn
        async def fixture_run(case_id, campaign_dir):
            assert case_id == "multi_turn_miss_func_33" and Path(campaign_dir).resolve() == CAMPAIGN
            state = json.loads((CAMPAIGN / "execution-control.json").read_bytes())
            assert state["test_only"] is True and state["case_ids"] == [case_id]
            profile = capture_current_profile(SOURCE, model="deepseek-flash", reasoning_effort="none")
            async def handle(request):
                body = json.loads(request.content)
                assert request.method == "POST" and request.url.path == "/responses"
                assert body["model"] == "deepseek-flash" and body["reasoning"] == {"effort": "none"}
                assert body["max_output_tokens"] == 8192 and not body.get("tools")
                verdict = {"decision":"clarify", "goal":"Complete the current user request", "pending_fields":["details"],
                    "parameter_sources":[], "completed_steps":[], "remaining_steps":[], "next_tools":[],
                    "reply":"Which required detail should I use?"}
                event = {"type":"response.completed", "response":{"status":"completed", "output":[
                    {"type":"message", "content":[{"type":"output_text", "text":json.dumps(verdict)}]}],
                    "usage":{"input_tokens":30, "output_tokens":7, "output_tokens_details":{"reasoning_tokens":0}}}}
                return httpx.Response(200, request=request, headers={"content-type":"text/event-stream"},
                    text="event: response.completed\\ndata: " + json.dumps(event) + "\\n\\n")
            return await original_run(case_id, campaign_dir, _profile=profile, _test_transport_factory=lambda:httpx.MockTransport(handle))
        backend.run_one_current_multiturn = fixture_run
        def call_api(prompt, options, context):
            return provider.call_api(prompt, options, context)
    ''')
    for key, value in {"__WORKSPACE__": workspace, "__SOURCE__": source, "__CAMPAIGN__": campaign,
                       "__GUARD__": PROJECT / "artifacts/bfcl/v4-integration-20261029/offline_gate.py",
                       "__DEFAULT_RUNTIME__": Path.home() / ".aster"}.items():
        fixture = fixture.replace(key, repr(str(value)))
    (output / "fixture_provider.py").write_text(fixture, encoding="utf-8")
    assertion = dedent('''
        import json
        from pathlib import Path
        def get_assert(output, context):
            compact = json.loads(output)
            from aster.evaluation.bfcl_multiturn_concurrent import read_task_receipt
            row = read_task_receipt(Path(context["test"]["metadata"]["campaign_dir"]), context["vars"]["task_id"])
            assert row["id"] == compact["id"] == "multi_turn_miss_func_33"
            assert row["result"] == "unsupported" and row["verification_result"] == "fail"
            assert row["model_api_calls"] == 0 and row["http_attempts"] == 6
            assert len(row["runs"]) == 6 and all(run["runtime_status"] == "completed" for run in row["runs"])
            assert all(run["model_request_purposes"] == {"task_control":1} for run in row["runs"])
            return {"pass":True, "score":1, "reason":"Single-task frozen ABI/selection/guard verification; no ability score"}
    ''')
    (output / "fixture_assertion.py").write_text(assertion, encoding="utf-8")
    config = launcher.build_config(plan, campaign, asset_root=PROJECT)
    assert len(config["tests"]) == 1 and config["tests"][0]["vars"] == {"task_id": supplement.TASK_ID}
    config["providers"][0]["id"] = "file://" + (output / "fixture_provider.py").as_posix()
    config["defaultTest"] = {"assert":[{"type":"python", "metric":"offline_supplement_structure", "value":"file://fixture_assertion.py"}]}
    write(output / "promptfooconfig.json", config)
    (output / "empty.env").write_text("", encoding="utf-8")
    completed = subprocess.run(launcher.cli_command(output, 20, asset_root=PROJECT), cwd=output,
        env=launcher.child_environment(output, control, asset_root=PROJECT), capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=150, check=False)
    assert completed.returncode == 0, completed.stdout[-2000:] + completed.stderr[-2000:]
    native = json.loads((output / "results.json").read_bytes())["results"]
    assert native["stats"]["successes"] == len(native["results"]) == 1
    report = backend.finalize_campaign(campaign)
    assert len(report["results"]) == 1 and report["http_transport_attempts"] == 6 and report["model_api_calls"] == 0
    guard = json.loads((output / "mock-guard.json").read_bytes())
    assert guard["offline_guards_passed"] is True
    starts = list((output / "dispatches").glob("*.started.json"))
    assert len(starts) == 1
    observed = json.loads(starts[0].read_bytes())
    assert observed["runtime_import_identity"] == runtime_proof
    assert observed["runtime_import_identity_sha256"] == plan["runtime_import_identity_sha256"]
    supplement.verify_original(PROJECT)
    evidence = {"status":"passed", "configuration_label":"A3", "task_id":supplement.TASK_ID,
        "native_promptfoo_version":"0.125.1", "planned_tasks":1, "authority_holdout_tasks":60,
        "maximum_configured_concurrency":20, "actual_overlapping_tasks":1, "Mock_HTTP_requests":6,
        "complete_user_turns":6, "model_api_calls":0, "judge_api_calls":0, "new_holdout_model_measurements":0,
        "original_campaign_files_verified_unchanged":1278, "runtime_import_identity":runtime_proof,
        "callback_import_witness_matches":True, "all_guards_passed":True, "capability_score":None}
    write(root / "native-supplement-evidence.json", evidence)
    return 0


@pytest.mark.windows
@pytest.mark.skipif(os.name != "nt", reason="Native Windows orchestration")
def test_single_native_supplement_uses_only_one_source_bound_case(record_property):
    boundary = PROJECT / "artifacts/bfcl"
    with tempfile.TemporaryDirectory(prefix="compact-holdout60-a3-zero-dispatch-supplement-native-", dir=boundary) as temporary:
        root = Path(temporary).resolve()
        assert root.parent == boundary.resolve()
        builder = load("supplement_test_builder", PROJECT / "evals/bfcl_multiturn/promptfoo/frozen_holdout_runtime.py")
        workspace = root / "runtime-workspace"
        builder.materialize_runtime("A3", workspace)
        env = dict(os.environ)
        env.update(PYTHONPATH=str(workspace / "src"), PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1")
        completed = subprocess.run([str(PROJECT / ".venv/Scripts/python.exe"), "-u", str(Path(__file__).resolve()),
            "--native-worker", str(root)], cwd=workspace, env=env, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=200, check=False)
        assert completed.returncode == 0, completed.stdout[-1500:] + completed.stderr[-2500:]
        evidence = json.loads((root / "native-supplement-evidence.json").read_bytes())
        destination = PROJECT / "artifacts/bfcl/holdout60-a3-zero-dispatch-forensic-20261031/native-supplement-evidence.json"
        with destination.open("x", encoding="utf-8") as stream:
            json.dump(evidence, stream, sort_keys=True, ensure_ascii=False, indent=2)
            stream.write("\n")
        record_property("native_offline_evidence", json.dumps(evidence, sort_keys=True))


if __name__ == "__main__":
    assert len(sys.argv) == 3 and sys.argv[1] == "--native-worker"
    raise SystemExit(native_worker(Path(sys.argv[2])))


@pytest.fixture(scope="module", autouse=True)
def _native_prerequisites():
    require_native(promptfoo=True, frozen=True)
