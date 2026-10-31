import importlib.util
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from bfcl_test_prerequisites import require_corpus

ADAPTER = Path(__file__).resolve().parents[1] / "evals/bfcl_multiturn/promptfoo"


def _module(name):
    spec = importlib.util.spec_from_file_location("test_no_thinking_" + name, ADAPTER / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


provider, assertion, runner = (_module(name) for name in (
    "no_thinking_provider", "no_thinking_assertion", "run_no_thinking"))


def row_for(task_id="multi_turn_base_151", result="pass"):
    return {"id": task_id, "result": result, "protocol_result": "pass",
            "scoring_verification": {"valid": result == "pass"}, "model_api_calls": 42,
            "usage": [{"input_tokens": 20, "output_tokens": 5}] * 42,
            "messages": [{"content": "large tool context" * 20000}]}


def install_fake_backend(monkeypatch, row):
    calls = []

    @contextmanager
    def phase():
        yield

    async def run_one(task_id, campaign):
        calls.append((task_id, campaign))
        return row

    backend = ModuleType("aster.evaluation.bfcl_multiturn_unmetered")
    backend.run_one_current_multiturn = run_one
    backend.read_task_receipt = lambda *_: row
    monkeypatch.setitem(sys.modules, "aster.evaluation.bfcl_multiturn_unmetered", backend)
    monkeypatch.setitem(sys.modules, "sitecustomize", SimpleNamespace(authorized_backend=phase))
    return calls


def test_complete_task_provider_keeps_all_recorded_calls_and_compact_no_thinking_identity(monkeypatch, tmp_path):
    row = row_for()
    calls = install_fake_backend(monkeypatch, row)
    response = provider.call_api("Replace original question with an easier task", {"config": {"campaign_dir": str(tmp_path)}},
                                 {"vars": {"task_id": row["id"]}})
    compact = json.loads(response["output"])
    assert calls == [(row["id"], tmp_path)]
    assert len(response["output"]) < 1200
    assert compact["model_api_calls"] == 42  # Never clipped to the old 20 whole-task cap.
    assert compact["api_count_limit"] is None
    assert compact["reasoning_effort"] == "none"
    assert response["tokenUsage"] == {"prompt": 840, "completion": 210, "total": 1050}
    context = {"vars": {"task_id": row["id"]}, "test": {"metadata": {"campaign_dir": str(tmp_path)}}}
    assert assertion.get_assert(response["output"], context)["pass"] is True
    compact["reasoning_effort"] = None
    with pytest.raises(ValueError, match="identity"):
        assertion.get_assert(json.dumps(compact), context)
    compact["reasoning_effort"] = "none"
    compact["backend_result_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="durable"):
        assertion.get_assert(json.dumps(compact), context)


def test_unknown_usage_stays_unknown_and_null_task_is_an_excluded_native_error(monkeypatch, tmp_path):
    row = row_for("multi_turn_base_173", "blocked_dataset_conflict")
    row.update(model_api_calls=0, usage=[])
    install_fake_backend(monkeypatch, row)
    response = provider.call_api("", {"config": {"campaign_dir": str(tmp_path)}}, {"vars": {"task_id": row["id"]}})
    compact = json.loads(response["output"])
    assert compact["main_score"] is None
    assert response["error"].startswith("EXCLUDED blocked_dataset_conflict")
    native = {"vars": {"task_id": row["id"]}, "provider": {"label": runner.PROVIDER_LABEL},
              "response": response, "error": response["error"], "success": False}
    assert runner.validate_native({"results": {"results": [native], "stats": {"errors": 1}}}, [row]) == []
    compact["api_count_limit"] = 200
    native["response"]["output"] = json.dumps(compact)
    assert "unexpected_no_thinking_execution_settings" in runner.validate_native(
        {"results": {"results": [native], "stats": {"errors": 1}}}, [row])
    row.update(model_api_calls=1, usage=[])
    response = provider.call_api("", {"config": {"campaign_dir": str(tmp_path)}}, {"vars": {"task_id": row["id"]}})
    assert response["tokenUsage"] == {}


@pytest.mark.bfcl_evidence
def test_preflight_retains_exact_twenty_with_no_extra_count_cap_and_default_is_offline(tmp_path):
    require_corpus()
    old = runner._helpers.preflight()
    new = runner.preflight()
    assert [(x["group"], x["question_sha256"], x["user_turns"]) for x in old["groups"]] == [
        (x["group"], x["question_sha256"], x["user_turns"]) for x in new["groups"]]
    assert new["planned_entries"] == 20
    assert new["authorized_unlimited_api"] is True
    assert new["authorized_api_call_limit"] is None
    assert new["per_case_api_call_cap"] is None
    assert new["reasoning_effort"] == "none"
    assert new["official_max_steps_per_user_turn"] == 20
    assert new["reserve_for_each_later_admissible_task"] == 0
    assert new["inherited_runtime_safety"]["max_total_tokens_per_user_run"] == 300000
    assert new["inherited_runtime_safety"]["duplicate_limit"] == 3
    assert all(x["api_call_cap"] is None and x["official_model_step_bound"] == 20*x["user_turns"] for x in new["groups"])
    output = tmp_path / "offline-preflight"
    assert runner.main(["--campaign-dir", str(output)]) == 0
    assert {path.name for path in output.iterdir()} == {"campaign-plan.json"}


@pytest.mark.bfcl_evidence
def test_native_config_uses_whole_tasks_new_provider_and_no_cache_repeat():
    require_corpus()
    config = runner.build_config(runner.preflight(), Path("D:/fixture-new-campaign"))
    assert len(config["tests"]) == 20
    assert len({test["vars"]["task_id"] for test in config["tests"]}) == 20
    assert config["providers"][0]["id"].endswith("no_thinking_provider.py")
    assert config["providers"][0]["label"] == runner.PROVIDER_LABEL
    assert config["providers"][0]["config"]["timeout"] > 3600 * 1000
    assert config["defaultTest"]["assert"][0]["value"] == "file://no_thinking_assertion.py"
    assert config["evaluateOptions"] == {"maxConcurrency": 1, "showProgressBar": False, "repeat": 1}


@pytest.mark.bfcl_evidence
def test_actual_launcher_plan_passes_production_admission_without_dispatching_http(monkeypatch, tmp_path):
    require_corpus()
    from test_bfcl_current import source_at

    from aster.evaluation import bfcl_multiturn_unmetered as backend
    from aster.evaluation.bfcl_current import capture_current_profile
    from aster.evaluation.bfcl_v4_data import load_corpus

    def forbidden_driver(*_args, **_kwargs):
        raise AssertionError("Admission must not construct a model client or dispatch HTTP")

    monkeypatch.setattr(backend, "_CurrentModelDriver", forbidden_driver)
    plan = runner.preflight()
    by_id = {case["id"]: case for case in load_corpus(runner._helpers.CORPUS,
                                                    tracks=list(runner._helpers.CATEGORIES))}
    cases = [by_id[group["group"]] for group in plan["groups"]]
    source = source_at(tmp_path)
    profile = capture_current_profile(source, provider_id="deepseek", model="deepseek-flash", reasoning_effort="none")
    output = tmp_path / "production-admission-no-http"
    output.mkdir()
    (output / "campaign-plan.json").write_text(json.dumps(plan), encoding="utf-8")
    (output / "source-manifest.json").write_text(json.dumps({"files": []}), encoding="utf-8")
    control = backend.initialize_campaign(cases, output, profile=profile, max_api_calls=None,
        max_output_tokens=8192, authorized_unlimited_api=True, _test_only=False)
    assert control["test_only"] is False  # Exercise the production plan contract, not the mock-only bypass.
    assert control["case_ids"] == [group["group"] for group in plan["groups"]]
    assert control["profile"]["reasoning_effort"] == "none"
    assert control["max_api_calls"] is None
    assert control["case_deadline_seconds"] == plan["case_deadline_seconds"] == 3600
    assert control["case_tool_safety_limit"] == plan["case_tool_safety_limit"] == 10000
    ledger = json.loads((output / "results/http-budget.json").read_text(encoding="utf-8"))
    assert ledger["calls"] == []
    assert ledger["maximum"] is None
    # The originally mismatched canonical field must be required even if its display alias remains.
    broken = tmp_path / "missing-canonical-policy"
    broken.mkdir()
    del plan["case_deadline_seconds"]
    (broken / "campaign-plan.json").write_text(json.dumps(plan), encoding="utf-8")
    (broken / "source-manifest.json").write_text(json.dumps({"files": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="policy"):
        backend.initialize_campaign(cases, broken, profile=profile, max_api_calls=None,
            max_output_tokens=8192, authorized_unlimited_api=True, _test_only=False)
    assert not (broken / "results").exists()
