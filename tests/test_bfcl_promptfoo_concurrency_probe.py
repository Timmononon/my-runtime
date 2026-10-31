"""Offline scenarios for a promptfoo-only, unchanged-backend concurrency probe."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from bfcl_test_prerequisites import require_corpus

ADAPTER = Path(__file__).resolve().parents[1] / "evals/bfcl_multiturn/promptfoo"


def _module(name):
    spec = importlib.util.spec_from_file_location("test_concurrency_probe_" + name, ADAPTER / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner, provider = (_module(name) for name in ("run_concurrency_probe", "probe_provider"))


@pytest.mark.bfcl_evidence
def test_selection_uses_first_five_development_tasks_and_preflight_is_offline(tmp_path):
    require_corpus()
    plan = runner.preflight()
    ids = [group["group"] for group in plan["groups"]]
    assert ids == ["multi_turn_base_151", "multi_turn_miss_func_54", "multi_turn_miss_param_107",
                   "multi_turn_long_context_29", "multi_turn_miss_func_178"]
    development = set((runner.FROZEN / "development-ids.txt").read_text().splitlines())
    assert set(ids) <= development
    assert plan["planned_entries"] == 5
    assert plan["authorized_unlimited_api"] is True and plan["authorized_api_call_limit"] is None
    assert all(group["api_call_cap"] is None for group in plan["groups"])
    assert plan["reasoning_effort"] == "none" and plan["max_output_tokens"] == 8192
    output = tmp_path / "preflight"
    assert runner.main(["--campaign-dir", str(output)]) == 0
    assert {path.name for path in output.iterdir()} == {"campaign-plan.json"}


@pytest.mark.parametrize("concurrency", [1, 5])
@pytest.mark.bfcl_evidence
def test_three_concurrency_settings_agree_and_whole_tasks_are_not_repeated(concurrency, tmp_path):
    require_corpus()
    plan = runner.preflight(concurrency)
    config = runner.build_config(plan, tmp_path)
    assert len(config["tests"]) == 5
    assert config["providers"][0]["config"]["workers"] == concurrency
    assert config["evaluateOptions"] == {"maxConcurrency": concurrency, "repeat": 1, "showProgressBar": False}
    assert config["providers"][0]["id"].endswith("probe_provider.py")
    assert config["defaultTest"]["assert"][0]["value"] == "file://no_thinking_assertion.py"
    command = runner.cli_command(tmp_path, concurrency)
    assert command[-2:] == ["--max-concurrency", str(concurrency)]
    assert {"--no-cache", "--no-share", "--no-write"} <= set(command)


@pytest.mark.parametrize(("error", "kind"), [
    (TimeoutError("Lock busy: private-path-secret/campaign.lock"), "backend_campaign_lock_busy"),
    (ValueError("Only the next serial promptfoo task may begin"), "backend_serial_order_rejected"),
    (RuntimeError("credential-private-secret"), "unclassified_provider_exception"),
])
def test_safe_exception_observation_has_zero_http_and_cannot_replay(error, kind, monkeypatch, tmp_path):
    output = tmp_path / "promptfoo/dispatches"
    output.mkdir(parents=True)
    (tmp_path / "results").mkdir()
    (tmp_path / "results/http-budget.json").write_text(json.dumps({"calls": []}))
    calls = []

    def failed(*args):
        calls.append(args)
        raise error

    monkeypatch.setattr(provider._provider, "call_api", failed)
    options = {"config": {"campaign_dir": str(tmp_path), "probe_telemetry_dir": str(output)}}
    context = {"vars": {"task_id": "multi_turn_base_151"}}
    response = provider.call_api("", options, context)
    compact = json.loads(response["output"])
    assert compact["probe_error_kind"] == kind and compact["main_score"] is None
    finished = json.loads(next(output.glob("*.finished.json")).read_bytes())
    assert finished["http_attempts_for_task"] == 0
    assert finished["probe_error_kind"] == kind
    assert "secret" not in json.dumps([response, finished])
    duplicate = provider.call_api("", options, context)
    assert duplicate["metadata"]["probe_error_kind"] == "duplicate_callback_refused"
    assert len(calls) == 1
    assert len(list(output.glob("*.started.json"))) == len(list(output.glob("*.finished.json"))) == 1


def test_observed_lock_errors_are_valid_experiment_evidence_not_task_success(tmp_path):
    # Only callback accounting is under test; no frozen benchmark rows are needed.
    plan = {"groups": [{"group": f"multi_turn_base_{index}"} for index in range(5)]}
    dispatches = tmp_path / "dispatches"
    dispatches.mkdir()
    native, rows = [], []
    task_ids = [group["group"] for group in plan["groups"]]
    for index, task_id in enumerate(task_ids):
        row = {"id": task_id, "result": "pass" if index == 0 else "not_run", "model_api_calls": int(index == 0)}
        rows.append(row)
        telemetry = {"task_id": task_id, "pid": index + 100, "started_at": 100 + index / 100,
            "finished_at": 101 if index == 0 else 100.5,
            "outcome": "backend_returned" if index == 0 else "provider_exception",
            "probe_error_kind": None if index == 0 else "backend_campaign_lock_busy",
            "error_type": None if index == 0 else "TimeoutError"}
        (dispatches / f"{index}.started.json").write_text(json.dumps(telemetry))
        (dispatches / f"{index}.finished.json").write_text(json.dumps(telemetry))
        compact = {"id": task_id, "result": row["result"], "main_score": 1.0,
            "model": "deepseek-flash", "reasoning_effort": "none", "api_count_limit": None,
            "backend_result_sha256": runner._helpers.compact_digest(row), "model_api_calls": row["model_api_calls"],
            "judge_api_calls": 0}
        item = {"vars": {"task_id": task_id}, "provider": {"label": runner._base.PROVIDER_LABEL},
            "success": index == 0, "response": {"output": json.dumps(compact)},
            "gradingResult": {"componentResults": [{"reason": "Live BFCL official complete-task result: pass"}]}}
        if index:
            compact.update(result="probe_dispatch_error", main_score=None,
                probe_error_kind="backend_campaign_lock_busy", error_type="TimeoutError")
            item["response"]["output"] = json.dumps(compact)
            item["error"] = "PROBE backend_campaign_lock_busy; automatic replay disabled"
        native.append(item)
    payload = {"results": {"results": native, "stats": {"errors": 4}}}
    report = {"results": rows, "http_calls": [{"case_id": task_ids[0]}]}
    analysis = runner.analyze_native(payload, report, plan, tmp_path)
    assert analysis["evidence_errors"] == []
    assert len(analysis["observed_dispatch_errors"]) == 4
    assert analysis["peak_overlapping_provider_callbacks"] == 5
    assert analysis["tasks_with_http"] == analysis["backend_returned_task_count"] == 1
    assert analysis["all_five_tasks_executed"] is False and analysis["capability_score"] is None
    report["http_calls"].append({"case_id": task_ids[1]})
    assert "admission_rejection_has_http_attempts" in runner.analyze_native(payload, report, plan, tmp_path)["evidence_errors"]


def test_launcher_refuses_started_campaign_without_changing_it(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "preflight", lambda *_: {"fixture_only": True})
    marker = tmp_path / "execution-started.json"
    marker.write_text("original")
    with pytest.raises(SystemExit) as error:
        runner.main(["--campaign-dir", str(tmp_path), "--execute"])
    assert error.value.code == 2
    assert marker.read_text() == "original"
    assert {path.name for path in tmp_path.iterdir()} == {"execution-started.json"}
