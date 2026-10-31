"""Offline scenarios for promptfoo-only per-task campaign isolation."""
from __future__ import annotations

import importlib.util
import json
from copy import deepcopy
from pathlib import Path

import pytest
from bfcl_test_prerequisites import require_corpus

ADAPTER = Path(__file__).resolve().parents[1] / "evals/bfcl_multiturn/promptfoo"


def _module(name):
    spec = importlib.util.spec_from_file_location("test_task_isolated_" + name, ADAPTER / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner, provider = (_module(name) for name in ("run_task_isolated", "task_isolated_provider"))


def _write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def batch(tmp_path):
    require_corpus()
    from test_bfcl_current import source_at

    from aster.evaluation.bfcl_current import capture_current_profile
    from aster.evaluation.bfcl_v4_data import load_corpus

    root = tmp_path / "isolated-batch"
    root.mkdir()
    plan = runner.preflight()
    runner._helpers.new_json(root / "campaign-plan.json", plan)
    runner._helpers.new_json(root / "source-manifest.json", {"files": []})
    (root / "as-run-source").mkdir()
    source = source_at(tmp_path)
    profile = capture_current_profile(source, provider_id="deepseek", model="deepseek-flash", reasoning_effort="none")
    by_id = {case["id"]: case for case in load_corpus(runner._helpers.CORPUS, tracks=list(runner._helpers.CATEGORIES))}
    cases = [by_id[group["group"]] for group in plan["groups"]]
    control, _ = runner.initialize_children(root, plan, cases, profile)
    digest = provider.digest((root / "batch-control.json").read_bytes())
    return root, plan, control, digest


def _invoke(root, control_hash, task_id):
    return provider.call_api("", {"config": {"batch_dir": str(root), "batch_control_sha256": control_hash}},
                             {"vars": {"task_id": task_id}})


@pytest.mark.bfcl_evidence
def test_five_children_have_independent_actual_admission_and_empty_ledgers(batch):
    root, plan, control, control_hash = batch
    assert len(control["routes"]) == len(set(plan["task_routes"].values())) == 5
    bindings, ledgers = [], []
    for task_id in control["task_ids"]:
        child, state = provider.route_child(root, control, task_id)
        assert state["case_ids"] == [task_id] and state["test_only"] is False
        assert state["max_api_calls"] is None and state["profile"]["reasoning_effort"] == "none"
        assert state["source-manifest.json_sha256"] == control["source_manifest_sha256"]
        bindings.append(state["dataset_binding"])
        ledger = child / "results/http-budget.json"
        ledgers.append(ledger)
        assert json.loads(ledger.read_bytes())["calls"] == []
        assert len(json.loads((child / "private-cases.json").read_bytes())) == 1
    assert len(set(bindings)) == len(set(ledgers)) == 5
    assert provider.read_control(root, control_hash) == control
    assert not (root / "execution-control.json").exists()  # No shared backend campaign.


@pytest.mark.parametrize("concurrency", [1, 5])
@pytest.mark.bfcl_evidence
def test_whole_task_routing_and_all_scheduling_parameters_agree(concurrency, tmp_path):
    require_corpus()
    plan = runner.preflight(concurrency)
    config = runner.build_config(plan, tmp_path, "f" * 64)
    assert len(config["tests"]) == 5 and len(plan["task_routes"]) == 5
    assert config["providers"][0]["config"]["workers"] == concurrency
    assert config["evaluateOptions"] == {"maxConcurrency": concurrency, "repeat": 1, "showProgressBar": False}
    assert runner._probe.cli_command(tmp_path, concurrency)[-2:] == ["--max-concurrency", str(concurrency)]
    assert config["defaultTest"]["assert"][0]["value"] == "file://no_thinking_assertion.py"
    for test in config["tests"]:
        task_id = test["vars"]["task_id"]
        assert test["metadata"]["campaign_dir"] == (tmp_path / plan["task_routes"][task_id]).as_posix()
    assert all(group["api_call_cap"] is None for group in plan["groups"])


@pytest.mark.bfcl_evidence
def test_unknown_task_and_route_escape_reject_before_backend(batch):
    root, _plan, control, control_hash = batch
    with pytest.raises(provider.BatchDispatchError, match="outside_frozen_batch"):
        provider.route_child(root, control, "../../private")
    bad = deepcopy(control)
    task_id = control["task_ids"][0]
    bad["routes"][task_id]["relative_path"] = "../../outside"
    with pytest.raises(provider.BatchDispatchError, match="outside_frozen_scope"):
        provider.route_child(root, bad, task_id)
    payload = json.loads((root / "batch-control.json").read_bytes())
    payload["model"] = "changed"
    _write(root / "batch-control.json", payload)
    response = _invoke(root, control_hash, task_id)
    assert response["metadata"]["probe_error_kind"] == "batch_control_identity_changed"
    assert list((root / "callback-intents").iterdir()) == []


@pytest.mark.bfcl_evidence
def test_completed_or_unknown_callback_is_not_replayed(batch, monkeypatch):
    root, _plan, control, control_hash = batch
    calls = []

    def existing(_prompt, options, context):
        calls.append((options["config"]["campaign_dir"], context["vars"]["task_id"]))
        return {"output": "original-provider-result"}

    monkeypatch.setattr(provider._provider, "call_api", existing)
    task_id = control["task_ids"][0]
    assert _invoke(root, control_hash, task_id)["output"] == "original-provider-result"
    assert calls == [(str(root / control["routes"][task_id]["relative_path"]), task_id)]
    duplicate = _invoke(root, control_hash, task_id)
    assert duplicate["metadata"]["probe_error_kind"] == "duplicate_callback_refused"
    assert len(calls) == len(list((root / "callback-intents").glob("*.json"))) == 1
    assert len(list((root / "promptfoo/dispatches").glob("*.finished.json"))) == 1


@pytest.mark.bfcl_evidence
def test_unknown_child_outcome_halts_later_unstarted_tasks(batch, monkeypatch):
    root, _plan, control, control_hash = batch
    calls = []

    def unknown(_prompt, options, context):
        calls.append(context["vars"]["task_id"])
        child = Path(options["config"]["campaign_dir"])
        state = json.loads((child / "execution-control.json").read_bytes())
        state["fatal"] = "unknown_provider_request_outcome"
        _write(child / "execution-control.json", state)
        return {"output": "existing-provider-null-result"}

    monkeypatch.setattr(provider._provider, "call_api", unknown)
    first, second = control["task_ids"][:2]
    _invoke(root, control_hash, first)
    halt = json.loads((root / "batch-halt.json").read_bytes())
    assert halt["reason"] == "unknown_provider_request_outcome"
    response = _invoke(root, control_hash, second)
    assert response["metadata"]["probe_error_kind"] == "batch_halted_before_dispatch"
    assert calls == [first]
    second_child = root / control["routes"][second]["relative_path"]
    assert json.loads((second_child / "results/http-budget.json").read_bytes())["calls"] == []
    assert not list((second_child / "results/intents").glob("*.json"))


@pytest.mark.bfcl_evidence
def test_unclassified_callback_exception_is_sanitized_and_halts_batch(batch, monkeypatch):
    root, _plan, control, control_hash = batch

    def unknown(*_args):
        raise RuntimeError("credential-private-secret")

    monkeypatch.setattr(provider._provider, "call_api", unknown)
    response = _invoke(root, control_hash, control["task_ids"][0])
    assert response["metadata"]["probe_error_kind"] == "provider_callback_exception"
    assert "secret" not in json.dumps(response)
    assert (root / "batch-halt.json").exists()
    response = _invoke(root, control_hash, control["task_ids"][1])
    assert response["metadata"]["probe_error_kind"] == "batch_halted_before_dispatch"


@pytest.mark.bfcl_evidence
def test_aggregation_keeps_model_failures_separate_from_infrastructure(batch):
    root, plan, control, control_hash = batch
    native = []
    for index, task_id in enumerate(control["task_ids"]):
        child, state = provider.route_child(root, control, task_id)
        passed = index == 0
        count = plan["groups"][index]["user_turns"]
        row = {"id": task_id, "result": "pass" if passed else "fail", "model_api_calls": 1,
            "protocol_result": "pass", "scoring_verification": {"valid": passed}, "termination_kind": "completed",
            "usage": [{"input_tokens": 20, "output_tokens": 5, "reasoning_tokens": 0,
                       "cache_hit_tokens": 10, "cache_miss_tokens": 10}],
            "session_id": f"unique-session-{index}", "runs": [{"runtime_status": "completed"}] * count}
        key = provider.digest(task_id.encode())[:24]
        _write(child / "results/receipts" / (key + ".json"), {"dataset_binding": state["dataset_binding"], "result": row})
        _write(child / "results/intents" / (key + ".json"), {"task_id": task_id, "automatic_replay": False})
        ledger = json.loads((child / "results/http-budget.json").read_bytes())
        ledger["calls"] = [{"case_id": task_id, "budget_group": task_id, "started_at": 100 + index / 100,
            "status": "response_headers_received", "status_code": 200, "thinking_disabled_verified": True,
            "wire_model": "deepseek-flash", "wire_reasoning_effort": "none", "wire_max_output_tokens": 8192}]
        _write(child / "results/http-budget.json", ledger)
        telemetry = {"task_id": task_id, "pid": index + 1, "started_at": 99, "finished_at": 101,
            "outcome": "backend_returned", "probe_error_kind": None, "error_type": None, "http_attempts_for_task": 1}
        _write(root / "promptfoo/dispatches" / (key + ".started.json"), telemetry)
        _write(root / "promptfoo/dispatches" / (key + ".finished.json"), telemetry)
        compact = {"id": task_id, "result": row["result"], "main_score": float(passed),
            "model": "deepseek-flash", "reasoning_effort": "none", "api_count_limit": None,
            "backend_result_sha256": runner._helpers.compact_digest(row), "model_api_calls": 1, "judge_api_calls": 0}
        native.append({"vars": {"task_id": task_id}, "provider": {"label": runner._base.PROVIDER_LABEL},
            "success": passed, "response": {"output": json.dumps(compact)},
            "gradingResult": {"componentResults": [{"reason": "Live BFCL official complete-task result: " + row["result"]}]}})
    report = runner.collect_batch(root, plan, {"results": {"results": native, "stats": {"errors": 0}}}, control_hash)
    assert report["evidence_errors"] == []
    assert report["formal_task_results"] == {"pass": 1, "fail": 4}
    assert report["all_five_tasks_executed"] is True and report["complete_runtime_tasks"] == 5
    assert report["independent_session_count"] == report["peak_overlapping_tasks_with_http"] == 5
    assert report["model_api_calls"] == report["wire_none_verified_calls"] == 5
    assert report["known_usage_totals"]["input_tokens"] == 100
    assert report["unknown_core_usage_calls"] == report["unknown_reasoning_usage_calls"] == 0
    assert report["all_known_reasoning_tokens_zero"] is True
    assert report["capability_score"] is None and report["global_halt_recorded"] is False


def test_default_preflight_and_started_batch_refusal(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "preflight", lambda *_: {"fixture_only": True})
    root = tmp_path / "offline"
    assert runner.main(["--campaign-dir", str(root)]) == 0
    assert {path.name for path in root.iterdir()} == {"campaign-plan.json"}
    marker = root / "execution-started.json"
    marker.write_text("original")
    with pytest.raises(SystemExit) as error:
        runner.main(["--campaign-dir", str(root), "--execute"])
    assert error.value.code == 2 and marker.read_text() == "original"
    assert not (root / "children").exists()
