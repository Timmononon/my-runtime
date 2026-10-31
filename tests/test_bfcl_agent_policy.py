"""Opt-in Agent controls retain official boundaries and legacy admission."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

import httpx
import pytest
from bfcl_test_prerequisites import require_corpus
from test_bfcl_current import source_at
from test_bfcl_multiturn_concurrent import case_at
from test_bfcl_promptfoo_compact_development import collected as compact_collected
from test_bfcl_promptfoo_compact_development import native_provider
from test_bfcl_v4_live import response

from aster.evaluation import bfcl_multiturn_concurrent as backend
from aster.evaluation.bfcl_current import capture_current_profile

POLICY = "readiness_state_v1"
PROJECT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("a3_launcher_test", PROJECT /
    "evals/bfcl_multiturn/promptfoo/run_compact_development_a3.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def decision(kind, *, next_tools=None, reply="", pending_fields=None):
    return {"decision": kind, "goal": "Complete the current file request",
        "pending_fields": pending_fields or [], "parameter_sources": [], "completed_steps": [],
        "remaining_steps": ["Perform next declared step"] if kind == "execute" else [],
        "next_tools": next_tools or [], "reply": reply}


def setup(tmp_path, case=None, policy=POLICY):
    tmp_path.mkdir(parents=True, exist_ok=True)
    profile = capture_current_profile(source_at(tmp_path), model="deepseek-flash", reasoning_effort="none")
    root = tmp_path / "campaign"
    control = backend.initialize_campaign([case or case_at(0)], root, profile=profile,
        maximum_concurrency=20, _test_only=True, agent_policy=policy)
    return root, profile, control


def test_policy_is_explicit_and_legacy_none_preserves_binding_shape(tmp_path):
    _root, _profile, legacy = setup(tmp_path / "old", policy=None)
    _root, _profile, changed = setup(tmp_path / "new")
    assert "agent_policy" not in legacy and changed["agent_policy"] == POLICY
    assert legacy["dataset_binding"] != changed["dataset_binding"]
    assert legacy["inherited_runtime_safety"] == changed["inherited_runtime_safety"]


def test_unknown_policy_rejects_without_creating_campaign(tmp_path):
    profile = capture_current_profile(source_at(tmp_path), model="deepseek-flash", reasoning_effort="none")
    root = tmp_path / "rejected"
    with pytest.raises(ValueError, match="Agent execution policy"):
        backend.initialize_campaign([case_at(0)], root, profile=profile,
            maximum_concurrency=20, _test_only=True, agent_policy="arbitrary")
    assert not root.exists()


def test_only_explicit_known_control_failure_is_an_agent_failure():
    row = {"result": "fail", "agent_control_failure": True,
        "runs": [{"runtime_status": "failed", "agent_control_failure": True,
            "runtime_diagnostic": {"error_type": "TaskControlError"}}]}
    assert runner._classification(row) == "agent_control_failure"
    assert runner._runtime_infrastructure(row) is False
    assert runner._core._classification(row) == "infrastructure_error"
    unknown = copy.deepcopy(row)
    unknown["runs"][0]["runtime_diagnostic"]["error_type"] = "StateError"
    assert runner._classification(unknown) == "infrastructure_error"
    unflagged = copy.deepcopy(row)
    unflagged.pop("agent_control_failure")
    assert runner._classification(unflagged) == "infrastructure_error"


@pytest.mark.asyncio
async def test_policy_tamper_is_detected_before_transport(tmp_path):
    root, profile, _control = setup(tmp_path)
    path = root / "execution-control.json"
    control = json.loads(path.read_bytes())
    control.pop("agent_policy")
    path.write_text(json.dumps(control), encoding="utf-8")

    def forbidden():
        raise AssertionError("A changed policy cannot construct HTTP transport")

    with pytest.raises(ValueError, match="policy differs"):
        await backend.run_one_current_multiturn("multi_turn_base_0", root,
            _profile=profile, _test_transport_factory=forbidden)


@pytest.mark.asyncio
async def test_controller_and_actions_use_one_real_protocol_and_account_every_request(tmp_path):
    case = case_at(0)
    case["provenance"] = {"private_marker": "PRIVATE_ORACLE_ONLY"}
    root, profile, _control = setup(tmp_path, case)
    phases = [
        {"text": json.dumps(decision("execute", next_tools=["touch", "echo"]))},
        {"calls": [("touch", {"file_name": "memo.txt"}),
            ("echo", {"content": "alpha-0", "file_name": "memo.txt"})]},
        {"text": json.dumps(decision("complete", reply="Created."))},
        {"text": json.dumps(decision("execute", next_tools=["cat"]))},
        {"calls": [("cat", {"file_name": "memo.txt"})]},
        {"text": json.dumps(decision("complete", reply="alpha-0"))},
    ]
    bodies = []

    def handle(request):
        body = json.loads(request.content)
        assert "PRIVATE_ORACLE_ONLY" not in request.content.decode()
        assert body["model"] == "deepseek-flash" and body["reasoning"] == {"effort": "none"}
        assert body["max_output_tokens"] == 8192
        index = len(bodies)
        bodies.append(body)
        assert bool(body.get("tools")) is (index in (1, 4))
        return response(request, index=index, **phases[index])

    row = await backend.run_one_current_multiturn(case["id"], root, _profile=profile,
        _test_transport_factory=lambda: httpx.MockTransport(handle))
    assert row["verification_result"] == "pass" and row["result"] == "unsupported"
    assert row["agent_policy"] == POLICY and len(bodies) == row["http_attempts"] == 6
    for run in row["runs"]:
        assert run["runtime_status"] == "completed"
        assert run["model_request_purposes"] == {"task_control": 2, "main": 1}
        assert run["runtime_budget"]["model_requests"] == 3
        assert run["task_control"]["controller_requests"] == 2
        assert run["task_control"]["main_requests"] == 1
        assert len(run["model_request_receipts"]) == 3
    assert not any(call["name"].startswith("task_") for message in row["messages"]
        for call in message.get("tool_calls", []))


@pytest.mark.asyncio
async def test_clarification_uses_zero_tools_then_reconstructs_pending_goal(tmp_path):
    case = case_at(0)
    case["question"] = [[{"role": "user", "content": "Create a file containing alpha-0; ask me its filename."}],
        [{"role": "user", "content": "Use memo.txt."}]]
    case["expected"] = [[], ["touch(file_name='memo.txt')", "echo(content='alpha-0', file_name='memo.txt')"]]
    root, profile, _control = setup(tmp_path, case)
    phases = [
        {"text": json.dumps(decision("clarify", reply="Which filename?", pending_fields=["file_name"]))},
        {"text": json.dumps(decision("execute", next_tools=["touch", "echo"]))},
        {"calls": [("touch", {"file_name": "memo.txt"}),
            ("echo", {"content": "alpha-0", "file_name": "memo.txt"})]},
        {"text": json.dumps(decision("complete", reply="Created."))},
    ]
    calls = []

    def handle(request):
        index = len(calls)
        calls.append(json.loads(request.content))
        return response(request, index=index, **phases[index])

    row = await backend.run_one_current_multiturn(case["id"], root, _profile=profile,
        _test_transport_factory=lambda: httpx.MockTransport(handle))
    assert row["verification_result"] == "pass" and len(calls) == 4
    assert row["runs"][0]["model_request_purposes"] == {"task_control": 1}
    assert row["runs"][0]["answer"] == "Which filename?"
    assert row["runs"][1]["task_control"]["main_requests"] == 1
    assert row["scoring_verification"]["turns"][0]["valid"] is True


@pytest.mark.asyncio
async def test_twenty_step_limit_includes_controller_and_main_generations(tmp_path):
    root, profile, _control = setup(tmp_path)
    calls = []

    def handle(request):
        body = json.loads(request.content)
        index = len(calls)
        calls.append(body)
        if not body.get("tools"):
            return response(request, index=index,
                text=json.dumps(decision("execute", next_tools=["echo"])))
        return response(request, index=index,
            calls=[("echo", {"file_name": "memo.txt", "content": f"progress-{index}"})])

    row = await backend.run_one_current_multiturn("multi_turn_base_0", root, _profile=profile,
        _test_transport_factory=lambda: httpx.MockTransport(handle))
    assert len(calls) == 20 and row["runs"][0]["runtime_status"] == "budget_exhausted"
    assert sum(row["runs"][0]["model_request_purposes"].values()) == 20
    assert row["runs"][0]["runtime_budget"]["iterations"] == 20


@pytest.mark.asyncio
async def test_malformed_controller_output_preserves_failed_run_and_is_agent_failure(tmp_path):
    root, profile, _control = setup(tmp_path)
    requests = []

    def handle(request):
        requests.append(request)
        return response(request, text="This is not the required JSON object.")

    row = await backend.run_one_current_multiturn("multi_turn_base_0", root, _profile=profile,
        _test_transport_factory=lambda: httpx.MockTransport(handle))
    assert len(requests) == 1 and row["agent_control_failure"] is True
    run = row["runs"][0]
    assert run["runtime_status"] == "failed" and run["agent_control_failure"] is True
    assert run["runtime_diagnostic"]["error_type"] == "TaskControlError"
    assert row["scoring_verification"]["valid"] is False and row["forced_termination"] is True
    assert row["result"] == "unsupported"  # Mock results never become model scores.
    projected = {**row, "result": "fail"}
    assert runner._classification(projected) == "agent_control_failure"


@pytest.fixture(scope="module")
def selection():
    require_corpus()
    return runner._core._dataset.load_development_dataset()


@pytest.fixture
def plan(selection, monkeypatch):
    monkeypatch.setattr(runner._core._dataset, "load_development_dataset", lambda: copy.deepcopy(selection))
    return runner.preflight(20)


@pytest.mark.bfcl_evidence
def test_native_forty_matrix_pins_A3_and_same_twenty_way_settings(plan, tmp_path):
    assert plan["agent_policy"] == POLICY and plan["configuration_label"] == "A3"
    assert plan["planned_entries"] == 40 and plan["defined_user_turns"] == 156
    config = runner.build_config(plan, tmp_path)
    assert len(config["tests"]) == 40
    assert config["evaluateOptions"]["maxConcurrency"] == config["providers"][0]["config"]["workers"] == 20
    assert all(test["metadata"]["configuration_label"] == "A3" for test in config["tests"])
    assert all(set(test["vars"]) == {"task_id"} for test in config["tests"])


@pytest.mark.bfcl_evidence
def test_collector_callback_counts_known_control_failure_zero_without_changing_A0_default(plan, tmp_path, monkeypatch):
    root, plan, payload, output, rows = compact_collected.__wrapped__(plan, tmp_path, monkeypatch)
    row = next(row for row in rows if row["result"] == "fail")
    row["agent_control_failure"] = True
    row["runs"][0] = {"runtime_status": "failed", "agent_control_failure": True,
        "runtime_diagnostic": {"error_type": "TaskControlError"}}
    item = next(item for item in payload["results"]["results"] if item["vars"]["task_id"] == row["id"])
    item["response"] = native_provider.render_response(row)
    (root / "results/results.json").write_text(json.dumps({"results": rows,
        "dataset_binding": "fixture-binding", "model_api_calls": 40, "max_api_calls": None}), encoding="utf-8")
    legacy = runner._core.collect_campaign(root, plan, payload, output)
    assert legacy["main_success_rate"] is None and legacy["scorable_tasks"] == 39
    assert legacy["failure_classifications"]["infrastructure_error"] == 1
    projected = runner._core.collect_campaign(root, plan, payload, output,
        classification=runner._classification, runtime_infrastructure=runner._runtime_infrastructure)
    assert projected["evidence_errors"] == [] and projected["scorable_tasks"] == 40
    assert projected["main_success_rate"] == 0.5
    task = next(task for task in projected["tasks"] if task["task_id"] == row["id"])
    assert task["classification"] == "agent_control_failure" and task["main_score"] == 0.0
    assert projected["runtime_infrastructure_ids"] == []
    assert row["runs"][0]["runtime_status"] == "failed"  # Original execution truth remains intact.


@pytest.mark.bfcl_evidence
def test_actual_paid_A3_admission_has_zero_http_and_rejects_a_different_saved_policy(plan, selection, tmp_path, monkeypatch):
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
    assert control["agent_policy"] == POLICY and control["test_only"] is False
    assert len(control["case_ids"]) == 40 and control["concurrent_tasks"]["maximum"] == 20
    assert json.loads((root / "results/http-budget.json").read_bytes())["calls"] == []
