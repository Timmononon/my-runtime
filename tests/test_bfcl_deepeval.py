"""Run with the isolated DeepEval launcher; normal Aster tests need no new deps."""
from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from pathlib import Path

import pytest
from bfcl_test_prerequisites import require_campaign

if os.environ.get("DEEPEVAL_TELEMETRY_OPT_OUT") != "1" or os.environ.get("DEEPEVAL_DISABLE_DOTENV") != "1":
    pytest.skip("Use the isolated DeepEval offline launcher", allow_module_level=True)
pytest.importorskip("deepeval")

from aster.evaluation.bfcl_multiturn_export import load_bundle, load_saved_campaign, write_bundle
from evals.bfcl_multiturn.deepeval.adapter import (
    BFCLSavedTaskMetric,
    auxiliary_observations,
    evaluate_records,
    record_to_conversation,
)
from evals.bfcl_multiturn.deepeval.run_offline import child_environment


@pytest.fixture(scope="module")
def records():
    require_campaign("v4-live-smoke-20261029")
    root = Path(__file__).resolve().parents[1]
    return load_saved_campaign(root / "artifacts" / "bfcl" / "v4-live-smoke-20261029",
                               config_label="as-run-deepseek-flash-low")


@pytest.mark.bfcl_evidence
def test_complete_tasks_roundtrip_without_relabeling(records, tmp_path):
    write_bundle(records, tmp_path / "bundle")
    loaded = load_bundle(tmp_path / "bundle")
    cases = [record_to_conversation(record) for record in loaded]
    assert len(cases) == 4
    for record, case in zip(loaded, cases, strict=True):
        assert case.metadata["aster_bfcl_record"] == record
        assert len(case.turns) == sum(message["role"] in {"user", "assistant"}
                                      for turn in record["turns"] for message in turn["messages"])
        assert not case.metadata["model_capability_measurement"]
        assert not case.metadata["judge_enabled"]


@pytest.mark.bfcl_evidence
def test_native_tool_outputs_and_step_order_preserved(records):
    for record in records:
        case = record_to_conversation(record)
        for user_turn in record["turns"]:
            responses = {message["tool_call_id"]: message["content"]
                         for message in user_turn["messages"] if message["role"] == "tool"}
            for native in (turn for turn in case.turns
                           if turn.metadata["bfcl_user_turn_index"] == user_turn["turn_index"]):
                index = native.metadata["canonical_message_index"]
                message = user_turn["messages"][index]
                assert native.content == (message["content"] or "")
                assert len(native.tools_called or []) == len(message["tool_calls"])
                for call, original in zip(native.tools_called or [], message["tool_calls"], strict=True):
                    assert call.name == original["name"]
                    assert call.input_parameters == original["arguments"]
                    assert call.output == responses.get(original["id"])


@pytest.mark.bfcl_evidence
def test_budget_termination_not_overridden_by_valid_diagnostic_states(records):
    record = next(row for row in records if row["task_id"] == "multi_turn_base_151")
    assert record["scoring"]["passed"] is False
    assert record["scoring"]["diagnostic_turn_checks"]
    metric = BFCLSavedTaskMetric()
    assert metric.measure(record_to_conversation(record)) == 0.0
    assert not metric.is_successful()
    assert metric.evaluation_cost == 0
    assert metric.model is None
    assert "budget_terminated" in metric.reason


@pytest.mark.bfcl_evidence
def test_missing_function_future_turns_not_fabricated(records):
    record = next(row for row in records if row["subcategory"] == "multi_turn_miss_func")
    case = record_to_conversation(record)
    assert len(record["turns"]) == 3
    assert [turn["runtime_status"] for turn in record["turns"]][1:] == ["not_run", "not_run"]
    assert {turn.metadata["bfcl_user_turn_index"] for turn in case.turns} == {0}
    assert len(case.metadata["aster_bfcl_record"]["turns"]) == 3


@pytest.mark.bfcl_evidence
def test_native_evaluate_reproduces_all_four_task_results(records):
    result, cases, excluded = evaluate_records(records)
    assert not excluded
    assert len(cases) == len(result.test_results) == 4
    assert {row.name: row.success for row in result.test_results} == {
        row["task_id"]: row["scoring"]["passed"] for row in records}
    assert result.confident_link is None
    assert result.test_run_id is None
    assert sum(row.success for row in result.test_results) == 2


@pytest.mark.bfcl_evidence
def test_unknown_outcome_excluded_instead_of_zero(records):
    unknown = deepcopy(records[0])
    unknown["scoring"]["passed"] = None
    unknown["scoring"]["formal_result"] = "blocked_dataset_conflict"
    unknown["outcome"] = {"category": "dataset_conflict", "reasons": ["test_dataset_conflict"]}
    unknown["record_sha256"] = hashlib.sha256(json.dumps(
        {key: value for key, value in unknown.items() if key != "record_sha256"},
        sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()).hexdigest()
    result, cases, excluded = evaluate_records([unknown])
    assert result is None and not cases
    assert excluded[0]["score"] is None
    with pytest.raises(ValueError, match="Unknown BFCL outcome"):
        BFCLSavedTaskMetric().measure(record_to_conversation(unknown))


@pytest.mark.bfcl_evidence
def test_tampered_score_cannot_enter_metric(records):
    record = deepcopy(records[0])
    record["scoring"]["passed"] = True
    with pytest.raises(ValueError):
        record_to_conversation(record)


@pytest.mark.bfcl_evidence
def test_auxiliaries_default_disabled_and_separate(records):
    record = deepcopy(records[1])
    before = deepcopy(record)
    values = auxiliary_observations(record)
    assert values["clarification_quality"]["score"] is None
    assert values["unavailable_tool_exploration"]["status"] == "not_implemented"
    assert values["observed_tool_calls"] == 23
    values = auxiliary_observations(record, extensions={
        "clarification_quality": lambda row: {"status": "test_only", "score": 0.1},
    })
    assert values["clarification_quality"]["score"] == 0.1
    assert record == before
    assert record["scoring"]["passed"] is False


def test_child_environment_drops_credentials_and_other_run_state(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-do-not-forward")
    monkeypatch.setenv("CONFIDENT_API_KEY", "fixture-do-not-forward")
    monkeypatch.setenv("DEEPEVAL_UPDATE_WARNING_OPT_IN", "1")
    monkeypatch.setenv("HTTP_PROXY", "fixture-do-not-forward")
    env = child_environment(tmp_path)
    assert not any(key in env for key in ("OPENAI_API_KEY", "CONFIDENT_API_KEY", "HTTP_PROXY"))
    assert env["DEEPEVAL_TELEMETRY_OPT_OUT"] == "1"
    assert env["DEEPEVAL_DISABLE_DOTENV"] == env["DEEPEVAL_DISABLE_LEGACY_KEYFILE"] == "1"
    assert env["DEEPEVAL_UPDATE_WARNING_OPT_IN"] == "0"
    assert env["HOME"] == str(tmp_path)
    assert env["ASTER_HOME"].startswith(str(tmp_path))


@pytest.mark.bfcl_evidence
def test_unknown_auxiliary_extension_rejected(records):
    with pytest.raises(ValueError, match="Unknown auxiliary"):
        auxiliary_observations(records[0], extensions={"unregistered": lambda row: 1.0})
