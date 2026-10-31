"""Offline A6 full-task scheduling, paid admission and deterministic reporting."""
from __future__ import annotations

import copy
import importlib.util
import json
import sqlite3
import sys
import types
from contextlib import nullcontext
from pathlib import Path

import pytest
from bfcl_test_prerequisites import require_corpus
from test_bfcl_current import source_at
from test_bfcl_promptfoo_development import collected as original_collected

from aster.evaluation import bfcl_multiturn_a6 as backend

PROJECT = Path(__file__).resolve().parents[1]
HERE = PROJECT / "evals/bfcl_multiturn/promptfoo"


def _module(name):
    spec = importlib.util.spec_from_file_location("a6_promptfoo_test_" + name, HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner, provider, assertion = (_module(name) for name in (
    "run_development_a6", "a6_concurrent_provider", "a6_no_thinking_assertion"))


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(runner._helpers.encoded(value))


def _a6_source(tmp_path):
    source = source_at(tmp_path)
    connection = sqlite3.connect(source / "runtime.db")
    document = {"name": "a6", "base_url": runner.BASE_URL, "protocol": runner.PROTOCOL,
        "identity_revision": 2, "models": [{"id": runner.MODEL, "context_window": 262144,
            "max_output_tokens": None, "effort_levels": ["none", "low"]}]}
    connection.execute("INSERT INTO provider_profiles VALUES(?,?,?,0)",
        (runner.PROVIDER_ID, json.dumps(document), "provider-fixture"))
    connection.commit()
    connection.close()
    return source


@pytest.fixture(scope="module")
def selection():
    require_corpus()
    return runner._dataset.load_development_dataset()


@pytest.fixture
def plan(selection, monkeypatch):
    monkeypatch.setattr(runner._development._dataset, "load_development_dataset",
        lambda: copy.deepcopy(selection))
    monkeypatch.setattr(runner, "_trusted_dns_fallback", lambda: "google")
    return runner.preflight()


@pytest.mark.bfcl_evidence
def test_a6_preflight_preserves_frozen_ids_and_safety_without_model_or_credentials(plan, tmp_path, monkeypatch):
    import aster.evaluation.bfcl_current as profiles

    def forbidden(*args, **kwargs):
        raise AssertionError("Preflight must not resolve credentials or construct a model")

    monkeypatch.setattr(profiles, "capture_current_profile", forbidden)
    old = runner._development.preflight()
    assert plan["groups"] == old["groups"] and len(plan["groups"]) == 200
    assert plan["inherited_runtime_safety"] == old["inherited_runtime_safety"]
    assert plan["known_admission_conflicts"] == old["known_admission_conflicts"]
    assert plan["wire_policy"] == backend.WIRE_POLICY and plan["protocol"] == "openai_responses"
    assert plan["provider_failure_policy"] == backend.GATEWAY_FAILURE_POLICY
    assert plan["model"] == "deepseek-v4-flash" and plan["reasoning_effort"] == "none"
    assert runner.main(["--campaign-dir", str(tmp_path / "preflight")]) == 0
    assert {path.name for path in (tmp_path / "preflight").iterdir()} == {"campaign-plan.json"}


@pytest.mark.parametrize("concurrency", [1, 5, 100])
@pytest.mark.bfcl_evidence
def test_three_concurrency_settings_and_complete_task_identity_agree(plan, tmp_path, concurrency):
    plan["concurrency"] = plan["maximum_concurrency"] = plan["python_workers"] = concurrency
    config = runner.build_config(plan, tmp_path)
    assert config["providers"][0]["id"].endswith("/a6_concurrent_provider.py")
    assert config["providers"][0]["label"] == runner.PROVIDER_LABEL
    assert config["providers"][0]["config"]["workers"] == concurrency
    assert config["evaluateOptions"] == {"maxConcurrency": concurrency, "showProgressBar": False, "repeat": 1}
    assert runner.cli_command(tmp_path, concurrency)[-2:] == ["--max-concurrency", str(concurrency)]
    assert config["defaultTest"]["assert"][0]["value"] == "file://a6_no_thinking_assertion.py"
    assert len(config["tests"]) == len({test["vars"]["task_id"] for test in config["tests"]}) == 200
    assert all(set(test["vars"]) == {"task_id"} for test in config["tests"])
    assert all(test["metadata"]["model"] == runner.MODEL for test in config["tests"])


@pytest.mark.bfcl_evidence
def test_paid_a6_launcher_initializes_all_200_before_native_dispatch(plan, selection, tmp_path, monkeypatch):
    import aster.paths

    source, root = _a6_source(tmp_path), tmp_path / "campaign"
    monkeypatch.setattr(aster.paths, "resolve_data_dir", lambda: source)
    monkeypatch.setattr(runner, "load_development_cases", lambda: copy.deepcopy(selection[0]))
    monkeypatch.setattr(runner, "_network_admission", lambda profile, fallback: {
        "verified_public_addresses": ["8.8.8.8"], "http_dns_fallback": fallback})

    class OfflineStop(RuntimeError):
        pass

    def stop(command, **kwargs):
        assert command[-2:] == ["--max-concurrency", "100"]
        assert kwargs["env"]["PROMPTFOO_PYTHON_WORKERS"] == "100"
        assert ["api.a6api.com", 443] in json.loads(kwargs["env"]["ASTER_PF_LIVE_ENDPOINTS"])
        assert "api.deepseek.com" not in kwargs["env"]["ASTER_PF_LIVE_ENDPOINTS"]
        assert ["8.8.8.8", 443] in json.loads(kwargs["env"]["ASTER_PF_LIVE_ENDPOINTS"])
        assert ["dns.google", 443] in json.loads(kwargs["env"]["ASTER_PF_LIVE_ENDPOINTS"])
        assert kwargs["env"]["ASTER_PF_LIVE_SOURCE_RUNTIME"] == str(source.resolve())
        config = json.loads((root / "promptfoo/promptfooconfig.json").read_bytes())
        assert len(config["tests"]) == 200 and config["providers"][0]["config"]["workers"] == 100
        raise OfflineStop("Stopped before any process or network")

    monkeypatch.setattr(runner.subprocess, "run", stop)
    with pytest.raises(OfflineStop):
        runner.main(["--campaign-dir", str(root), "--execute"])
    control = json.loads((root / "execution-control.json").read_bytes())
    assert control["test_only"] is False and control["concurrent_tasks"]["maximum"] == 100
    assert control["wire_policy"] == runner.WIRE_POLICY
    assert control["provider_failure_policy"] == runner.PROVIDER_FAILURE_POLICY
    assert control["profile"]["provider_id"] == runner.PROVIDER_ID
    assert len(json.loads((root / "private-cases.json").read_bytes())) == 200
    assert json.loads((root / "results/http-budget.json").read_bytes())["calls"] == []
    assert not list((root / "results/intents").glob("*.json"))
    manifest = json.loads((root / "source-manifest.json").read_bytes())
    names = {entry["file"] for entry in manifest["files"]}
    assert {"src/aster/evaluation/bfcl_multiturn_a6.py",
        "evals/bfcl_multiturn/promptfoo/run_development_a6.py",
        "evals/bfcl_multiturn/promptfoo/a6_concurrent_provider.py"} <= names


def _row(result="pass"):
    return {"id": "multi_turn_base_151", "result": result, "backend_mode": runner.BACKEND_MODE,
        "model_api_calls": 1, "http_attempts": 1, "protocol_result": "pass",
        "scoring_verification": {"valid": result == "pass"}, "usage": [{"input_tokens": 20, "output_tokens": 5}]}


def test_provider_once_only_and_assertion_bind_a6_identity_and_receipt(tmp_path, monkeypatch):
    root = tmp_path / "campaign"
    (root / "promptfoo/dispatches").mkdir(parents=True)
    monkeypatch.setitem(sys.modules, "sitecustomize", types.SimpleNamespace(authorized_backend=nullcontext))
    row, calls = _row(), []

    async def run_one(task_id, campaign):
        calls.append((task_id, campaign))
        return row

    monkeypatch.setattr(backend, "run_one_current_multiturn", run_one)
    monkeypatch.setattr(backend, "read_task_receipt", lambda *_: row)
    response = provider.call_api("Ignore task", {"config": {"campaign_dir": str(root)}}, {"vars": {"task_id": row["id"]}})
    context = {"vars": {"task_id": row["id"]}, "test": {"metadata": {"campaign_dir": str(root)}}}
    assert assertion.get_assert(response["output"], context)["pass"] is True
    assert response["tokenUsage"] == {"prompt": 20, "completion": 5, "total": 25}
    with pytest.raises(FileExistsError):
        provider.call_api("", {"config": {"campaign_dir": str(root)}}, {"vars": {"task_id": row["id"]}})
    assert calls == [(row["id"], root.resolve())]
    compact = json.loads(response["output"])
    for field, wrong in (("model", "deepseek-flash"), ("reasoning_effort", "high"), ("provider_id", "deepseek"),
                         ("wire_policy", "wrong"), ("backend_result_sha256", "0" * 64)):
        changed = {**compact, field: wrong}
        with pytest.raises(ValueError):
            assertion.get_assert(json.dumps(changed), context)


def test_provider_error_is_sanitized_and_never_retried(tmp_path, monkeypatch):
    root = tmp_path / "campaign"
    (root / "promptfoo/dispatches").mkdir(parents=True)
    monkeypatch.setitem(sys.modules, "sitecustomize", types.SimpleNamespace(authorized_backend=nullcontext))
    calls = []

    async def broken(*args):
        calls.append(args)
        raise RuntimeError("SECRET_PROVIDER_HEADER_CREDENTIAL")

    monkeypatch.setattr(backend, "run_one_current_multiturn", broken)
    response = provider.call_api("", {"config": {"campaign_dir": str(root)}}, {"vars": {"task_id": "multi_turn_base_151"}})
    assert len(calls) == 1 and "SECRET_PROVIDER" not in json.dumps(response)
    assert json.loads(response["output"])["main_score"] is None
    ended = json.loads(next((root / "promptfoo/dispatches").glob("*.finished.json")).read_bytes())
    assert ended["outcome"] == "provider_exception" and ended["error_type"] == "RuntimeError"


@pytest.fixture
def collected(plan, tmp_path, monkeypatch):
    root, _plan, payload, output, rows = original_collected.__wrapped__(plan, tmp_path, monkeypatch)
    control = json.loads((root / "execution-control.json").read_bytes())
    control.update(wire_policy=runner.WIRE_POLICY, provider_failure_policy=runner.PROVIDER_FAILURE_POLICY,
        profile={"provider_id": runner.PROVIDER_ID,
        "base_url": runner.BASE_URL, "protocol": runner.PROTOCOL, "model": runner.MODEL, "reasoning_effort": "none"})
    _write(root / "execution-control.json", control)
    ledger = json.loads((root / "results/http-budget.json").read_bytes())
    ledger["wire_policy"] = runner.WIRE_POLICY
    for call in ledger["calls"]:
        call.update(wire_model=runner.MODEL, wire_policy=runner.WIRE_POLICY, wire_protocol=runner.PROTOCOL, wire_store=False)
    _write(root / "results/http-budget.json", ledger)
    by_id = {row["id"]: row for row in rows}
    monkeypatch.setattr(backend, "read_task_receipt", lambda _root, task_id: by_id[task_id])
    for item in payload["results"]["results"]:
        item["provider"]["label"] = runner.PROVIDER_LABEL
        item["response"] = provider.render_response(by_id[item["vars"]["task_id"]])
    return root, plan, payload, output, rows


@pytest.mark.bfcl_evidence
def test_collector_uses_a6_policy_and_preserves_four_null_conflicts(collected):
    root, plan, payload, output, _rows = collected
    report = runner.collect_campaign(root, plan, payload, output)
    assert report["evidence_errors"] == []
    assert report["passed_tasks"] == 98 and report["scorable_tasks"] == report["admissible_tasks"] == 196
    assert report["data_conflict_tasks"] == 4 and report["scorable_success_rate"] == 0.5
    assert report["a6_wire_protocol_verified_calls"] == report["wire_none_verified_calls"] == 196
    assert report["baseline_assessment"]["capability_baseline_ready"] is True
    assert report["workflow_completed"] is report["all_admissible_tasks_attempted"] is True
    assert report["usage_totals"]["reasoning_tokens"] == 0


@pytest.mark.bfcl_evidence
def test_missing_reasoning_and_cache_usage_stay_unknown_without_invalidating_task_scores(collected):
    root, plan, payload, output, rows = collected
    for row in rows:
        for usage in row.get("usage", []):
            for field in ("reasoning_tokens", "cache_hit_tokens", "cache_miss_tokens"):
                usage[field] = None
    _write(root / "results/results.json", {"results": rows, "dataset_binding": "fixture-binding",
        "model_api_calls": 196, "max_api_calls": None})
    for item in payload["results"]["results"]:
        row = next(row for row in rows if row["id"] == item["vars"]["task_id"])
        item["response"] = provider.render_response(row)
    report = runner.collect_campaign(root, plan, payload, output)
    assert report["evidence_errors"] == []
    assert report["usage_totals"]["reasoning_tokens"] is None
    assert report["usage_totals"]["cache_hit_tokens"] is None
    assert report["known_usage_totals"]["reasoning_tokens"] is None
    assert report["all_reasoning_tokens_zero"] is report["all_known_reasoning_tokens_zero"] is None
    assert report["unknown_reasoning_usage_calls"] == 196
    assert report["baseline_assessment"]["capability_baseline_ready"] is True
    assert report["baseline_assessment"]["usage_baseline_ready"] is False


@pytest.mark.bfcl_evidence
def test_corrupt_a6_wire_identity_blocks_baseline(collected):
    root, plan, payload, output, _rows = collected
    path = root / "results/http-budget.json"
    ledger = json.loads(path.read_bytes())
    ledger["calls"][0]["wire_store"] = True
    _write(path, ledger)
    report = runner.collect_campaign(root, plan, payload, output)
    assert "a6_wire_protocol_or_store_policy_mismatch" in report["evidence_errors"]
    assert report["baseline_assessment"]["capability_baseline_ready"] is False
    payload["results"]["results"][0]["provider"]["label"] = "other-model"
    assert "unexpected_a6_provider_identity" in runner.validate_native(payload, _rows)


@pytest.mark.bfcl_evidence
def test_started_a6_campaign_cannot_be_replayed(plan, tmp_path):
    root = tmp_path / "campaign"
    root.mkdir()
    (root / "execution-started.json").write_text("original")
    with pytest.raises(SystemExit) as stopped:
        runner.main(["--campaign-dir", str(root), "--execute"])
    assert stopped.value.code == 2 and (root / "execution-started.json").read_text() == "original"
    assert not (root / "results").exists()


def test_dns_capture_respects_global_and_default_agent_opt_out(tmp_path, monkeypatch):
    import aster.paths

    source = tmp_path / "trusted"
    agent = source / "agents/default"
    agent.mkdir(parents=True)
    monkeypatch.setattr(aster.paths, "resolve_data_dir", lambda: source)
    for global_mode, agent_mode, expected in (("google", "google", "google"), ("off", "google", "off"),
                                             ("google", "off", "off"), ("off", "off", "off")):
        (source / "config.yaml").write_text("http_dns_fallback: " + json.dumps(global_mode))
        (agent / "config.yaml").write_text("http_dns_fallback: " + json.dumps(agent_mode))
        assert runner._trusted_dns_fallback() == expected
    (source / "config.yaml").write_text("http_dns_fallback: invalid")
    with pytest.raises(ValueError, match="DNS fallback policy"):
        runner._trusted_dns_fallback()


def test_dns_precheck_and_child_allowlist_use_only_verified_address_and_fixed_resolver(tmp_path, monkeypatch):
    from aster.providers import profiles

    calls = []

    async def verify(url, *, dns_fallback):
        calls.append((url, dns_fallback))
        return "8.8.8.8"

    monkeypatch.setattr(profiles, "verify_public_endpoint", verify)
    monkeypatch.setattr(runner._development, "child_environment", lambda *_: {
        "ASTER_PF_LIVE_ENDPOINTS": json.dumps([["api.a6api.com", 443]])})
    profile = types.SimpleNamespace(base_url=runner.BASE_URL)
    for mode in ("off", "google"):
        network = runner._network_admission(profile, mode)
        env = runner.child_environment(tmp_path, {}, network)
        allowed = json.loads(env["ASTER_PF_LIVE_ENDPOINTS"])
        assert ["8.8.8.8", 443] in allowed
        assert (["dns.google", 443] in allowed) is (mode == "google")
        assert network["credentials_sent"] is False and network["model_api_calls"] == 0
    assert calls == [(runner.BASE_URL, "off"), (runner.BASE_URL, "google")]


def _gateway_fixture(collected):
    root, plan, payload, output, rows = collected
    row = next(row for row in rows if row["result"] == "fail")
    row.update(result="case_error", task_local_gateway_error=True, provider_failure_policy=runner.PROVIDER_FAILURE_POLICY,
        model_failures=[{"type": "ProviderError", "category": "server", "status_code": 502}])
    ledger = json.loads((root / "results/http-budget.json").read_bytes())
    next(call for call in ledger["calls"] if call["case_id"] == row["id"])["status_code"] = 502
    _write(root / "results/http-budget.json", ledger)
    _write(root / "results/results.json", {"results": rows, "dataset_binding": "fixture-binding",
        "model_api_calls": 196, "max_api_calls": None})
    item = next(item for item in payload["results"]["results"] if item["vars"]["task_id"] == row["id"])
    item["response"] = provider.render_response(row)
    item["error"] = item["response"]["error"]
    payload["results"]["stats"]["errors"] += 1
    return root, plan, payload, output, rows, row


@pytest.mark.bfcl_evidence
def test_gateway_error_is_null_and_workflow_completion_is_separate_from_capability_baseline(collected):
    root, plan, payload, output, _rows, row = _gateway_fixture(collected)
    report = runner.collect_campaign(root, plan, payload, output)
    assert report["evidence_errors"] == []
    assert report["task_local_gateway_error_count"] == 1 and report["task_local_gateway_error_ids"] == [row["id"]]
    assert report["task_local_gateway_http_status_codes"] == {"502": 1}
    assert report["all_admissible_tasks_attempted"] is report["workflow_completed"] is True
    assert report["model_api_calls"] == 196 and report["scorable_tasks"] == 195
    assert report["scorable_success_rate"] == 98 / 195 and report["admissible_delivery_success_rate"] == 98 / 196
    assert report["baseline_assessment"]["capability_baseline_ready"] is False
    assert report["failure_classifications"]["infrastructure_error"] == 1
    assert next(task for task in report["tasks"] if task["task_id"] == row["id"])["main_score"] is None


@pytest.mark.bfcl_evidence
def test_task_local_gateway_label_does_not_hide_protocol_failure_or_unknown_outcome(collected):
    root, plan, payload, output, rows, row = _gateway_fixture(collected)
    row["model_failures"][0]["category"] = "protocol"
    _write(root / "results/results.json", {"results": rows, "dataset_binding": "fixture-binding",
        "model_api_calls": 196, "max_api_calls": None})
    item = next(item for item in payload["results"]["results"] if item["vars"]["task_id"] == row["id"])
    item["response"] = provider.render_response(row)
    report = runner.collect_campaign(root, plan, payload, output)
    assert "invalid_task_local_gateway_error_classification" in report["evidence_errors"]
    assert report["workflow_completed"] is False
    row["model_failures"][0]["category"] = "server"
    _write(root / "results/results.json", {"results": rows, "dataset_binding": "fixture-binding",
        "model_api_calls": 196, "max_api_calls": None})
    item["response"] = provider.render_response(row)
    ledger_path = root / "results/http-budget.json"
    ledger = json.loads(ledger_path.read_bytes())
    next(call for call in ledger["calls"] if call["case_id"] == row["id"])["status"] = "outcome_unknown"
    _write(ledger_path, ledger)
    report = runner.collect_campaign(root, plan, payload, output)
    assert "invalid_task_local_gateway_error_classification" in report["evidence_errors"]
    assert report["unknown_request_outcomes"] == 1 and report["workflow_completed"] is False
