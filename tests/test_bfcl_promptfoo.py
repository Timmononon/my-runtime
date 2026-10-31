"""Offline contracts for promptfoo's complete-task saved-result adapter."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
from bfcl_test_prerequisites import require_campaign

from aster.evaluation.bfcl_multiturn_export import load_saved_campaign, write_bundle

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "evals/bfcl_multiturn/promptfoo"


def _module(name):
    spec = importlib.util.spec_from_file_location("test_bfcl_promptfoo_" + name, ADAPTER / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def saved_records():
    require_campaign("v4-live-smoke-20261029")
    return load_saved_campaign(ROOT / "artifacts/bfcl/v4-live-smoke-20261029")


@pytest.fixture
def bundle(tmp_path, saved_records):
    target = tmp_path / "bundle"
    write_bundle(copy.deepcopy(saved_records), target)
    return target


def _context(row):
    digest = hashlib.sha256(json.dumps(row, sort_keys=True, separators=(",", ":"),
                                       ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    return {"vars": {"task_id": row["task_id"]},
            "test": {"metadata": {"record_digests": {row["config_label"]: digest}}}}


@pytest.mark.bfcl_evidence
def test_provider_returns_complete_task_with_zero_new_usage(bundle, saved_records):
    provider = _module("provider")
    row = saved_records[0]
    result = provider.call_api("unused", {"config": {"bundle_dir": str(bundle)}}, _context(row))
    output = json.loads(result["output"])
    assert len(output["turns"]) == len(row["turns"])
    assert output == row
    assert result["tokenUsage"] == {"total": 0, "prompt": 0, "completion": 0}
    assert result["metadata"]["recorded_usage"]["model_requests"] == 8
    assert result["metadata"]["new_model_api_calls"] == 0


@pytest.mark.bfcl_evidence
def test_provider_rejects_unmatched_task(bundle):
    with pytest.raises(ValueError, match="matching complete task"):
        _module("provider").call_api("", {"config": {"bundle_dir": str(bundle)}},
                                     {"vars": {"task_id": "multi_turn_base_missing"}})


@pytest.mark.bfcl_evidence
def test_assertion_preserves_official_failures(saved_records):
    assertion = _module("assertion")
    assert [assertion.get_assert(json.dumps(row), _context(row))["pass"] for row in saved_records] == [
        False, False, True, True]


@pytest.mark.bfcl_evidence
def test_assertion_rejects_score_tampering(saved_records):
    row = copy.deepcopy(saved_records[0])
    context = _context(row)
    row["scoring"]["passed"] = True
    with pytest.raises(ValueError, match="record digest"):
        _module("assertion").get_assert(json.dumps(row), context)


@pytest.mark.bfcl_evidence
def test_assertion_rejects_unscoreable_result(saved_records):
    row = copy.deepcopy(saved_records[0])
    row["scoring"]["passed"] = None
    with pytest.raises(ValueError, match="Unscoreable"):
        _module("assertion").get_assert(json.dumps(row), _context(row))


@pytest.mark.bfcl_evidence
def test_generator_one_case_per_complete_task(bundle):
    config, comparison = _module("run_offline").build_config([bundle], python_executable=Path("python.exe"))
    assert len(config["tests"]) == 4
    assert len(config["providers"]) == 1
    assert config["sharing"] is False
    assert config["defaultTest"]["assert"][0]["type"] == "python"
    value = next(iter(comparison["configs"].values()))
    assert value["recorded_success_rate"] == 0.5
    assert value["recorded_usage"]["model_requests"] == {"known_total": 32, "unknown_tasks": 0}


@pytest.mark.bfcl_evidence
def test_generator_excludes_unscoreable_and_lists_missing(monkeypatch, saved_records):
    module = _module("run_offline")
    left, right = copy.deepcopy(saved_records), copy.deepcopy(saved_records)
    for row in right:
        row["config_label"] = "unit-test-other-recorded-label"
    left[0]["scoring"]["passed"] = None
    right.pop()
    monkeypatch.setattr(module, "load_bundle", lambda path: left if path.name == "left" else right)
    config, comparison = module.build_config([Path("left"), Path("right")], python_executable=Path("python.exe"))
    assert len(config["tests"]) == 2
    assert len(config["providers"]) == 2
    assert comparison["excluded_unscoreable_task_ids"] == [left[0]["task_id"]]
    assert comparison["unpaired_task_ids"] == [left[-1]["task_id"]]
    assert len(comparison["excluded_unscoreable_records"]) == 2


@pytest.mark.bfcl_evidence
def test_generator_rejects_mixed_identity_under_one_label(monkeypatch, saved_records):
    module = _module("run_offline")
    rows = copy.deepcopy(saved_records)
    rows[1]["config_identity"]["sha256"] = "f" * 64
    monkeypatch.setattr(module, "load_bundle", lambda path: rows)
    with pytest.raises(ValueError, match="mix different recorded identities"):
        module.build_config([Path("one")], python_executable=Path("python.exe"))


@pytest.mark.bfcl_evidence
def test_generator_rejects_same_id_with_different_question(monkeypatch, saved_records):
    module = _module("run_offline")
    left, right = copy.deepcopy(saved_records), copy.deepcopy(saved_records)
    for row in right:
        row["config_label"] = "unit-test-other-recorded-label"
    right[0]["provenance"]["question_sha256"] = "f" * 64
    monkeypatch.setattr(module, "load_bundle", lambda path: left if path.name == "left" else right)
    with pytest.raises(ValueError, match="Shared task question"):
        module.build_config([Path("left"), Path("right")], python_executable=Path("python.exe"))


@pytest.mark.bfcl_evidence
def test_provider_filters_label_before_task_uniqueness(monkeypatch, saved_records):
    provider = _module("provider")
    row, other = copy.deepcopy(saved_records[0]), copy.deepcopy(saved_records[0])
    other["config_label"] = "unit-test-other-recorded-label"
    monkeypatch.setattr(provider, "load_bundle", lambda path: [row, other])
    response = provider.call_api("", {"config": {"bundle_dir": "unit-bundle", "config_label": other["config_label"]}},
                                 _context(other))
    assert json.loads(response["output"])["config_label"] == other["config_label"]


def test_child_environment_strips_keys_and_enables_guards(monkeypatch, tmp_path):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fake-unit-test-key")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-unit-test-key")
    env = _module("run_offline").child_environment(tmp_path, Path("python.exe"))
    assert "DEEPSEEK_API_KEY" not in env and "OPENAI_API_KEY" not in env
    assert env["PROMPTFOO_DISABLE_TELEMETRY"] == "1"
    assert "network_guard.cjs" in env["NODE_OPTIONS"]
    assert "guards" in env["PYTHONPATH"]


@pytest.mark.parametrize("corruption", ("missing_row", "duplicate_pair", "framework_error", "score", "assertion"))
@pytest.mark.bfcl_evidence
def test_native_framework_validation_detects_corruption(bundle, saved_records, corruption):
    module = _module("run_offline")
    _, comparison = module.build_config([bundle], python_executable=Path("python.exe"))
    label = saved_records[0]["config_label"]
    payload = {"results": {"stats": {"successes": 2, "failures": 2, "errors": 0,
                                      "tokenUsage": {"total": 0}},
                           "results": [{
                               "success": row["scoring"]["passed"], "provider": {"label": label},
                               "vars": {"task_id": row["task_id"]},
                               "response": {"output": json.dumps(row)},
                               "gradingResult": {"componentResults": [{"reason": "Saved BFCL result: test"}]},
                           } for row in saved_records]}}
    assert module.validate_framework_results(payload, comparison) == []
    if corruption == "missing_row":
        payload["results"]["results"].pop()
    elif corruption == "duplicate_pair":
        payload["results"]["results"][1] = payload["results"]["results"][0]
    elif corruption == "framework_error":
        payload["results"]["stats"]["errors"] = 1
    elif corruption == "score":
        payload["results"]["results"][0]["success"] = True
    elif corruption == "assertion":
        payload["results"]["results"][0]["gradingResult"]["componentResults"][0]["reason"] = "Python exception"
    assert module.validate_framework_results(payload, comparison)
