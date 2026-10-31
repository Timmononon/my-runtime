"""Offline comparison integrity and actual isolated deterministic DeepEval tests."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from bfcl_test_prerequisites import require_campaign

from aster.evaluation.bfcl_multiturn_export import _digest, validate_record

PROJECT = Path(__file__).resolve().parents[1]
CAMPAIGN = PROJECT / "artifacts/bfcl/compact-dev40-c20-20261030"
HERE = PROJECT / "evals/bfcl_multiturn/deepeval"


def _module(name):
    spec = importlib.util.spec_from_file_location("comparison_test_" + name, HERE / (name + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


data = _module("comparison_data")
_profile_identity = data._profile_identity
auxiliary_turn_counts = data.auxiliary_turn_counts
compare_campaigns = data.compare_campaigns
diagnostic_warnings = data.diagnostic_warnings
load_campaign = data.load_campaign
normalize_runtime_outcomes = data.normalize_runtime_outcomes
summarize_campaign = data.summarize_campaign
child_environment = _module("run_comparison").child_environment


@pytest.fixture(scope="module")
def baseline():
    require_campaign("compact-dev40-c20-20261030")
    return load_campaign(CAMPAIGN, label="A0")


def candidate_fixture(baseline):
    result = deepcopy(baseline)
    result.update(label="A3", root="synthetic-candidate-for-unit-test-only")
    result["identity"]["agent_policy"] = "readiness_state_v1"
    for record in result["records"]:
        record["config_label"] = "A3"
        record["config_identity"]["snapshot"]["agent_policy"] = "readiness_state_v1"
        record["config_identity"]["sha256"] = _digest(record["config_identity"]["snapshot"])
        record["record_sha256"] = _digest({key: value for key, value in record.items() if key != "record_sha256"})
    return result


def prompt_candidate_fixture(baseline):
    result = candidate_fixture(baseline)
    result.update(label="A4.1", root="synthetic-prompt-candidate-for-unit-test-only")
    identity = {"policy": data.PROMPT_POLICY, "prompt_sha256": "a" * 64}
    result["identity"].update(agent_policy=data.PROMPT_POLICY, agent_policy_identity=identity)
    for record in result["records"]:
        record["config_label"] = "A4.1"
        snapshot = record["config_identity"]["snapshot"]
        snapshot.update(agent_policy=data.PROMPT_POLICY, agent_policy_identity=deepcopy(identity))
        record["config_identity"]["sha256"] = _digest(snapshot)
        record["record_sha256"] = _digest({key: value for key, value in record.items() if key != "record_sha256"})
    return result


def gate_candidate_fixture(baseline, *, policy=data.GATE_POLICY):
    result = prompt_candidate_fixture(baseline)
    identity = {"policy": policy, "gate_prompt_sha256": "b" * 64,
        "native_prompt_policy": data.PROMPT_POLICY, "native_prompt_sha256": "a" * 64,
        "check_frequency": "once_per_user_turn", "parse_failure_policy": "fallback_to_native"}
    result.update(label="MF-Gate-v1" if policy == data.GATE_POLICY else "Readiness-Gate-v2",
                  root="synthetic-" + policy + "-candidate-for-unit-test-only")
    result["identity"].update(agent_policy=policy, agent_policy_identity=identity)
    for record in result["records"]:
        record["config_label"] = result["label"]
        snapshot = record["config_identity"]["snapshot"]
        snapshot.update(agent_policy=policy, agent_policy_identity=deepcopy(identity))
        record["config_identity"]["sha256"] = _digest(snapshot)
        record["record_sha256"] = _digest({key: value for key, value in record.items() if key != "record_sha256"})
    return result


def write_prompt_source(root, *, code=None):
    payload = (code or "POLICY = 'readiness_prompt_v1'\nSYSTEM_PREAMBLE = ('First. ' 'Second.')\n").encode()
    source = root / "as-run-source" / data.PROMPT_SOURCE
    source.parent.mkdir(parents=True)
    source.write_bytes(payload)
    (root / "source-manifest.json").write_text(json.dumps({"files": [{"file": data.PROMPT_SOURCE,
        "bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}]}), encoding="utf-8")
    return source


def prompt_saved_objects(root):
    write_prompt_source(root)
    identity = data.saved_prompt_identity(root)
    envelope = {"agent_policy": data.PROMPT_POLICY, "agent_policy_identity": identity}
    row = {**deepcopy(envelope), "runs": [{"runtime_prompt_policy": data.PROMPT_POLICY,
        "runtime_task_control": "off", "agent_policy_identity": deepcopy(identity), "model_request_purposes": {"main": 1},
        "model_request_receipts": [{"purpose": "main", "request_number": 1}],
        "runtime_budget": {"model_requests": 1}}]}
    return [deepcopy(envelope) for _ in range(3)], [row]


def gate_saved_objects(root, *, status="ready", policy=data.GATE_POLICY):
    write_prompt_source(root)
    code = (f"POLICY = '{policy}'\nCHECK_SYSTEM = ('Check only capabilities. ' 'No tools.')\n").encode()
    relative = data.GATE_SOURCES[policy]
    source = root / "as-run-source" / relative
    source.write_bytes(code)
    manifest = json.loads((root / "source-manifest.json").read_bytes())
    manifest["files"].append({"file": relative, "bytes": len(code), "sha256": hashlib.sha256(code).hexdigest()})
    (root / "source-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    identity, check_system, native_prefix = data.saved_gate_contract(root, policy)
    envelope = {"agent_policy": policy, "agent_policy_identity": identity}
    tools = [{"name": "create_report", "description": "Create a report", "parameters": {"type": "object"}}]
    public_user = {"role": "user", "content": "Create a report", "tool_calls": [], "tool_call_id": None}
    payload = {"policy": policy, "conversation": [public_user],
        "tools": tools, "tools_sha256": data._gate_digest(tools)}
    check = {"index": 0, "status": status, "parse_status": "invalid_json" if status == "fallback_to_native" else "ok", "input_sha256": data._gate_digest(payload),
        "tools_sha256": payload["tools_sha256"], "request_number": 1, "response_sha256": "c" * 64}
    check["user_turn_sha256"] = data._gate_digest({"user": public_user, "mailbox_id": None})
    gate_system = native_prefix + "Operator instructions\n\n" + check_system
    observations = [{"turn_index": 0, "system": gate_system,
        "system_sha256": hashlib.sha256(gate_system.encode()).hexdigest(), "request_purpose": "capability_check",
        "request_number": 1, "messages": [{"role": "user", "content": json.dumps(payload), "tool_calls": []}], "offered_tools": []}]
    receipts, purposes = [{"purpose": "capability_check", "request_number": 1}], {"capability_check": 1}
    if status not in {"missing_capability", "needs_clarification"}:
        system = native_prefix + "Operator instructions"
        observations.append({"turn_index": 0, "system": system, "system_sha256": hashlib.sha256(system.encode()).hexdigest(),
            "request_purpose": "main", "request_number": 2, "messages": payload["conversation"], "offered_tools": [tool["name"] for tool in tools]})
        receipts.append({"purpose": "main", "request_number": 2})
        purposes["main"] = 1
    trace = {"policy": policy, "policy_identity": identity, "run_id": "run-a", "session_id": "session-a",
        "checks": [check], "check_requests": 1, "main_requests": purposes.get("main", 0)}
    row = {**deepcopy(envelope), "session_id": "session-a", "step_observations": observations, "tool_ledger": [],
        "runs": [{"run_id": "run-a", "turn_index": 0, "runtime_status": "completed",
            "runtime_prompt_policy": data.PROMPT_POLICY, "runtime_task_control": "off",
            "runtime_capability_gate": policy, "agent_policy_identity": deepcopy(identity),
            "model_request_purposes": purposes, "model_request_receipts": receipts,
            "runtime_budget": {"model_requests": len(receipts), "iterations": len(receipts)}, "capability_gate": trace}]}
    return [deepcopy(envelope) for _ in range(3)], [row]


@pytest.mark.parametrize("status", ["ready", "missing_capability", "fallback_to_native"])
def test_gate_saved_contract_uses_actual_input_and_native_fallback(tmp_path, status):
    envelopes, rows = gate_saved_objects(tmp_path, status=status)
    assert data.validate_gate_campaign(tmp_path, *envelopes, rows) == envelopes[0]["agent_policy_identity"]
    trace = data._gate_projection(rows[0]["runs"][0])
    assert trace["checks"][0]["status"] == status
    assert "conversation" not in trace and "reply" not in trace


@pytest.mark.parametrize("status", ["ready", "missing_capability", "needs_clarification", "fallback_to_native"])
def test_readiness_v2_contract_preserves_two_terminal_states_and_actual_inputs(tmp_path, status):
    envelopes, rows = gate_saved_objects(tmp_path, status=status, policy=data.CLARIFY_POLICY)
    assert data.validate_gate_campaign(tmp_path, *envelopes, rows) == envelopes[0]["agent_policy_identity"]
    run = rows[0]["runs"][0]
    assert run["model_request_purposes"].get("main", 0) == (0 if status in {
        "missing_capability", "needs_clarification"} else 1)
    if status == "needs_clarification":
        rows[0]["tool_ledger"] = [{"run_id": run["run_id"]}]
        with pytest.raises(ValueError, match="cannot execute business tools"):
            data.validate_gate_campaign(tmp_path, *envelopes, rows)


def test_v1_cannot_be_relabelled_as_a_valid_clarification_state(tmp_path):
    envelopes, rows = gate_saved_objects(tmp_path, status="needs_clarification")
    with pytest.raises(ValueError, match="Unknown saved"):
        data.validate_gate_campaign(tmp_path, *envelopes, rows)


def test_v2_uses_its_own_frozen_prompt_and_does_not_import_it(tmp_path):
    envelopes, rows = gate_saved_objects(tmp_path, policy=data.CLARIFY_POLICY)
    identity, _gate, _native = data.saved_gate_contract(tmp_path, data.CLARIFY_POLICY)
    assert identity["policy"] == data.CLARIFY_POLICY
    with pytest.raises(ValueError, match="exactly once"):
        data.saved_gate_contract(tmp_path)  # No v1 source is an authority for v2.
    source = tmp_path / "as-run-source" / data.CLARIFY_SOURCE
    source.write_bytes(source.read_bytes() + b"\nraise RuntimeError('never execute snapshot')\n")
    with pytest.raises(ValueError, match="differs from its manifest"):
        data.validate_gate_campaign(tmp_path, *envelopes, rows)


@pytest.mark.parametrize("tamper", ["source", "policy", "scope", "input", "tools", "system", "purpose", "stale_user", "parse_status",
                                    "duplicate_check", "hidden_main", "fallback_without_native", "budget"])
def test_gate_saved_contract_refuses_tampered_or_mislabeled_evidence(tmp_path, tamper):
    status = "missing_capability" if tamper == "hidden_main" else "fallback_to_native" if tamper == "fallback_without_native" else "ready"
    envelopes, rows = gate_saved_objects(tmp_path, status=status)
    row, run = rows[0], rows[0]["runs"][0]
    if tamper == "source":
        (tmp_path / "as-run-source" / data.GATE_SOURCE).write_text("POLICY='changed'", encoding="utf-8")
    elif tamper == "policy":
        envelopes[0]["agent_policy_identity"]["parse_failure_policy"] = "silently_abstain"
    elif tamper == "scope":
        run["capability_gate"]["run_id"] = "a-different-run"
    elif tamper in {"input", "tools"}:
        check = run["capability_gate"]["checks"][0]
        check["input_sha256" if tamper == "input" else "tools_sha256"] = "d" * 64
    elif tamper == "system":
        item = row["step_observations"][0]
        item["system"] = item["system"].replace("Check only capabilities", "A different gate")
        item["system_sha256"] = hashlib.sha256(item["system"].encode()).hexdigest()
    elif tamper == "purpose":
        row["step_observations"][0]["request_purpose"] = "main"
    elif tamper == "stale_user":
        run["capability_gate"]["checks"][0]["user_turn_sha256"] = "e" * 64
    elif tamper == "parse_status":
        run["capability_gate"]["checks"][0]["parse_status"] = "invalid_json"
    elif tamper == "duplicate_check":
        run["capability_gate"]["checks"] *= 2
    elif tamper == "hidden_main":
        row["tool_ledger"].append({"run_id": run["run_id"]})
    elif tamper == "fallback_without_native":
        run["model_request_receipts"].pop()
        run["model_request_purposes"].pop("main")
        run["runtime_budget"] = {"model_requests": 1, "iterations": 1}
        run["capability_gate"]["main_requests"] = 0
        row["step_observations"].pop()
    else:
        run["runtime_budget"]["model_requests"] = 21
    with pytest.raises(ValueError):
        data.validate_gate_campaign(tmp_path, *envelopes, rows)


@pytest.mark.bfcl_evidence
def test_gate_candidate_pairs_with_a0_and_a41_and_preserves_other_category_regressions(baseline):
    candidate = gate_candidate_fixture(baseline)
    for old in (baseline, prompt_candidate_fixture(baseline)):
        paired = compare_campaigns(old, candidate)
        assert paired["paired_scorable_tasks"] == 40
        assert paired["agent_policies"]["candidate"] == data.GATE_POLICY
        assert paired["main_success_rate_delta"] == 0
        assert sum(map(len, paired["missing_function_transitions"].values())) == 10
        assert sum(map(len, paired["other_category_transitions"].values())) == 30
    candidate["records"][0]["config_identity"]["snapshot"]["agent_policy_identity"]["gate_prompt_sha256"] = "c" * 64
    record = candidate["records"][0]
    record["config_identity"]["sha256"] = _digest(record["config_identity"]["snapshot"])
    record["record_sha256"] = _digest({key: value for key, value in record.items() if key != "record_sha256"})
    with pytest.raises(ValueError, match="policy identities differ"):
        compare_campaigns(baseline, candidate)


@pytest.mark.bfcl_evidence
def test_readiness_v2_pairs_with_a0_and_preserved_capability_gate(baseline):
    candidate = gate_candidate_fixture(baseline, policy=data.CLARIFY_POLICY)
    for control in (baseline, gate_candidate_fixture(baseline)):
        paired = compare_campaigns(control, candidate)
        assert paired["paired_scorable_tasks"] == 40
        assert paired["agent_policies"]["candidate"] == data.CLARIFY_POLICY
        assert sum(map(len, paired["missing_function_transitions"].values())) == 10
        assert sum(map(len, paired["missing_parameter_transitions"].values())) == 10
        assert sum(map(len, paired["base_and_long_context_transitions"].values())) == 20


@pytest.mark.bfcl_evidence
def test_mp_breakdown_has_fixed_ten_tasks_and_preserves_baseline_scores(baseline):
    before = [item["record_sha256"] for item in baseline["records"]]
    result = data.capability_diagnostics(baseline)
    mp = result["missing_parameters"]
    assert mp["allocated_tasks"] == mp["scorable_tasks"] == mp["blocked_turns"] == 10
    assert mp["passed_tasks"] == mp["completed_zero_business_call_turns"] == 0
    assert mp["post_blocked_checks_eligible"] == 18
    assert mp["semantic_clarification_quality_judge"] is False
    assert result["base_and_long_context"]["allocated_tasks"] == 20
    assert before == [item["record_sha256"] for item in baseline["records"]]


@pytest.mark.bfcl_evidence
def test_clarification_diagnostics_are_not_mixed_with_capability_or_native_abstention(baseline):
    campaign = deepcopy(baseline)
    campaign["identity"]["agent_policy"] = data.CLARIFY_POLICY
    mp = next(record for record in campaign["records"] if record["subcategory"] == "multi_turn_miss_param")
    raw = next(row for row in campaign["rows"] if row["id"] == mp["task_id"])
    indices = campaign["reference_call_counts"][mp["task_id"]]
    blocked = indices.index(0)
    nonempty = next(index for index, count in enumerate(indices) if count > 0)
    for index in (blocked, nonempty):
        run = next(run for run in raw["runs"] if run["turn_index"] == index)
        run["capability_gate"] = {"checks": [{"status": "needs_clarification", "parse_status": "ok"}]}
    result = data.capability_diagnostics(campaign)
    clarification = result["capability_gate"]
    assert len(clarification["needs_clarification_reference_zero_turns"]) == 1
    assert len(clarification["needs_clarification_expected_execution_turns"]) == 1
    assert clarification["blocked_expected_execution_turns"][0]["gate_status"] == "needs_clarification"
    assert result["missing_parameters"]["gate_needs_clarification_blocked_turns"] == 1


@pytest.mark.bfcl_evidence
def test_mf_breakdown_matches_preserved_baseline_and_does_not_change_score(baseline):
    before = [record["record_sha256"] for record in baseline["records"]]
    summary = data.capability_diagnostics(baseline)
    mf = summary["missing_functions"]
    assert (mf["passed_tasks"], mf["completed_zero_business_call_turns"], mf["blocked_business_tool_calls"]) == (0, 0, 37)
    assert (mf["post_blocked_checks_passed"], mf["post_blocked_checks_eligible"]) == (21, 28)
    assert summary["other_categories"]["allocated_tasks"] == 30
    assert summary["capability_gate"]["enabled"] is False
    assert summary["affects_main_score"] is summary["counterfactual_passes_claimed"] is False
    assert before == [record["record_sha256"] for record in baseline["records"]]


@pytest.mark.bfcl_evidence
def test_preserved_a41_is_rechecked_from_its_own_sources_and_mf_boundary():
    require_campaign("compact-dev40-a41-c20-20261030")
    campaign = load_campaign(PROJECT / "artifacts/bfcl/compact-dev40-a41-c20-20261030", label="A4.1")
    assert campaign["summary"]["passed"] == 13
    mf = data.capability_diagnostics(campaign)["missing_functions"]
    assert mf["allocated_tasks"] == 10 and mf["passed_tasks"] == 0
    assert mf["completed_zero_business_call_turns"] == 0 and mf["blocked_business_tool_calls"] == 18
    assert (mf["post_blocked_checks_passed"], mf["post_blocked_checks_eligible"]) == (24, 28)
    assert mf["first_restored_checks_passed"] == 8 and mf["only_blocked_turns_failed_tasks"] == 7


@pytest.mark.bfcl_evidence
def test_preserved_mf_gate_and_native_metadata_are_cross_checked_by_primary_task_and_turn():
    require_campaign("compact-dev40-mf-gate-c20-20261030")
    campaign = load_campaign(PROJECT / "artifacts/bfcl/compact-dev40-mf-gate-c20-20261030", label="MF-Gate-v1")
    result = data.capability_diagnostics(campaign)
    assert campaign["summary"]["passed"] == 19
    assert result["missing_functions"]["passed_tasks"] == 7
    assert result["missing_parameters"]["passed_tasks"] == 2
    blocked = [item for item in result["capability_gate"]["checks"] if item["status"] == "missing_capability"]
    assert len(blocked) == 13
    assert sum(item["subcategory"] == "multi_turn_miss_func" for item in blocked) == 8
    assert sum(item["subcategory"] == "multi_turn_miss_param" for item in blocked) == 5
    assert all(item["status"] == "ready" for item in result["capability_gate"]["checks"]
               if item["task_id"] == "multi_turn_miss_func_107")
    cases = [{"metadata": {"aster_bfcl_record": deepcopy(record)}} for record in campaign["records"]]
    verified = data.verify_native_gate_projection(campaign, cases)
    assert verified["native_cases_verified"] == 40 and verified["checks_verified"] == 156
    cases[0]["metadata"]["aster_bfcl_record"]["capability_gate"]["turns"][0]["checks"][0]["status"] = "needs_clarification"
    with pytest.raises(ValueError, match="differs from primary"):
        data.verify_native_gate_projection(campaign, cases)


def test_prompt_identity_is_derived_from_immutable_literals_not_executed_source(tmp_path):
    source = write_prompt_source(tmp_path, code=(
        "POLICY = 'readiness_prompt_v1'\nSYSTEM_PREAMBLE = ('First. ' 'Second.')\n"
        "raise RuntimeError('This source must never execute')\n"))
    assert data.saved_prompt_identity(tmp_path) == {"policy": data.PROMPT_POLICY,
        "prompt_sha256": hashlib.sha256(b"First. Second.").hexdigest()}
    source.write_text("POLICY = 'tampered'\n", encoding="utf-8")
    with pytest.raises(ValueError, match="differs from its manifest"):
        data.saved_prompt_identity(tmp_path)


@pytest.mark.parametrize("code", [
    "POLICY = 'readiness_prompt_v1'\nSYSTEM_PREAMBLE = get_prompt()\n",
    "POLICY = 'readiness_prompt_v1'\nPOLICY = 'readiness_prompt_v1'\nSYSTEM_PREAMBLE = 'text'\n",
])
def test_prompt_identity_rejects_dynamic_or_ambiguous_contract(tmp_path, code):
    write_prompt_source(tmp_path, code=code)
    with pytest.raises(ValueError):
        data.saved_prompt_identity(tmp_path)


def test_prompt_saved_binding_requires_all_envelopes_and_actual_runtime_policy(tmp_path):
    envelopes, rows = prompt_saved_objects(tmp_path)
    assert data.validate_prompt_campaign(tmp_path, *envelopes, rows) == envelopes[0]["agent_policy_identity"]
    envelopes[2]["agent_policy_identity"]["prompt_sha256"] = "b" * 64
    with pytest.raises(ValueError, match="bound consistently"):
        data.validate_prompt_campaign(tmp_path, *envelopes, rows)
    envelopes[2]["agent_policy_identity"] = deepcopy(envelopes[0]["agent_policy_identity"])
    rows[0]["runs"][0]["runtime_task_control"] = "readiness_state_v1"
    with pytest.raises(ValueError, match="captured Runtime policy"):
        data.validate_prompt_campaign(tmp_path, *envelopes, rows)
    rows[0]["runs"][0]["runtime_task_control"] = "off"
    rows[0]["step_observations"] = [{"system": "A different run prefix"}]
    with pytest.raises(ValueError, match="model inputs"):
        data.validate_prompt_campaign(tmp_path, *envelopes, rows)


@pytest.mark.parametrize("change", ["helper", "over_budget", "missing_receipt"])
def test_prompt_saved_binding_rejects_hidden_helper_or_wrong_budget(tmp_path, change):
    envelopes, rows = prompt_saved_objects(tmp_path)
    run = rows[0]["runs"][0]
    if change == "helper":
        run["model_request_purposes"] = {"task_control": 1}
        run["model_request_receipts"][0]["purpose"] = "task_control"
    elif change == "over_budget":
        run["model_request_receipts"] *= 21
        run["model_request_purposes"] = {"main": 21}
        run["runtime_budget"]["model_requests"] = 21
    else:
        run["model_request_receipts"] = []
    with pytest.raises(ValueError, match="main requests within"):
        data.validate_prompt_campaign(tmp_path, *envelopes, rows)


@pytest.mark.bfcl_evidence
def test_prompt_candidate_pairs_with_a0_and_a3_without_changing_metric_definitions(baseline):
    prompt = prompt_candidate_fixture(baseline)
    for old in (baseline, candidate_fixture(baseline)):
        paired = compare_campaigns(old, prompt)
        assert paired["unique_tasks"] == 40 and paired["paired_scorable_tasks"] == 40
        assert paired["agent_policies"]["candidate"] == data.PROMPT_POLICY
        assert paired["main_success_rate_delta"] == 0
        assert not paired["transitions"]["gained"] and not paired["transitions"]["regressed"]
    changed = prompt["records"][0]
    changed["config_identity"]["snapshot"]["agent_policy_identity"]["prompt_sha256"] = "b" * 64
    changed["config_identity"]["sha256"] = _digest(changed["config_identity"]["snapshot"])
    changed["record_sha256"] = _digest({key: value for key, value in changed.items() if key != "record_sha256"})
    with pytest.raises(ValueError, match="policy identities differ"):
        compare_campaigns(baseline, prompt)


@pytest.mark.bfcl_evidence
def test_saved_forty_task_boundaries_identity_and_redacted_trajectories(baseline):
    assert baseline["summary"]["tasks"] == baseline["summary"]["scorable"] == 40
    assert baseline["summary"]["passed"] == 11 and baseline["summary"]["main_success_rate"] == 0.275
    assert baseline["summary"]["recorded_model_requests"] == 475
    assert baseline["summary"]["recorded_tokens"]["reasoning_tokens"] == 0
    assert sum(len(record["turns"]) for record in baseline["records"]) == 156
    for record in baseline["records"]:
        assert record["source_identity"]["current_workspace_source_used"] is False
        assert record["config_identity"]["snapshot"]["maximum_concurrency"] == 20
        assert record["config_identity"]["snapshot"]["agent_policy"] == "off"
        validate_record(record)
    assert not baseline["summary"]["excluded"]


def test_unsealed_campaign_refused_before_reading_control(tmp_path):
    with pytest.raises(ValueError, match="sealed completed"):
        load_campaign(tmp_path, label="A3")


@pytest.mark.parametrize("field,value", [("model", "other"), ("reasoning_effort", "high"),
                                        ("base_url", "https://other.invalid"), ("protocol", "openai_chat")])
@pytest.mark.bfcl_evidence
def test_wrong_saved_model_identity_is_not_paired(baseline, field, value):
    control = {"profile": deepcopy(baseline["identity"]["model"])}
    control["profile"][field] = value
    with pytest.raises(ValueError, match="official DeepSeek"):
        _profile_identity(control)


@pytest.mark.bfcl_evidence
def test_same_saved_run_cannot_be_relabelled_as_optimization(baseline):
    candidate = deepcopy(baseline)
    candidate["label"] = "A3"
    with pytest.raises(ValueError, match="relabel"):
        compare_campaigns(baseline, candidate)


@pytest.mark.bfcl_evidence
def test_pairing_rejects_different_tasks_and_budgets(baseline):
    candidate = candidate_fixture(baseline)
    candidate["records"].reverse()
    with pytest.raises(ValueError, match="Paired tasks"):
        compare_campaigns(baseline, candidate)
    candidate = candidate_fixture(baseline)
    candidate["identity"]["runtime_policy"]["max_output_tokens"] = 4096
    with pytest.raises(ValueError, match="matching model, task, budget"):
        compare_campaigns(baseline, candidate)


@pytest.mark.bfcl_evidence
def test_paired_transition_lists_and_unknown_denominator_are_explicit(baseline):
    candidate = candidate_fixture(baseline)
    changed = next(record for record in candidate["records"] if record["scoring"]["passed"] is False)
    changed["scoring"]["passed"] = True
    changed["scoring"]["formal_result"] = "pass"
    changed["scoring"]["turn_checks"] = [{"valid": True} for _turn in changed["turns"]]
    changed["outcome"]["category"] = "pass"
    changed["record_sha256"] = _digest({key: value for key, value in changed.items() if key != "record_sha256"})
    candidate["summary"] = summarize_campaign(candidate["records"], candidate["report"])
    report = compare_campaigns(baseline, candidate)
    assert report["transitions"]["gained"] == [changed["task_id"]]
    assert report["main_success_rate_delta_percentage_points"] == pytest.approx(2.5)
    changed["scoring"]["passed"] = None
    changed["scoring"]["formal_result"] = "fail"
    changed["outcome"]["category"] = "infrastructure_error"
    changed["record_sha256"] = _digest({key: value for key, value in changed.items() if key != "record_sha256"})
    candidate["summary"] = summarize_campaign(candidate["records"], candidate["report"])
    report = compare_campaigns(baseline, candidate)
    assert report["main_success_rate_delta"] is None
    assert report["paired_scorable_tasks"] == 39
    assert report["transitions"]["unscorable_pair"] == [changed["task_id"]]


@pytest.mark.bfcl_evidence
def test_typed_runtime_state_error_is_null_but_known_control_contract_error_is_zero(baseline):
    record = deepcopy(next(record for record in baseline["records"] if record["scoring"]["passed"] is False))
    row = deepcopy(next(row for row in baseline["rows"] if row["id"] == record["task_id"]))
    row["runs"][0].update(runtime_status="failed", runtime_diagnostic={"error_type": "StateError"})
    result = normalize_runtime_outcomes([record], [row])[0]
    assert result["scoring"]["passed"] is None and result["outcome"]["category"] == "infrastructure_error"
    row["runs"][0]["runtime_diagnostic"]["error_type"] = "TaskControlError"
    row["runs"][0]["agent_control_failure"] = True
    row["agent_control_failure"] = True
    result = normalize_runtime_outcomes([record], [row])[0]
    assert result["scoring"]["passed"] is False and result["outcome"]["category"] == "task_failed"
    row["runs"][0]["runtime_diagnostic"]["error_type"] = "StateError"
    assert normalize_runtime_outcomes([record], [row])[0]["scoring"]["passed"] is None


@pytest.mark.bfcl_evidence
def test_fixed_budget_exhaustion_remains_a_scored_failure(baseline):
    record = deepcopy(next(record for record in baseline["records"] if record["scoring"]["passed"] is False))
    row = deepcopy(next(row for row in baseline["rows"] if row["id"] == record["task_id"]))
    row["runs"][0].update(runtime_status="budget_exhausted", runtime_diagnostic={})
    result = normalize_runtime_outcomes([record], [row])[0]
    assert result["scoring"]["passed"] is False and result["outcome"]["category"] == "budget_terminated"


@pytest.mark.bfcl_evidence
def test_auxiliaries_have_explicit_reference_scope_and_do_not_change_main(baseline):
    values = {}
    digests = [record["record_sha256"] for record in baseline["records"]]
    for kind in ("no_premature_calls", "post_blocked_continuation", "nonempty_turn_checks"):
        counts = [auxiliary_turn_counts(record, baseline["reference_call_counts"][record["task_id"]], kind)
                  for record in baseline["records"]]
        values[kind] = (sum(good for good, _total in counts), sum(total for _good, total in counts))
    assert values == {"no_premature_calls": (0, 20), "post_blocked_continuation": (29, 46),
                      "nonempty_turn_checks": (97, 136)}
    assert digests == [record["record_sha256"] for record in baseline["records"]]
    assert len(diagnostic_warnings()["family4_source_semantic_warning"]) == 4


@pytest.mark.bfcl_evidence
def test_unknown_tokens_are_not_summed_as_zero(baseline):
    records = deepcopy(baseline["records"])
    records[0]["recorded_usage"]["input_tokens"] = None
    summary = summarize_campaign(records, baseline["report"])
    assert summary["recorded_tokens"]["input_tokens"] is None
    assert summary["recorded_tokens"]["output_tokens"] == 46381


def test_child_environment_drops_keys_proxies_and_cloud_state(monkeypatch, tmp_path):
    for key in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "CONFIDENT_API_KEY", "HTTP_PROXY"):
        monkeypatch.setenv(key, "private-unit-test-value")
    env = child_environment(tmp_path)
    assert not any(key in env for key in ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "CONFIDENT_API_KEY", "HTTP_PROXY"))
    assert env["DEEPEVAL_TELEMETRY_OPT_OUT"] == env["DEEPEVAL_DISABLE_DOTENV"] == "1"
    assert env["CONFIDENT_TRACE_FLUSH"] == "0"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only isolated metadata projection")
def test_isolated_windows_metadata_never_queries_wmi_or_spawns_ver(monkeypatch):
    import platform

    def forbidden(*args, **kwargs):
        raise AssertionError("Unbounded OS metadata collection is forbidden in this test")

    # Register restoration before the worker changes these process-local functions.
    monkeypatch.setattr(platform, "uname", platform.uname)
    monkeypatch.setattr(platform, "win32_ver", platform.win32_ver)
    monkeypatch.setattr(platform, "_wmi_query", forbidden)
    monkeypatch.setattr(platform, "_syscmd_ver", forbidden)
    monkeypatch.setattr(platform, "_platform_cache", {})
    metadata = _module("comparison_worker").configure_host_metadata()
    observed = sys.getwindowsversion()
    expected = list(getattr(observed, "platform_version", observed[:3]))
    assert metadata["kernel_version"] == expected and metadata["wmi_queried"] is False
    assert metadata["dependency_files_changed"] is metadata["affects_metric_definitions"] is False
    assert platform.system() == "Windows"
    assert len(tuple(platform.uname())) == 6 and platform.uname().processor == ""
    assert platform.uname().machine == platform.uname().node == ""
    assert "kernel-" in platform.platform() and platform.win32_ver()[1] == ".".join(map(str, expected))


@pytest.mark.bfcl_evidence
def test_actual_native_deepeval_metrics_are_deterministic_without_a_model(baseline):
    if os.environ.get("DEEPEVAL_TELEMETRY_OPT_OUT") != "1":
        pytest.skip("Use the isolated DeepEval comparison launcher for the actual framework test")
    if importlib.util.find_spec("deepeval") is None:
        pytest.skip("DeepEval lives only in the project-local evaluation environment")
    from evals.bfcl_multiturn.deepeval.adapter import BFCLSavedTaskMetric, record_to_conversation
    from evals.bfcl_multiturn.deepeval.comparison_metrics import (
        BFCLDeterministicAuxiliaryMetric,
        evaluate_metric,
    )

    record = deepcopy(next(record for record in baseline["records"] if record["subcategory"] == "multi_turn_miss_param"))
    # Metadata retention fixture only; it does not relabel this sealed A0 task
    # as a measured gate campaign or change its official result.
    record["capability_gate"] = {"fixture_only": True, "turns": [{"status": "fallback_to_native",
        "parse_status": "invalid_json", "input_sha256": "a" * 64, "response_sha256": "b" * 64,
        "model_requests": [{"purpose": "capability_check", "request_number": 1,
                            "recorded_usage": {field: None for field in data.TOKEN_FIELDS}}]}]}
    record["record_sha256"] = _digest({key: value for key, value in record.items() if key != "record_sha256"})
    case = record_to_conversation(record)
    assert case.metadata["aster_bfcl_record"]["capability_gate"] == record["capability_gate"]
    case.metadata["bfcl_aux_reference_call_counts"] = baseline["reference_call_counts"][record["task_id"]]
    metric = BFCLDeterministicAuxiliaryMetric("no_premature_calls")
    assert metric.measure(case) == 0 and metric.model is None and metric.evaluation_cost == 0
    main = evaluate_metric([case], BFCLSavedTaskMetric(), identifier="offline-comparison-test-main")
    auxiliary = evaluate_metric([case], metric, identifier="offline-comparison-test-aux")
    assert main.test_results[0].success is record["scoring"]["passed"]
    assert auxiliary.test_results[0].metrics_data[0].score == 0
    assert main.confident_link is auxiliary.confident_link is None
    assert main.test_run_id is auxiliary.test_run_id is None
    assert record["record_sha256"] == _digest({key: value for key, value in record.items() if key != "record_sha256"})
