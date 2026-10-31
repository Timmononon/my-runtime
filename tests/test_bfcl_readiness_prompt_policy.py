"""A4.1 native prompt integration; all transports and user state are fixtures."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from bfcl_test_prerequisites import require_corpus
from test_bfcl_current import source_at
from test_bfcl_multiturn_concurrent import case_at, phases
from test_bfcl_promptfoo_compact_development import collected as compact_collected
from test_bfcl_promptfoo_compact_development import native_provider
from test_bfcl_v4_live import response

from aster.engine.readiness_prompt import POLICY, SYSTEM_PREAMBLE, policy_identity
from aster.evaluation import bfcl_multiturn_concurrent as backend
from aster.evaluation.bfcl_current import capture_current_profile

PROJECT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("a41_launcher_test", PROJECT /
    "evals/bfcl_multiturn/promptfoo/run_compact_development_a41.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def setup(tmp_path, case=None, policy=POLICY):
    tmp_path.mkdir(parents=True, exist_ok=True)
    profile = capture_current_profile(source_at(tmp_path), model="deepseek-flash", reasoning_effort="none")
    root = tmp_path / "campaign"
    control = backend.initialize_campaign([case or case_at(0)], root, profile=profile,
        maximum_concurrency=20, _test_only=True, agent_policy=policy)
    return root, profile, control


def test_prompt_identity_is_explicit_legacy_shapes_stay_unchanged(tmp_path):
    _root, _profile, a0 = setup(tmp_path / "a0", policy=None)
    _root, _profile, a3 = setup(tmp_path / "a3", policy="readiness_state_v1")
    _root, _profile, a41 = setup(tmp_path / "a41")
    assert "agent_policy" not in a0 and "agent_policy_identity" not in a0
    assert a3["agent_policy"] == "readiness_state_v1" and "agent_policy_identity" not in a3
    assert a41["agent_policy"] == POLICY and a41["agent_policy_identity"] == policy_identity()
    assert a0["inherited_runtime_safety"] == a3["inherited_runtime_safety"] == a41["inherited_runtime_safety"]


def test_windows_capture_avoids_blocking_wmi_and_reports_local_method(monkeypatch):
    monkeypatch.setattr(runner._core.sys, "platform", "win32")
    monkeypatch.setattr(runner._core.sys, "getwindowsversion",
        lambda: SimpleNamespace(major=10, minor=0, build=12345), raising=False)

    def forbidden():
        raise AssertionError("Windows source capture must not query WMI")

    monkeypatch.setattr(runner._core.platform, "platform", forbidden)
    captured = runner._core._platform_metadata()
    assert captured["platform"] == "Windows-10.0.12345"
    assert captured["platform_capture"] == {"method": "sys.getwindowsversion", "sys_platform": "win32", "wmi": False}


@pytest.mark.asyncio
async def test_native_wire_has_prompt_tools_and_only_main_requests(tmp_path):
    case = case_at(0)
    case["provenance"] = {"private_marker": "PRIVATE_REFERENCE_NEVER_SENT"}
    root, profile, _control = setup(tmp_path, case)
    scripted, bodies = phases(0), []

    def handle(request):
        body = json.loads(request.content)
        assert "PRIVATE_REFERENCE_NEVER_SENT" not in request.content.decode()
        assert body["model"] == "deepseek-flash" and body["reasoning"] == {"effort": "none"}
        assert body["max_output_tokens"] == 8192 and body["tools"]
        assert body["instructions"].startswith(SYSTEM_PREAMBLE)
        index = len(bodies)
        bodies.append(body)
        return response(request, index=index, **scripted[index])

    row = await backend.run_one_current_multiturn(case["id"], root, _profile=profile,
        _test_transport_factory=lambda: httpx.MockTransport(handle))
    assert row["verification_result"] == "pass" and row["result"] == "unsupported"
    assert row["model_api_calls"] == 0 and row["http_attempts"] == len(bodies) == 4
    assert row["agent_policy_identity"] == policy_identity()
    for run in row["runs"]:
        assert run["runtime_prompt_policy"] == POLICY and run["runtime_task_control"] == "off"
        assert run["task_control"] is None and run["agent_control_failure"] is False
        assert run["agent_policy_identity"] == policy_identity()
        assert run["model_request_purposes"] == {"main": 2}
        assert len(run["model_request_receipts"]) == run["runtime_budget"]["model_requests"] == 2
    assert all(item["system"].startswith(SYSTEM_PREAMBLE) for item in row["step_observations"])
    assert backend.read_task_receipt(root, case["id"]) == row
    final = backend.finalize_campaign(root)
    assert final["agent_policy_identity"] == policy_identity() and final["model_api_calls"] == 0


@pytest.mark.asyncio
async def test_plain_clarification_has_zero_calls_and_native_followup_keeps_history(tmp_path):
    case = case_at(0)
    case["question"] = [[{"role": "user", "content": "Create a file containing alpha-0; ask me its filename."}],
        [{"role": "user", "content": "Use memo.txt."}]]
    case["expected"] = [[], ["touch(file_name='memo.txt')", "echo(content='alpha-0', file_name='memo.txt')"]]
    root, profile, _control = setup(tmp_path, case)
    scripted = [{"text": "Which filename?"},
        {"calls": [("touch", {"file_name": "memo.txt"}),
            ("echo", {"content": "alpha-0", "file_name": "memo.txt"})]}, {"text": "Created."}]
    bodies = []

    def handle(request):
        index = len(bodies)
        bodies.append(json.loads(request.content))
        return response(request, index=index, **scripted[index])

    row = await backend.run_one_current_multiturn(case["id"], root, _profile=profile,
        _test_transport_factory=lambda: httpx.MockTransport(handle))
    assert row["verification_result"] == "pass" and len(bodies) == 3
    assert row["runs"][0]["model_request_purposes"] == {"main": 1}
    assert row["runs"][1]["model_request_purposes"] == {"main": 2}
    assert row["runs"][0]["answer"] == "Which filename?"
    assert row["scoring_verification"]["turns"][0]["valid"] is True
    assert "Create a file containing alpha-0" in json.dumps(bodies[1])
    assert "Use memo.txt." in json.dumps(bodies[1])


@pytest.mark.asyncio
async def test_same_twenty_native_request_limit_is_not_relaxed(tmp_path):
    root, profile, _control = setup(tmp_path)
    bodies = []

    def handle(request):
        index = len(bodies)
        bodies.append(json.loads(request.content))
        return response(request, index=index,
            calls=[("echo", {"file_name": "memo.txt", "content": f"progress-{index}"})])

    row = await backend.run_one_current_multiturn("multi_turn_base_0", root, _profile=profile,
        _test_transport_factory=lambda: httpx.MockTransport(handle))
    assert len(bodies) == 20 and row["runs"][0]["runtime_status"] == "budget_exhausted"
    assert row["runs"][0]["model_request_purposes"] == {"main": 20}
    assert row["termination_kind"] == "official_per_turn_step_limit"
    assert runner._core._classification({**row, "result": "fail"}) == "budget_termination"


@pytest.mark.asyncio
async def test_changed_prompt_identity_rejects_before_constructing_transport(tmp_path):
    root, profile, _control = setup(tmp_path)
    path = root / "execution-control.json"
    value = json.loads(path.read_bytes())
    value["agent_policy_identity"]["prompt_sha256"] = "0" * 64
    path.write_text(json.dumps(value), encoding="utf-8")

    def forbidden():
        raise AssertionError("Changed prompt identity cannot create a transport")

    with pytest.raises(ValueError, match="prompt policy identity"):
        await backend.run_one_current_multiturn("multi_turn_base_0", root,
            _profile=profile, _test_transport_factory=forbidden)
    assert json.loads((root / "results/http-budget.json").read_bytes())["calls"] == []


def test_finalized_uninvoked_results_retain_identity_and_cannot_be_relabelled(tmp_path):
    root, _profile, _control = setup(tmp_path)
    final = backend.finalize_campaign(root)
    row = final["results"][0]
    assert row["result"] == "not_run" and row["agent_policy_identity"] == policy_identity()
    path = next((root / "results/receipts").glob("*.json"))
    saved = json.loads(path.read_bytes())
    saved["result"]["agent_policy_identity"]["prompt_sha256"] = "0" * 64
    path.write_text(json.dumps(saved), encoding="utf-8")
    with pytest.raises(ValueError, match="prompt policy identity"):
        backend.read_task_receipt(root, row["id"])


@pytest.fixture(scope="module")
def selection():
    require_corpus()
    return runner._core._dataset.load_development_dataset()


@pytest.fixture
def plan(selection, monkeypatch):
    monkeypatch.setattr(runner._core._dataset, "load_development_dataset", lambda: copy.deepcopy(selection))
    return runner.preflight(20)


@pytest.mark.bfcl_evidence
def test_offline_plan_has_exact_forty_and_twenty_way_dispatch(plan, selection, tmp_path, monkeypatch):
    import aster.paths

    def forbidden():
        raise AssertionError("Preflight cannot read the user Runtime")

    monkeypatch.setattr(aster.paths, "resolve_data_dir", forbidden)
    assert plan["agent_policy"] == POLICY and plan["agent_policy_identity"] == policy_identity()
    assert plan["configuration_label"] == "A4.1" and plan["defined_user_turns"] == 156
    assert plan["allocated_development_tasks"] == 40 and plan["allocated_holdout_tasks"] == 60
    assert plan["additional_planner_requests"] is False and plan["runtime_task_control"] == "off"
    config = runner.build_config(plan, tmp_path)
    assert len(config["tests"]) == 40
    assert [test["vars"]["task_id"] for test in config["tests"]] == [case["id"] for case in selection[0]]
    assert all(set(test["vars"]) == {"task_id"} for test in config["tests"])
    assert all(test["metadata"]["configuration_label"] == "A4.1" for test in config["tests"])
    assert config["evaluateOptions"]["maxConcurrency"] == config["providers"][0]["config"]["workers"] == 20
    assert runner.cli_command(tmp_path, 20)[-2:] == ["--max-concurrency", "20"]
    output = tmp_path / "preflight"
    assert runner.main(["--campaign-dir", str(output)]) == 0
    assert {path.name for path in output.iterdir()} == {"campaign-plan.json"}


@pytest.mark.bfcl_evidence
def test_paid_admission_freezes_prompt_and_stops_before_any_dispatch(plan, selection, tmp_path, monkeypatch):
    import aster.paths

    source, root = source_at(tmp_path), tmp_path / "paid"
    monkeypatch.setattr(aster.paths, "resolve_data_dir", lambda: source)
    monkeypatch.setattr(runner, "load_development_cases", lambda: copy.deepcopy(selection[0]))

    class StopBeforeDispatch(RuntimeError):
        pass

    def stop(command, **kwargs):
        assert command[-2:] == ["--max-concurrency", "20"]
        assert kwargs["env"]["PROMPTFOO_PYTHON_WORKERS"] == "20"
        raise StopBeforeDispatch()

    monkeypatch.setattr(runner.subprocess, "run", stop)
    with pytest.raises(StopBeforeDispatch):
        runner.main(["--campaign-dir", str(root), "--execute"])
    control = json.loads((root / "execution-control.json").read_bytes())
    assert control["agent_policy_identity"] == policy_identity() and control["test_only"] is False
    assert control["case_ids"] == [case["id"] for case in selection[0]]
    assert control["concurrent_tasks"]["maximum"] == 20
    assert json.loads((root / "results/http-budget.json").read_bytes())["calls"] == []
    files = {item["file"] for item in json.loads((root / "source-manifest.json").read_bytes())["files"]}
    assert "src/aster/engine/readiness_prompt.py" in files
    assert "evals/bfcl_multiturn/promptfoo/run_compact_development_a41.py" in files


@pytest.fixture
def collected(plan, tmp_path, monkeypatch):
    root, plan, payload, output, rows = compact_collected.__wrapped__(plan, tmp_path, monkeypatch)
    control = json.loads((root / "execution-control.json").read_bytes())
    control.update(agent_policy=POLICY, agent_policy_identity=policy_identity())
    (root / "execution-control.json").write_text(json.dumps(control), encoding="utf-8")
    ledger = json.loads((root / "results/http-budget.json").read_bytes())
    original = {call["case_id"]: call for call in ledger["calls"]}
    ledger["calls"] = []
    for row in rows:
        row.update(agent_policy=POLICY, agent_policy_identity=policy_identity())
        count = len(row["runs"])
        row["runs"] = [{"turn_index": index, "runtime_status": "completed", "runtime_budget": {"model_requests": 1},
            "model_request_purposes": {"main": 1}, "model_request_receipts": [{"purpose": "main"}],
            "runtime_prompt_policy": POLICY, "runtime_task_control": "off", "task_control": None,
            "agent_policy_identity": policy_identity()} for index in range(count)]
        row["step_observations"] = [{"turn_index": index, "system": SYSTEM_PREAMBLE + "Synthetic context"}
            for index in range(count)]
        row["model_api_calls"] = row["http_attempts"] = count
        row["usage"] *= count
        ledger["calls"].extend(copy.deepcopy(original[row["id"]]) for _ in range(count))
        key = runner._helpers.digest(row["id"].encode())[:24]
        for suffix in (".started.json", ".finished.json"):
            path = output / "dispatches" / (key + suffix)
            value = json.loads(path.read_bytes())
            value["http_attempts_for_task"] = count
            path.write_text(json.dumps(value), encoding="utf-8")
        item = next(item for item in payload["results"]["results"] if item["vars"]["task_id"] == row["id"])
        item["response"] = native_provider.render_response(row)
    (root / "results/http-budget.json").write_text(json.dumps(ledger), encoding="utf-8")
    _save_rows(root, rows)
    return root, plan, payload, output, rows


def _save_rows(root, rows):
    (root / "results/results.json").write_text(json.dumps({"results": rows, "dataset_binding": "fixture-binding",
        "model_api_calls": sum(row["model_api_calls"] for row in rows), "max_api_calls": None}), encoding="utf-8")


@pytest.mark.bfcl_evidence
def test_collector_retains_official_fixed_denominator_and_native_usage(collected):
    root, plan, payload, output, _rows = collected
    report = runner.collect_campaign(root, plan, payload, output)
    assert report["evidence_errors"] == []
    assert report["main_success_rate"] == .5 and report["scorable_tasks"] == 40
    assert report["model_api_calls"] == report["logical_model_request_reservations"] == 156
    assert report["controller_model_requests"] == 0 and report["action_model_requests"] == 156
    assert report["agent_policy_identity"] == policy_identity()
    assert report["baseline_assessment"]["capability_baseline_ready"] is True


@pytest.mark.parametrize("tamper", ["observed_prefix", "helper_request", "runtime_policy", "missing_observation"])
@pytest.mark.bfcl_evidence
def test_collector_refuses_declared_identity_without_actual_native_evidence(collected, tamper):
    root, plan, payload, output, rows = collected
    row, run = rows[0], rows[0]["runs"][0]
    if tamper == "observed_prefix":
        row["step_observations"][0]["system"] = "A different prompt"
    elif tamper == "helper_request":
        run["model_request_purposes"] = {"task_control": 1}
        run["model_request_receipts"][0]["purpose"] = "task_control"
    elif tamper == "runtime_policy":
        run["runtime_task_control"] = "readiness_state_v1"
    else:
        row["step_observations"].pop()
    _save_rows(root, rows)
    report = runner.collect_campaign(root, plan, payload, output)
    assert report["evidence_errors"] and report["main_success_rate"] is None
    assert report["baseline_assessment"]["capability_baseline_ready"] is False


@pytest.mark.bfcl_evidence
def test_started_candidate_is_never_resumed_or_overwritten(plan, tmp_path):
    root = tmp_path / "started"
    root.mkdir()
    marker = root / "execution-started.json"
    marker.write_text("original", encoding="utf-8")
    with pytest.raises(SystemExit):
        runner.main(["--campaign-dir", str(root), "--execute"])
    assert marker.read_text() == "original" and not (root / "results").exists()
