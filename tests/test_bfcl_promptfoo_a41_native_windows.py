"""Twenty native promptfoo workers exercise A4.1 with offline synthetic tasks."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest
from bfcl_test_prerequisites import require_native
from test_bfcl_current import source_at
from test_bfcl_multiturn_concurrent import case_at
from test_bfcl_promptfoo_concurrent_native_windows import (
    FIXTURE_ASSERTION,
    FIXTURE_PROVIDER,
    _module,
    _owned_cli,
)

from aster.engine.readiness_prompt import POLICY, SYSTEM_PREAMBLE, policy_identity
from aster.evaluation import bfcl_multiturn_concurrent as backend
from aster.evaluation.bfcl_current import capture_current_profile

PROJECT = Path(__file__).resolve().parents[1]
pytestmark = [pytest.mark.bfcl_native, pytest.mark.windows, pytest.mark.skipif(os.name != "nt", reason="Native Windows workers")]


@pytest.fixture
def native_root():
    boundary = (PROJECT / "artifacts/bfcl").resolve()
    directory = Path(tempfile.mkdtemp(prefix="n41-", dir=boundary)).resolve()
    assert directory.parent == boundary and directory.name.startswith("n41-")
    try:
        yield directory
    finally:
        assert directory.resolve().parent == boundary and directory.name.startswith("n41-")
        shutil.rmtree(directory)


def test_twenty_native_workers_share_prompt_candidate_and_never_call_external_models(native_root, record_property):
    root = native_root
    assert shutil.which("node")
    launcher = _module("run_compact_development_a41")
    source = source_at(root)
    source_before = {path.relative_to(source).as_posix(): path.read_bytes()
                     for path in source.rglob("*") if path.is_file()}
    profile = capture_current_profile(source, model="deepseek-flash", reasoning_effort="none")
    campaign = root / "campaign"
    campaign.mkdir()
    cases = [case_at(index) for index in range(20)]
    plan = launcher.preflight(20)
    plan.update(campaign=campaign.name, groups=[{"group": case["id"], "entry_ids": [case["id"]],
        "category": "multi_turn_base", "user_turns": len(case["question"])} for case in cases],
        test_scope="Twenty synthetic fixture tasks, not development predictions")
    (campaign / "campaign-plan.json").write_text(json.dumps(plan), encoding="utf-8")
    control = backend.initialize_campaign(cases, campaign, profile=profile, maximum_concurrency=20,
        _test_only=True, agent_policy=POLICY)
    output = campaign / "promptfoo"
    (output / "dispatches").mkdir(parents=True)
    (campaign / "mock-arrivals").mkdir()
    (campaign / "mock-requests").mkdir()
    fixture = FIXTURE_PROVIDER.replace("__PROJECT__", repr(str(PROJECT))).replace(
        "__DEFAULT_RUNTIME__", repr(str(Path.home() / ".aster")))
    fixture = fixture.replace('state["concurrent_tasks"]["maximum"] == 5',
        'state["concurrent_tasks"]["maximum"] == 20')
    fixture = fixture.replace('glob("*.json"))) != 5', 'glob("*.json"))) != 20')
    fixture = fixture.replace("Five native workers", "Twenty native workers").replace("time.monotonic() + 35", "time.monotonic() + 70")
    fixture = fixture.replace('assert body["max_output_tokens"] == 8192',
        'assert body["max_output_tokens"] == 8192\n        '
        'from aster.engine.readiness_prompt import SYSTEM_PREAMBLE\n        '
        'assert body["instructions"].startswith(SYSTEM_PREAMBLE)')
    (output / "fixture_provider.py").write_text(fixture, encoding="utf-8")
    (output / "fixture_assertion.py").write_text(FIXTURE_ASSERTION, encoding="utf-8")
    config = launcher.build_config(plan, campaign)
    assert config["providers"][0]["config"]["workers"] == config["evaluateOptions"]["maxConcurrency"] == 20
    config["providers"][0]["id"] = "file://" + (output / "fixture_provider.py").as_posix()
    config["defaultTest"] = {"assert": [{"type": "python", "metric": "offline_structure_only",
        "value": "file://fixture_assertion.py"}]}
    (output / "promptfooconfig.json").write_text(json.dumps(config), encoding="utf-8")
    (output / "empty.env").write_text("", encoding="utf-8")
    env = launcher.child_environment(output, control)
    env["PROMPTFOO_PYTHON_WORKERS"] = "20"
    command = launcher.cli_command(output, 20)
    assert command[-2:] == ["--max-concurrency", "20"]
    code, stdout, stderr = _owned_cli(command, cwd=output, env=env)
    (output / "native.stdout.txt").write_text(stdout, encoding="utf-8")
    (output / "native.stderr.txt").write_text(stderr, encoding="utf-8")
    assert code == 0, f"Native A4.1 fixture exited {code}"
    native = json.loads((output / "results.json").read_bytes())["results"]
    assert native["stats"]["successes"] == len(native["results"]) == 20
    assert native["stats"]["errors"] == native["stats"]["failures"] == 0
    for row in native["results"]:
        compact = json.loads(row["response"]["output"])
        assert compact["main_score"] is None and compact["model_api_calls"] == 0
        assert compact["result"] == "unsupported" and row["success"] is True
    report = backend.finalize_campaign(campaign)
    assert report["http_transport_attempts"] == report["wire_policy_verified_calls"] == 80
    assert report["model_api_calls"] == 0 and report["fatal"] is None
    assert report["agent_policy_identity"] == policy_identity()
    assert len(report["results"]) == len({row["session_id"] for row in report["results"]}) == 20
    for row in report["results"]:
        assert row["verification_result"] == row["protocol_result"] == "pass"
        assert row["result"] == "unsupported" and row["http_attempts"] == 4
        assert row["agent_policy_identity"] == policy_identity()
        assert all(item["system"].startswith(SYSTEM_PREAMBLE) for item in row["step_observations"])
        for run in row["runs"]:
            assert run["runtime_prompt_policy"] == POLICY and run["runtime_task_control"] == "off"
            assert run["model_request_purposes"] == {"main": 2} and run["task_control"] is None
    assert len(list(campaign.rglob("execution-control.json"))) == len(list(campaign.rglob("http-budget.json"))) == 1
    arrivals = [json.loads(path.read_bytes()) for path in (campaign / "mock-arrivals").glob("*.json")]
    assert len(arrivals) == len({row["pid"] for row in arrivals}) == 20
    starts = [json.loads(path.read_bytes()) for path in (output / "dispatches").glob("*.started.json")]
    finishes = [json.loads(path.read_bytes()) for path in (output / "dispatches").glob("*.finished.json")]
    assert len(starts) == len(finishes) == len({row["pid"] for row in starts}) == 20
    active = peak = 0
    for _, change in sorted([(row["started_monotonic"], 1) for row in starts]
                            + [(row["finished_monotonic"], -1) for row in finishes]):
        active += change
        peak = max(peak, active)
    assert peak == 20
    guards = [json.loads(path.read_bytes()) for path in output.glob("*-guard.*.json")]
    mocks = [json.loads(path.read_bytes()) for path in output.glob("mock-guard.*.json")]
    assert len(mocks) == 20 and all(row["offline_guards_passed"] for row in mocks)
    assert all(row.get("blocked_attempts", row.get("external_attempts", 0)) == 0 for row in guards)
    assert any(path.name.startswith("node-guard.") for path in output.glob("*-guard.*.json"))
    assert {"persistent_wrapper.py", "wrapper.py"} <= {row.get("entry_script") for row in guards}
    assert source_before == {path.relative_to(source).as_posix(): path.read_bytes()
                             for path in source.rglob("*") if path.is_file()}
    evidence = {
        "native_promptfoo_version": "0.125.1", "native_exit_code": code, "same_campaign": True,
        "python_workers": 20, "peak_overlapping_provider_tasks": peak,
        "mock_transport_barrier_participants": len(arrivals), "mock_transport_requests": 80,
        "independent_sessions": 20, "single_control_file": True, "single_http_ledger": True,
        "agent_policy_identity": policy_identity(), "controller_requests": 0,
        "all_official_fixture_checks_valid": True, "model_api_calls": 0, "judge_api_calls": 0,
        "capability_score": None, "source_runtime_unchanged": True,
        "mock_worker_guards": mocks, "native_guard_blocked_attempts": 0,
        "scope": "Native CLI/production provider/new prompt policy; synthetic fixtures only"}
    record_property("native_offline_evidence", json.dumps(evidence, sort_keys=True))
    print(json.dumps({"native_offline_evidence": evidence}, sort_keys=True))


@pytest.fixture(scope="module", autouse=True)
def _native_prerequisites():
    require_native(promptfoo=True, frozen=False)
