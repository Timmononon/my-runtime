"""Offline scenarios for one shared campaign behind native promptfoo."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import time
import types
from contextlib import nullcontext
from pathlib import Path

import pytest
from bfcl_test_prerequisites import require_corpus
from test_bfcl_current import source_at

from aster.evaluation import bfcl_multiturn_concurrent as backend
from aster.evaluation.bfcl_current import capture_current_profile
from aster.evaluation.bfcl_v4_data import load_corpus

PROJECT = Path(__file__).resolve().parents[1]
HERE = PROJECT / "evals/bfcl_multiturn/promptfoo"


def _load(name):
    spec = importlib.util.spec_from_file_location("test_shared_" + name, HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner, provider = (_load(name) for name in ("run_concurrent", "concurrent_provider"))


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(runner._helpers.encoded(value))


def _snapshot(root):
    source = Path(backend.__file__)
    relative = source.relative_to(PROJECT)
    target = root / "as-run-source" / relative
    target.parent.mkdir(parents=True)
    target.write_bytes(source.read_bytes())
    _write(root / "source-manifest.json", {"files": [{"file": relative.as_posix(),
        "sha256": runner._helpers.digest(source.read_bytes())}]})


@pytest.fixture
def admitted(tmp_path):
    require_corpus()
    root = tmp_path / "campaign"
    root.mkdir()
    plan = runner.preflight()
    plan["campaign"] = root.name
    _write(root / "campaign-plan.json", plan)
    _snapshot(root)
    profile = capture_current_profile(source_at(tmp_path), provider_id="deepseek",
        model="deepseek-flash", reasoning_effort="none")
    by_id = {case["id"]: case for case in load_corpus(runner._helpers.CORPUS, tracks=list(runner._helpers.CATEGORIES))}
    cases = [by_id[group["group"]] for group in plan["groups"]]
    control = backend.initialize_campaign(cases, root, profile=profile, maximum_concurrency=5)
    (root / "promptfoo/dispatches").mkdir(parents=True)
    return root, plan, profile, control


@pytest.mark.parametrize("concurrency", [1, 5])
@pytest.mark.bfcl_evidence
def test_all_concurrency_settings_and_new_backend_identity_agree(concurrency, tmp_path):
    require_corpus()
    plan = runner.preflight(concurrency)
    config = runner.build_config(plan, tmp_path)
    assert plan["maximum_concurrency"] == plan["concurrency"] == concurrency
    assert plan["backend_mode"] == backend.MODE
    assert len(config["tests"]) == 5
    assert len({test["metadata"]["campaign_dir"] for test in config["tests"]}) == 1
    assert {group["category"] for group in plan["groups"]} == set(runner._helpers.CATEGORIES)
    assert config["providers"][0]["id"].endswith("/concurrent_provider.py")
    assert config["providers"][0]["config"]["workers"] == concurrency
    assert config["evaluateOptions"]["maxConcurrency"] == concurrency
    assert runner.cli_command(tmp_path, concurrency)[-2:] == ["--max-concurrency", str(concurrency)]
    assert config["defaultTest"]["assert"][0]["value"] == "file://no_thinking_assertion.py"
    assert plan["reasoning_effort"] == "none" and plan["max_output_tokens"] == 8192
    assert plan["authorized_api_call_limit"] is None


@pytest.mark.bfcl_evidence
def test_paid_admission_has_one_shared_control_and_empty_ledger(admitted):
    root, plan, _profile, control = admitted
    assert control["test_only"] is False and control["concurrent_tasks"]["maximum"] == 5
    assert control["case_ids"] == [group["group"] for group in plan["groups"]]
    assert len(json.loads((root / "private-cases.json").read_bytes())) == 5
    assert json.loads((root / "results/http-budget.json").read_bytes())["calls"] == []
    assert len(list(root.rglob("execution-control.json"))) == 1
    assert len(list(root.rglob("http-budget.json"))) == 1 and not (root / "children").exists()
    assert set(control["concurrent_tasks"]["states"].values()) == {"pending"}


@pytest.mark.bfcl_evidence
def test_launcher_reaches_new_paid_initialization_before_native_process_without_http(tmp_path, monkeypatch):
    require_corpus()
    import aster.paths

    source = source_at(tmp_path)
    root = tmp_path / "launch"
    monkeypatch.setattr(aster.paths, "resolve_data_dir", lambda: source)
    monkeypatch.setattr(runner._helpers, "snapshot_sources", _snapshot)
    called = []
    original = backend.initialize_campaign

    def initialize(*args, **kwargs):
        called.append(kwargs["maximum_concurrency"])
        return original(*args, **kwargs)

    class OfflineStop(RuntimeError):
        pass

    def stop_before_process(command, **kwargs):
        assert command[-2:] == ["--max-concurrency", "5"]
        config = json.loads((root / "promptfoo/promptfooconfig.json").read_bytes())
        assert config["providers"][0]["config"]["workers"] == 5
        assert kwargs["env"]["PROMPTFOO_PYTHON_WORKERS"] == "5"
        raise OfflineStop("Deliberate fixture stop before any process or HTTP")

    monkeypatch.setattr(backend, "initialize_campaign", initialize)
    monkeypatch.setattr(runner.subprocess, "run", stop_before_process)
    with pytest.raises(OfflineStop):
        runner.main(["--campaign-dir", str(root), "--execute"])
    assert called == [5]
    assert json.loads((root / "execution-control.json").read_bytes())["concurrent_tasks"]["mode"] == backend.MODE
    assert json.loads((root / "results/http-budget.json").read_bytes())["calls"] == []
    assert not list((root / "results/intents").glob("*.json"))


def _invoke(root, task_id):
    return provider.call_api("", {"config": {"campaign_dir": str(root)}}, {"vars": {"task_id": task_id}})


def test_provider_dispatches_new_backend_once_and_binds_receipt_projection(tmp_path, monkeypatch):
    root = tmp_path / "campaign"
    (root / "promptfoo/dispatches").mkdir(parents=True)
    monkeypatch.setitem(sys.modules, "sitecustomize", types.SimpleNamespace(authorized_backend=nullcontext))
    row = {"id": "multi_turn_base_151", "result": "pass", "backend_mode": backend.MODE,
        "model_api_calls": 1, "http_attempts": 1, "protocol_result": "pass",
        "scoring_verification": {"valid": True}, "usage": [{"input_tokens": 20, "output_tokens": 5}]}
    calls = []

    async def current(task_id, campaign):
        calls.append((task_id, campaign))
        return row

    monkeypatch.setattr(backend, "run_one_current_multiturn", current)
    response = _invoke(root, row["id"])
    compact = json.loads(response["output"])
    assert calls == [(row["id"], root.resolve())]
    assert compact["backend_result_sha256"] == runner._helpers.compact_digest(row)
    assert compact["main_score"] == 1.0 and compact["backend_mode"] == backend.MODE
    assert response["tokenUsage"] == {"prompt": 20, "completion": 5, "total": 25}
    with pytest.raises(FileExistsError):
        _invoke(root, row["id"])
    assert len(calls) == 1
    assert len(list((root / "promptfoo/dispatches").glob("*.finished.json"))) == 1


def test_provider_exception_is_sanitized_without_retry(tmp_path, monkeypatch):
    root = tmp_path / "campaign"
    (root / "promptfoo/dispatches").mkdir(parents=True)
    monkeypatch.setitem(sys.modules, "sitecustomize", types.SimpleNamespace(authorized_backend=nullcontext))
    calls = []

    async def broken(*args):
        calls.append(args)
        raise RuntimeError("PRIVATE_PROVIDER_HEADER_CREDENTIAL")

    monkeypatch.setattr(backend, "run_one_current_multiturn", broken)
    response = _invoke(root, "multi_turn_base_151")
    assert len(calls) == 1 and "PRIVATE_PROVIDER" not in json.dumps(response)
    assert json.loads(response["output"])["main_score"] is None
    finished = json.loads(next((root / "promptfoo/dispatches").glob("*.finished.json")).read_bytes())
    assert finished["outcome"] == "provider_exception" and finished["error_type"] == "RuntimeError"


def test_mock_unsupported_and_infrastructure_keep_null_main_score():
    unsupported = provider.render_response({"id": "multi_turn_base_0", "result": "unsupported", "model_api_calls": 0})
    assert json.loads(unsupported["output"])["main_score"] is None and "error" not in unsupported
    failed = provider.render_response({"id": "multi_turn_base_0", "result": "case_error", "model_api_calls": 0})
    assert json.loads(failed["output"])["main_score"] is None and failed["error"].startswith("EXCLUDED ")


def test_cancelled_callback_propagates_and_never_records_backend_success(tmp_path, monkeypatch):
    root = tmp_path / "campaign"
    (root / "promptfoo/dispatches").mkdir(parents=True)
    monkeypatch.setitem(sys.modules, "sitecustomize", types.SimpleNamespace(authorized_backend=nullcontext))

    async def interrupted(*_args):
        raise asyncio.CancelledError

    monkeypatch.setattr(backend, "run_one_current_multiturn", interrupted)
    with pytest.raises(asyncio.CancelledError):
        _invoke(root, "multi_turn_base_151")
    finished = json.loads(next((root / "promptfoo/dispatches").glob("*.finished.json")).read_bytes())
    assert finished["outcome"] == "provider_exception" and finished["error_type"] == "CancelledError"
    assert finished["http_attempts_for_task"] is None


@pytest.mark.bfcl_evidence
def test_collect_separates_formal_failures_from_framework_errors_in_one_campaign(admitted):
    root, plan, _profile, control = admitted
    rows, native, calls = [], [], []
    output = root / "promptfoo"
    for index, group in enumerate(plan["groups"]):
        task_id, passed = group["group"], index in {0, 3}
        row = {"id": task_id, "result": "pass" if passed else "fail", "backend_mode": backend.MODE,
            "model_api_calls": 1, "http_attempts": 1, "protocol_result": "pass",
            "scoring_verification": {"valid": passed}, "termination_kind": "completed",
            "session_id": f"fixture-session-{index}",
            "runs": [{"runtime_status": "completed"}] * group["user_turns"],
            "usage": [{"input_tokens": 20, "output_tokens": 5, "reasoning_tokens": 0,
                "cache_hit_tokens": 10, "cache_miss_tokens": 10}]}
        rows.append(row)
        key = runner._helpers.digest(task_id.encode())[:24]
        _write(root / "results/receipts" / (key + ".json"), {"dataset_binding": control["dataset_binding"], "result": row})
        _write(root / "results/intents" / (key + ".json"), {"task_id": task_id})
        telemetry = {"task_id": task_id, "pid": index + 1, "campaign_dir": str(root.resolve()),
            "backend_mode": backend.MODE, "started_at": 99, "finished_at": 102,
            "outcome": "backend_returned", "error_type": None, "http_attempts_for_task": 1}
        for suffix in (".started.json", ".finished.json"):
            _write(output / "dispatches" / (key + suffix), telemetry)
        calls.append({"case_id": task_id, "started_at": 100 + index / 100,
            "status": "response_headers_received", "status_code": 200, "thinking_disabled_verified": True,
            "wire_model": "deepseek-flash", "wire_reasoning_effort": "none", "wire_max_output_tokens": 8192})
        native.append({"vars": {"task_id": task_id}, "provider": {"label": runner.PROVIDER_LABEL},
            "success": passed, "response": provider.render_response(row), "gradingResult": {"componentResults": [
                {"reason": "Live BFCL official complete-task result: " + row["result"]}]}})
    control["concurrent_tasks"].update(finalized=True, states=dict.fromkeys(control["case_ids"], "completed"))
    _write(root / "execution-control.json", control)
    _write(root / "results/results.json", {"results": rows, "dataset_binding": control["dataset_binding"],
        "model_api_calls": 5, "max_api_calls": None})
    ledger = json.loads((root / "results/http-budget.json").read_bytes())
    ledger["calls"] = calls
    _write(root / "results/http-budget.json", ledger)
    payload = {"results": {"results": native, "stats": {"errors": 0}}}
    report = runner.collect_campaign(root, plan, payload, output)
    assert report["evidence_errors"] == [] and report["same_campaign"] is True
    assert report["formal_task_results"] == {"pass": 2, "fail": 3}
    assert report["failure_classifications"] == {"pass": 2, "task_failure": 3}
    assert report["subset_success_rate"] == 0.4 and report["peak_overlapping_tasks_with_http"] == 5
    assert report["independent_session_count"] == report["complete_runtime_tasks"] == 5
    assert report["completed_user_turns"] == report["defined_user_turns"] == 22
    assert report["wire_none_verified_calls"] == report["model_api_calls"] == 5
    assert report["known_usage_totals"]["input_tokens"] == 100
    assert report["unknown_core_usage_calls"] == report["unknown_reasoning_usage_calls"] == 0
    native[0]["response"]["output"] = "{}"
    assert runner.collect_campaign(root, plan, payload, output)["evidence_errors"]


def test_preflight_is_offline_and_started_campaign_never_replays(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "preflight", lambda *_: {"fixture_only": True})
    root = tmp_path / "preflight"
    assert runner.main(["--campaign-dir", str(root)]) == 0
    assert {path.name for path in root.iterdir()} == {"campaign-plan.json"}
    marker = root / "execution-started.json"
    marker.write_text(str(time.time()), encoding="utf-8")
    saved = marker.read_bytes()
    with pytest.raises(SystemExit) as error:
        runner.main(["--campaign-dir", str(root), "--execute"])
    assert error.value.code == 2 and marker.read_bytes() == saved and not (root / "results").exists()
