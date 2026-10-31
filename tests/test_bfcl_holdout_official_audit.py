"""Project-environment official simulator tests, separate from DeepEval deps."""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from bfcl_test_prerequisites import require_campaign, require_corpus

from evals.bfcl_multiturn.deepeval import comparison_data as saved
from evals.bfcl_multiturn.deepeval import holdout_official_audit as audit
from evals.bfcl_multiturn.promptfoo.compact_holdout_dataset import load_holdout_dataset

PROJECT = Path(__file__).resolve().parents[1]


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.fixture(scope="module")
def old_baseline():
    require_campaign("compact-dev40-c20-20261030")
    return saved.load_campaign(PROJECT / "artifacts/bfcl/compact-dev40-c20-20261030", label="A0")


@pytest.mark.bfcl_evidence
def test_official_audit_reconstructs_original_complete_turns(old_baseline, tmp_path, monkeypatch):
    campaign = deepcopy(old_baseline)
    campaign["rows"] = campaign["rows"][:1]
    original_cases = json.loads((Path(campaign["root"]) / "private-cases.json").read_bytes())[:1]
    campaign["root"] = str(tmp_path / "source")
    write(Path(campaign["root"]) / "private-cases.json", original_cases)
    # Replay-stage unit test on an exposed example. Production source admission
    # is separately tested below, without changing any benchmark or saved run.
    monkeypatch.setattr(audit, "verify_case_sources", lambda cases: {"source_revision": "synthetic-test-only"})
    result = audit.audit_campaign(campaign, tmp_path / "audit")
    assert result["completed_official_turns_verified"] == len(campaign["rows"][0]["runs"])
    assert result["simulator_outputs_replayed"] == len(campaign["rows"][0]["simulator_results"])
    assert result["scores_replaced"] is False
    assert result["new_model_api_calls"] == result["new_judge_api_calls"] == 0


@pytest.mark.bfcl_evidence
def test_official_audit_refuses_changed_simulator_output(old_baseline, tmp_path, monkeypatch):
    campaign = deepcopy(old_baseline)
    campaign["rows"] = campaign["rows"][:1]
    original_cases = json.loads((Path(campaign["root"]) / "private-cases.json").read_bytes())[:1]
    campaign["root"] = str(tmp_path / "source")
    write(Path(campaign["root"]) / "private-cases.json", original_cases)
    entry = next(item for item in campaign["rows"][0]["tool_ledger"]
                 if item.get("result", {}).get("data", {}).get("bfcl_simulator") is True)
    entry["result"]["content"] = "SYNTHETIC_TAMPER_NOT_A_REAL_TOOL_RETURN"
    monkeypatch.setattr(audit, "verify_case_sources", lambda cases: {"source_revision": "synthetic-test-only"})
    with pytest.raises(ValueError, match="recorded_simulator_output"):
        audit.audit_campaign(campaign, tmp_path / "audit")


@pytest.mark.bfcl_evidence
def test_official_audit_source_admission_accepts_only_relocated_pinned_sixty():
    require_corpus()
    cases, _metadata = load_holdout_dataset()
    for case in cases:
        case["provenance"]["corpus_dir"] = "synthetic-relocated-corpus-only"
    result = audit.verify_case_sources(cases)
    assert result["allocated_holdout_tasks"] == 60
    cases[0]["expected"] = [[] for _turn in cases[0]["expected"]]
    with pytest.raises(ValueError, match="raw_source_normalization"):
        audit.verify_case_sources(cases)
