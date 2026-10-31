"""Reject prior effects/unknown outcomes; supplement only the uninvoked slot."""
from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from bfcl_test_prerequisites import require_frozen_runtimes

PROJECT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("zero_dispatch_supplement_tests", PROJECT /
    "evals/bfcl_multiturn/promptfoo/run_holdout_zero_dispatch_supplement.py")
adapter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(adapter)


@pytest.fixture(scope="module")
def original():
    require_frozen_runtimes()
    return adapter.verify_original(PROJECT)


@pytest.mark.bfcl_evidence
def test_actual_original_slot_is_proven_zero_dispatch_without_mutating_it(original):
    proof, case, group = original
    assert proof["original_files_verified"] == 1278
    assert proof["eligibility"]["task_HTTP_attempts"] == proof["eligibility"]["task_run_count"] == 0
    assert proof["eligibility"]["intent_exists"] is proof["eligibility"]["case_output_exists"] is False
    assert case["id"] == group["group"] == adapter.TASK_ID
    assert len(case["question"]) == 6 and adapter.case_digest(case) == adapter.CASE_SHA256
    assert proof["new_model_api_calls_from_verification"] == proof["new_judge_api_calls"] == 0


@pytest.mark.parametrize("field,value", [
    ("task_HTTP_attempts", 1), ("unknown_HTTP_outcomes", 1), ("global_unknown_HTTP_outcomes", 1),
    ("intent_exists", True), ("protocol_exists", True), ("case_output_exists", True),
    ("task_run_count", 1), ("formal_result", "fail"), ("reason", "unknown_prior_task_outcome"),
    ("callback_error_type", "ProviderError"), ("callback_outcome", "backend_returned"),
    ("callback_guard_blocked_attempts", 1), ("original_sealed", False), ("original_test_only", True),
    ("configuration_label", "A0"), ("source_commit", "changed"), ("case_sha256", "changed"),
    ("task_id", "multi_turn_base_34"),
])
@pytest.mark.bfcl_evidence
def test_executed_unknown_rebound_or_unsealed_tasks_cannot_enter_a_supplement(original, field, value):
    facts = copy.deepcopy(original[0]["eligibility"])
    facts[field] = value
    with pytest.raises(ValueError):
        adapter.require_zero_dispatch(facts)


@pytest.mark.bfcl_evidence
def test_missing_proof_field_is_not_treated_as_zero(original):
    facts = copy.deepcopy(original[0]["eligibility"])
    del facts["task_HTTP_attempts"]
    with pytest.raises(ValueError):
        adapter.require_zero_dispatch(facts)


def test_corrupted_frozen_original_manifest_rejected_before_source_or_credentials(tmp_path):
    proof = tmp_path / adapter.FROZEN_MANIFEST_RELATIVE
    proof.parent.mkdir(parents=True)
    proof.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="authority changed"):
        adapter.verify_original(tmp_path)


def test_nonselected_task_rejected_before_reading_any_original_path(tmp_path):
    with pytest.raises(ValueError, match="predeclared"):
        adapter.verify_original(tmp_path, "multi_turn_base_34")


@pytest.mark.bfcl_evidence
def test_supplement_scope_is_one_case_and_never_exports_a_full_sixty_score(original):
    proof, case, group = original
    parent_plan = {"groups": [group] * 60, "question_record_sha256": {adapter.TASK_ID: "fixture-hash"}}
    runtime = SimpleNamespace(preflight=lambda *_a: copy.deepcopy(parent_plan), _load=lambda *_a: None,
        collect_campaign=lambda *_a: {"main_success_rate": 1.0, "baseline_assessment": {}},
        build_config=lambda *_a, **_k: {"tests": [{"metadata": {}}]})
    runtime = adapter.bind_adapter(runtime, proof, case, group)
    plan = runtime.preflight("A3", 20)
    assert len(plan["groups"]) == plan["planned_entries"] == plan["evaluation_task_count"] == 1
    assert plan["planned_holdout_tasks"] == 1 and plan["authority_holdout_tasks"] == 60
    assert plan["full_60_main_success_rate"] is None and plan["defined_user_turns"] == 6
    assert runtime._load("holdout_live_dataset", None).load_holdout_cases() == [case]
    report = runtime.collect_campaign(None, plan, None, None)
    assert report["single_task_success_rate"] == 1.0 and report["full_60_main_success_rate"] is None
    assert report["evaluation_task_count"] == 1 and report["supplement_of"] == proof["original_campaign"]


def test_started_supplement_cannot_be_resumed(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter, "verify_original", lambda *_: ({"original_campaign": str(tmp_path / "original")}, None, None))
    (tmp_path / "execution-started.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(adapter.subprocess, "run", lambda *_a, **_k: pytest.fail("must not spawn"))
    with pytest.raises(ValueError, match="already started"):
        adapter.main(["--campaign-dir", str(tmp_path), "--execute"])
