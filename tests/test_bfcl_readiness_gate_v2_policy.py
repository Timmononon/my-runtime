"""Readiness-v2 protocol and campaign tests; all model traffic is mocked."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import httpx
import pytest
from bfcl_test_prerequisites import require_corpus
from test_bfcl_current import source_at
from test_bfcl_multiturn_concurrent import case_at, phases
from test_bfcl_promptfoo_compact_development import collected as compact_collected
from test_bfcl_promptfoo_compact_development import native_provider
from test_bfcl_v4_live import response

from aster.engine.capability_gate import POLICY as V1_POLICY
from aster.engine.capability_gate import policy_identity as v1_identity
from aster.engine.readiness_gate_v2 import CHECK_SYSTEM, POLICY, policy_identity
from aster.engine.readiness_prompt import POLICY as NATIVE_POLICY
from aster.engine.readiness_prompt import SYSTEM_PREAMBLE
from aster.evaluation import bfcl_multiturn_concurrent as backend
from aster.evaluation.bfcl_current import capture_current_profile

PROJECT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("clarify_gate_launcher_test", PROJECT /
    "evals/bfcl_multiturn/promptfoo/run_compact_development_clarify_gate.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def setup(tmp_path, case=None, policy=POLICY):
    tmp_path.mkdir(parents=True, exist_ok=True)
    profile = capture_current_profile(source_at(tmp_path), model="deepseek-flash", reasoning_effort="none")
    root = tmp_path / "campaign"
    control = backend.initialize_campaign([case or case_at(0)], root, profile=profile,
        maximum_concurrency=20, _test_only=True, agent_policy=policy)
    return root, profile, control


def ready():
    return {"text": json.dumps({"status": "ready", "reply": ""})}


def missing_case(index=0):
    case = case_at(index)
    case.update(id=f"multi_turn_miss_func_{index}", category="multi_turn_miss_func",
        missed_function={"1": ["touch", "echo"]})
    create, read = copy.deepcopy(case["question"])
    calls = copy.deepcopy(case["expected"])
    case["question"] = [create, [{"role": "user", "content": "Tools have been updated."}], read]
    case["expected"] = [[], calls[0], calls[1]]
    return case


def test_explicit_gate_identity_keeps_prior_policy_shapes_and_budgets(tmp_path):
    controls = [setup(tmp_path / str(i), policy=policy)[2] for i, policy in enumerate(
        (None, "readiness_state_v1", NATIVE_POLICY, V1_POLICY, POLICY))]
    assert "agent_policy" not in controls[0] and "agent_policy_identity" not in controls[0]
    assert controls[1]["agent_policy"] == "readiness_state_v1" and "agent_policy_identity" not in controls[1]
    assert controls[2]["agent_policy_identity"]["policy"] == NATIVE_POLICY
    assert controls[3]["agent_policy_identity"] == v1_identity()
    assert controls[4]["agent_policy_identity"] == policy_identity()
    assert all(x["inherited_runtime_safety"] == controls[0]["inherited_runtime_safety"] for x in controls)
    assert len({x["dataset_binding"] for x in controls}) == 5


@pytest.mark.asyncio
async def test_one_gate_then_native_calls_share_pinned_protocol_and_receipts(tmp_path):
    case = case_at(0)
    case["provenance"] = {"hidden": "PRIVATE_ANSWER_MUST_NOT_REACH_MODEL"}
    root, profile, _control = setup(tmp_path, case)
    native = phases(0)
    scripted, bodies = [ready(), *native[:2], ready(), *native[2:]], []

    def handle(request):
        body = json.loads(request.content)
        assert "PRIVATE_ANSWER_MUST_NOT_REACH_MODEL" not in request.content.decode()
        assert body["model"] == "deepseek-flash" and body["reasoning"] == {"effort": "none"}
        assert body["max_output_tokens"] == 8192
        index = len(bodies)
        bodies.append(body)
        gate = index in (0, 3)
        assert bool(body.get("tools")) is not gate
        assert body["instructions"].startswith(SYSTEM_PREAMBLE)
        assert body["instructions"].endswith("\n\n" + CHECK_SYSTEM) is gate
        return response(request, index=index, **scripted[index])

    row = await backend.run_one_current_multiturn(case["id"], root, _profile=profile,
        _test_transport_factory=lambda: httpx.MockTransport(handle))
    assert row["verification_result"] == "pass" and row["result"] == "unsupported"
    assert row["model_api_calls"] == 0 and row["http_attempts"] == len(bodies) == 6
    assert row["agent_policy_identity"] == policy_identity()
    for run in row["runs"]:
        assert run["runtime_capability_gate"] == POLICY and run["runtime_prompt_policy"] == NATIVE_POLICY
        assert run["runtime_task_control"] == "off" and run["task_control"] is None
        assert run["model_request_purposes"] == {"capability_check": 1, "main": 2}
        assert run["runtime_budget"]["model_requests"] == run["runtime_budget"]["iterations"] == 3
        assert run["capability_gate"]["check_requests"] == 1
        assert run["capability_gate"]["checks"][0]["status"] == "ready"
    for observation in row["step_observations"]:
        assert observation["system_sha256"] == hashlib.sha256(observation["system"].encode()).hexdigest()
    assert backend.read_task_receipt(root, case["id"]) == row


@pytest.mark.asyncio
async def test_missing_capability_abstains_then_latest_tools_resume_original_goal(tmp_path):
    case = missing_case()
    root, profile, _control = setup(tmp_path, case)
    native = phases(0)
    scripted = [{"text": json.dumps({"status": "missing_capability", "reply": "File creation is unavailable."})},
        ready(), *native[:2], ready(), *native[2:]]
    bodies = []

    def handle(request):
        body = json.loads(request.content)
        index = len(bodies)
        bodies.append(body)
        return response(request, index=index, **scripted[index])

    row = await backend.run_one_current_multiturn(case["id"], root, _profile=profile,
        _test_transport_factory=lambda: httpx.MockTransport(handle))
    assert row["verification_result"] == "pass" and row["http_attempts"] == 7
    assert row["runs"][0]["model_request_purposes"] == {"capability_check": 1}
    assert row["runs"][0]["capability_gate"]["checks"][0]["status"] == "missing_capability"
    assert row["scoring_verification"]["turns"][0]["valid"] is True
    assert [item["request_purpose"] for item in row["step_observations"]] == [
        "capability_check", "capability_check", "main", "main", "capability_check", "main", "main"]
    observed = row["step_observations"]
    first = json.loads(observed[0]["messages"][0]["content"])
    restored = json.loads(observed[1]["messages"][0]["content"])
    assert {tool["name"] for tool in first["tools"]}.isdisjoint({"touch", "echo"})
    assert {"touch", "echo"} <= {tool["name"] for tool in restored["tools"]}
    assert "Create memo.txt" in json.dumps(restored["conversation"])
    assert first["tools_sha256"] != restored["tools_sha256"]


@pytest.mark.asyncio
async def test_bad_gate_format_falls_back_to_native_without_fake_abstention(tmp_path):
    root, profile, _control = setup(tmp_path)
    native = phases(0)
    scripted = [{"text": "Not the gate object."}, *native[:2], ready(), *native[2:]]
    calls = []

    def handle(request):
        index = len(calls)
        calls.append(request)
        return response(request, index=index, **scripted[index])

    row = await backend.run_one_current_multiturn("multi_turn_base_0", root, _profile=profile,
        _test_transport_factory=lambda: httpx.MockTransport(handle))
    assert row["verification_result"] == "pass" and len(calls) == 6
    assert row["runs"][0]["capability_gate"]["checks"][0]["status"] == "fallback_to_native"
    assert row["runs"][0]["model_request_purposes"] == {"capability_check": 1, "main": 2}
    assert row["agent_control_failure"] is False
    assert not any(x.get("runtime_diagnostic") for x in row["runs"])


@pytest.mark.asyncio
async def test_gate_spends_one_of_same_twenty_model_steps(tmp_path):
    root, profile, _control = setup(tmp_path)
    bodies = []

    def handle(request):
        index = len(bodies)
        bodies.append(json.loads(request.content))
        phase = ready() if index == 0 else {"calls": [("echo", {"file_name": "memo.txt", "content": f"step-{index}"})]}
        return response(request, index=index, **phase)

    row = await backend.run_one_current_multiturn("multi_turn_base_0", root, _profile=profile,
        _test_transport_factory=lambda: httpx.MockTransport(handle))
    assert len(bodies) == 20 and row["runs"][0]["runtime_status"] == "budget_exhausted"
    assert row["runs"][0]["model_request_purposes"] == {"capability_check": 1, "main": 19}
    assert row["termination_kind"] == "official_per_turn_step_limit"
    assert runner._core._classification({**row, "result": "fail"}) == "budget_termination"


@pytest.mark.asyncio
async def test_changed_gate_prompt_binding_fails_before_http(tmp_path):
    root, profile, _control = setup(tmp_path)
    path = root / "execution-control.json"
    control = json.loads(path.read_bytes())
    control["agent_policy_identity"]["gate_prompt_sha256"] = "0" * 64
    path.write_text(json.dumps(control), encoding="utf-8")

    def forbidden():
        raise AssertionError("Tampered gate cannot construct transport")

    with pytest.raises(ValueError, match="prompt policy identity"):
        await backend.run_one_current_multiturn("multi_turn_base_0", root, _profile=profile,
            _test_transport_factory=forbidden)
    assert json.loads((root / "results/http-budget.json").read_bytes())["calls"] == []


@pytest.fixture(scope="module")
def selection():
    require_corpus()
    return runner._core._dataset.load_development_dataset()


@pytest.fixture
def plan(selection, monkeypatch):
    monkeypatch.setattr(runner._core._dataset, "load_development_dataset", lambda: copy.deepcopy(selection))
    return runner.preflight(20)


@pytest.mark.bfcl_evidence
def test_exact_forty_task_plan_has_typed_gate_identity_without_user_runtime_reads(plan, selection, tmp_path, monkeypatch):
    import aster.paths

    def forbidden():
        raise AssertionError("Preflight cannot open user Runtime")

    monkeypatch.setattr(aster.paths, "resolve_data_dir", forbidden)
    assert plan["defined_user_turns"] == 156 and plan["agent_policy_identity"] == policy_identity()
    assert plan["configuration_label"] == "Readiness-Gate-v2" and plan["runtime_capability_gate"] == POLICY
    config = runner.build_config(plan, tmp_path)
    assert len(config["tests"]) == 40 and all(set(x["vars"]) == {"task_id"} for x in config["tests"])
    assert [x["vars"]["task_id"] for x in config["tests"]] == [x["id"] for x in selection[0]]
    assert config["evaluateOptions"]["maxConcurrency"] == config["providers"][0]["config"]["workers"] == 20
    output = tmp_path / "preflight"
    assert runner.main(["--campaign-dir", str(output)]) == 0
    assert {x.name for x in output.iterdir()} == {"campaign-plan.json"}


@pytest.mark.bfcl_evidence
def test_paid_admission_freezes_gate_and_native_sources_but_does_not_dispatch(plan, selection, tmp_path, monkeypatch):
    import aster.paths

    monkeypatch.setattr(aster.paths, "resolve_data_dir", lambda: source_at(tmp_path))
    monkeypatch.setattr(runner, "load_development_cases", lambda: copy.deepcopy(selection[0]))

    class Stop(RuntimeError):
        pass

    def stop(command, **kwargs):
        assert command[-2:] == ["--max-concurrency", "20"]
        assert kwargs["env"]["PROMPTFOO_PYTHON_WORKERS"] == "20"
        raise Stop()

    monkeypatch.setattr(runner.subprocess, "run", stop)
    root = tmp_path / "admitted"
    with pytest.raises(Stop):
        runner.main(["--campaign-dir", str(root), "--execute"])
    control = json.loads((root / "execution-control.json").read_bytes())
    assert control["test_only"] is False and control["agent_policy_identity"] == policy_identity()
    assert control["case_ids"] == [x["id"] for x in selection[0]]
    assert json.loads((root / "results/http-budget.json").read_bytes())["calls"] == []
    files = {x["file"] for x in json.loads((root / "source-manifest.json").read_bytes())["files"]}
    assert {"src/aster/engine/readiness_gate_v2.py", "src/aster/engine/readiness_prompt.py",
        "evals/bfcl_multiturn/promptfoo/run_compact_development_clarify_gate.py"} <= files


def _save_rows(root, rows):
    (root / "results/results.json").write_text(json.dumps({"results": rows, "dataset_binding": "fixture-binding",
        "model_api_calls": sum(row["model_api_calls"] for row in rows), "max_api_calls": None}), encoding="utf-8")


@pytest.fixture
def collected(plan, tmp_path, monkeypatch):
    root, plan, payload, output, rows = compact_collected.__wrapped__(plan, tmp_path, monkeypatch)
    control = json.loads((root / "execution-control.json").read_bytes())
    control.update(agent_policy=POLICY, agent_policy_identity=policy_identity())
    (root / "execution-control.json").write_text(json.dumps(control), encoding="utf-8")
    ledger = json.loads((root / "results/http-budget.json").read_bytes())
    original = {call["case_id"]: call for call in ledger["calls"]}
    ledger["calls"] = []
    gate_payload = {"policy": POLICY, "conversation": [{"role": "user", "content": "Synthetic task"}],
        "tools": [], "tools_sha256": runner._sha(runner._canonical([]))}
    content = runner._canonical(gate_payload)
    for row in rows:
        row.update(agent_policy=POLICY, agent_policy_identity=policy_identity())
        turns = len(row["runs"])
        row["runs"], row["step_observations"] = [], []
        for index in range(turns):
            rid = f"fixture-{row['id']}-{index}"
            trace = {"index": 0, "status": "ready", "parse_status": "ok", "input_sha256": runner._sha(content),
                "tools_sha256": gate_payload["tools_sha256"], "request_number": 1}
            row["runs"].append({"turn_index": index, "run_id": rid, "runtime_status": "completed",
                "runtime_budget": {"model_requests": 2}, "model_request_purposes": {"capability_check": 1, "main": 1},
                "model_request_receipts": [{"purpose": "capability_check", "request_number": 1},
                    {"purpose": "main", "request_number": 2}],
                "runtime_prompt_policy": NATIVE_POLICY, "runtime_task_control": "off", "runtime_capability_gate": POLICY,
                "agent_policy_identity": policy_identity(), "task_control": None,
                "capability_gate": {"policy": POLICY, "policy_identity": policy_identity(), "session_id": row["session_id"],
                    "run_id": rid, "check_requests": 1, "main_requests": 1, "checks": [trace], "current": trace}})
            for purpose, number, system, messages in [
                    ("capability_check", 1, SYSTEM_PREAMBLE + "\n\n" + CHECK_SYSTEM, [{"role": "user", "content": content}]),
                    ("main", 2, SYSTEM_PREAMBLE, [])]:
                row["step_observations"].append({"turn_index": index, "request_purpose": purpose, "request_number": number,
                    "system": system, "system_sha256": runner._sha(system), "offered_tools": [], "messages": messages})
        total = turns * 2
        row["model_api_calls"] = row["http_attempts"] = total
        row["usage"] *= total
        ledger["calls"].extend(copy.deepcopy(original[row["id"]]) for _ in range(total))
        key = runner._helpers.digest(row["id"].encode())[:24]
        for suffix in (".started.json", ".finished.json"):
            path = output / "dispatches" / (key + suffix)
            value = json.loads(path.read_bytes())
            value["http_attempts_for_task"] = total
            path.write_text(json.dumps(value), encoding="utf-8")
        item = next(x for x in payload["results"]["results"] if x["vars"]["task_id"] == row["id"])
        item["response"] = native_provider.render_response(row)
    (root / "results/http-budget.json").write_text(json.dumps(ledger), encoding="utf-8")
    _save_rows(root, rows)
    return root, plan, payload, output, rows


@pytest.mark.bfcl_evidence
def test_collector_keeps_fixed_denominator_and_counts_helper_cost(collected):
    root, plan, payload, output, _rows = collected
    result = runner.collect_campaign(root, plan, payload, output)
    assert result["evidence_errors"] == [] and result["main_success_rate"] == .5
    assert result["model_api_calls"] == result["logical_model_request_reservations"] == 312
    assert result["capability_check_model_requests"] == result["action_model_requests"] == 156
    assert result["gate_status_counts"] == {"ready": 156}
    assert result["scorable_tasks"] == 40 and result["holdout_inference_tasks"] == 0


@pytest.mark.parametrize("tamper", ["hidden_tools", "input_hash", "native_prompt", "purpose", "false_abstention", "false_clarification", "parse_state"])
@pytest.mark.bfcl_evidence
def test_collector_rejects_claims_without_matching_gate_execution(collected, tamper):
    root, plan, payload, output, rows = collected
    row, run = rows[0], rows[0]["runs"][0]
    if tamper == "hidden_tools":
        row["step_observations"][0]["offered_tools"] = ["touch"]
    elif tamper == "input_hash":
        run["capability_gate"]["checks"][0]["input_sha256"] = "0" * 64
    elif tamper == "native_prompt":
        row["step_observations"][1]["system"] = "Other prompt"
    elif tamper == "purpose":
        run["model_request_receipts"][0]["purpose"] = "main"
    elif tamper == "parse_state":
        run["capability_gate"]["checks"][0]["parse_status"] = "invalid_json"
    else:
        run["capability_gate"]["checks"][0]["status"] = (
            "needs_clarification" if tamper == "false_clarification" else "missing_capability")
    _save_rows(root, rows)
    result = runner.collect_campaign(root, plan, payload, output)
    assert result["evidence_errors"] and result["main_success_rate"] is None
    assert result["baseline_assessment"]["capability_baseline_ready"] is False


@pytest.mark.bfcl_evidence
def test_started_gate_campaign_cannot_resume_or_overwrite(tmp_path, plan):
    root = tmp_path / "started"
    root.mkdir()
    marker = root / "execution-started.json"
    marker.write_text("original")
    with pytest.raises(SystemExit):
        runner.main(["--campaign-dir", str(root), "--execute"])
    assert marker.read_text() == "original" and not (root / "results").exists()


def clarification_case(index=0):
    case = case_at(index)
    _create, read = copy.deepcopy(case["question"])
    calls = copy.deepcopy(case["expected"])
    case.update(id=f"multi_turn_miss_param_{index}", category="multi_turn_miss_param")
    case["question"] = [[{"role": "user", "content": f"Create a file containing alpha-{index}; ask me the filename."}],
        [{"role": "user", "content": "Use memo.txt."}], read]
    case["expected"] = [[], calls[0], calls[1]]
    return case


@pytest.mark.asyncio
async def test_needs_clarification_zero_calls_then_supplied_choice_resumes(tmp_path):
    case = clarification_case()
    case["provenance"] = {"hidden": "PRIVATE_CLARIFICATION_ANSWER_ONLY"}
    root, profile, _control = setup(tmp_path, case)
    native = phases(0)
    scripted = [{"text": json.dumps({"status": "needs_clarification", "reply": "Which filename should I use?"})},
        ready(), *native[:2], ready(), *native[2:]]
    bodies = []

    def handle(request):
        assert "PRIVATE_CLARIFICATION_ANSWER_ONLY" not in request.content.decode()
        body = json.loads(request.content)
        index = len(bodies)
        bodies.append(body)
        return response(request, index=index, **scripted[index])

    row = await backend.run_one_current_multiturn(case["id"], root, _profile=profile,
        _test_transport_factory=lambda: httpx.MockTransport(handle))
    assert row["verification_result"] == "pass" and row["http_attempts"] == 7
    assert row["result"] == "unsupported" and row["model_api_calls"] == 0
    first = row["runs"][0]
    assert first["runtime_status"] == "completed"
    assert first["answer"] == "Which filename should I use?"
    assert first["model_request_purposes"] == {"capability_check": 1}
    assert first["runtime_budget"]["model_requests"] == first["runtime_budget"]["iterations"] == 1
    assert first["capability_gate"]["checks"][0]["status"] == "needs_clarification"
    assert first["capability_gate"]["checks"][0]["parse_status"] == "ok"
    assert row["scoring_verification"]["turns"][0]["valid"] is True
    assert all(check["valid"] for check in row["scoring_verification"]["turns"])
    observations = row["step_observations"]
    initial = json.loads(observations[0]["messages"][0]["content"])
    resumed = json.loads(observations[1]["messages"][0]["content"])
    assert {"touch", "echo"} <= {tool["name"] for tool in initial["tools"]}
    assert initial["tools_sha256"] == resumed["tools_sha256"]
    assert "ask me the filename" in json.dumps(resumed["conversation"])
    assert "Use memo.txt." in json.dumps(resumed["conversation"])
    assert [item["request_purpose"] for item in observations] == [
        "capability_check", "capability_check", "main", "main", "capability_check", "main", "main"]
    assert not any(item.get("run_id") == first["run_id"] for item in row["tool_ledger"])
