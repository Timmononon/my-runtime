"""Held-out orchestration boundaries; fixtures never measure model capability."""
from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from bfcl_test_prerequisites import require_corpus
from test_bfcl_promptfoo_development import collected as original_collected

PROJECT = Path(__file__).resolve().parents[1]
ADAPTER = PROJECT / "evals/bfcl_multiturn/promptfoo"


def module(name):
    spec = importlib.util.spec_from_file_location("test_holdout_" + name, ADAPTER / (name + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture(scope="module")
def plan():
    require_corpus()
    return module("run_compact_holdout").preflight("A0", 20)


def test_public_parent_has_no_aster_import_before_fresh_worker():
    tree = ast.parse((ADAPTER / "run_compact_holdout.py").read_text(encoding="utf-8"))
    for item in tree.body:
        if isinstance(item, ast.ImportFrom):
            assert not (item.module or "").startswith("aster")
        elif isinstance(item, ast.Import):
            assert all(not name.name.startswith("aster") for name in item.names)


@pytest.mark.bfcl_evidence
def test_exact_sixty_holdout_matrix_has_no_development_or_gold_in_variables(plan, tmp_path):
    runner = module("run_compact_holdout")
    expected = (PROJECT / "evals/bfcl_multiturn/splits/compact100-v2/holdout-ids.txt").read_text().splitlines()
    development = set((PROJECT / "evals/bfcl_multiturn/splits/compact100-v2/development-ids.txt").read_text().splitlines())
    config = runner.build_config(plan, tmp_path)
    ids = [test["vars"]["task_id"] for test in config["tests"]]
    assert ids == expected and len(ids) == 60 and not set(ids) & development
    assert all(set(test["vars"]) == {"task_id"} for test in config["tests"])
    assert plan["evaluation_split"] == "holdout" and plan["evaluation_task_count"] == 60
    assert plan["defined_user_turns"] == 262 and plan["planned_holdout_tasks"] == 60
    assert plan["development_inference_tasks"] == plan["holdout_inference_tasks"] == 0
    assert config["providers"][0]["config"]["configuration_label"] == "A0"
    assert config["providers"][0]["config"]["workers"] == config["evaluateOptions"]["maxConcurrency"] == 20
    assert runner.cli_command(tmp_path, 20)[-2:] == ["--max-concurrency", "20"]


@pytest.mark.parametrize("configuration", ["A0", "A3", "MF-Gate-v1"])
@pytest.mark.bfcl_evidence
def test_configuration_flags_are_explicit_and_no_new_input_or_three_state_gate(configuration):
    require_corpus()
    plan = module("run_compact_holdout").preflight(configuration)
    assert plan["configuration_label"] == configuration
    assert plan["agent_policy"] == {"A0": None, "A3": "readiness_state_v1",
                                    "MF-Gate-v1": "capability_check_v1"}[configuration]
    assert plan["runtime_capability_gate"] == ("capability_check_v1" if configuration == "MF-Gate-v1" else "off")
    assert plan["runtime_task_control"] == ("readiness_state_v1" if configuration == "A3" else "off")
    assert plan["official_max_steps_per_user_turn"] == 20 and plan["max_output_tokens"] == 8192
    assert plan["provider_retries"] == plan["engine_retries"] == plan["judge_api_calls"] == 0


@pytest.mark.parametrize("configuration,concurrency", [("other", 20), ("A0", 0), ("A0", True), ("A0", 101)])
def test_invalid_scope_rejected_before_credentials(configuration, concurrency):
    with pytest.raises(ValueError):
        module("run_compact_holdout").preflight(configuration, concurrency)


@pytest.fixture
def captured(plan, tmp_path, monkeypatch):
    root, plan, payload, output, rows = original_collected.__wrapped__(plan, tmp_path, monkeypatch)
    control = json.loads((root / "execution-control.json").read_bytes())
    control["concurrent_tasks"]["maximum"] = 20
    write(root / "execution-control.json", control)
    ledger = json.loads((root / "results/http-budget.json").read_bytes())
    for index, call in enumerate(ledger["calls"]):
        first = 100 + (index // 20) * 200
        call["started_at"] = first
        key = module("holdout_collector")._helpers.digest(call["case_id"].encode())[:24]
        for suffix in (".started.json", ".finished.json"):
            path = output / "dispatches" / (key + suffix)
            row = json.loads(path.read_bytes())
            row.update(started_at=first - 1, finished_at=first + 100, pid=index % 20 + 1)
            write(path, row)
    write(root / "results/http-budget.json", ledger)
    payload["results"]["stats"]["errors"] = 0
    for row in payload["results"]["results"]:
        row["provider"]["label"] = "A0-holdout60-flash-none"
    return root, plan, payload, output, rows


@pytest.mark.bfcl_evidence
def test_sixty_task_collector_reports_actual_holdout_use(captured):
    root, plan, payload, output, _ = captured
    report = module("holdout_collector").collect_base_campaign(root, plan, payload, output)
    assert report["evidence_errors"] == []
    assert report["planned_tasks"] == report["holdout_inference_tasks"] == report["scorable_tasks"] == 60
    assert report["development_inference_tasks"] == 0 and report["evaluation_split"] == "holdout"
    assert report["passed_tasks"] == 30 and report["main_success_rate"] == .5
    assert report["baseline_assessment"]["holdout_validation_performed"] is True
    assert report["baseline_assessment"]["kind"] == "A0_frozen_holdout_agent_configuration"
    assert "development-set" not in report["score_note"]


def test_infrastructure_is_null_but_budget_and_control_failures_are_zero():
    core = module("holdout_collector")
    a3 = module("run_compact_development_a3")
    error = {"result": "fail", "agent_control_failure": True, "runs": [{"runtime_status": "failed",
        "agent_control_failure": True, "runtime_diagnostic": {"error_type": "TaskControlError"}}]}
    assert a3._classification(error) == "agent_control_failure" and not a3._runtime_infrastructure(error)
    error["runs"][0]["runtime_diagnostic"]["error_type"] = "StateError"
    assert a3._classification(error) == "infrastructure_error"
    assert core._classification({"result": "fail", "runs": [{"runtime_status": "budget_exhausted"}]}) == "budget_termination"


def test_parent_rejects_started_campaign_without_spawning(tmp_path, monkeypatch):
    runner = module("run_compact_holdout")
    write(tmp_path / "execution-started.json", {"outcome": "unknown"})
    monkeypatch.setattr(runner.subprocess, "run", lambda *_a, **_k: pytest.fail("must not spawn"))
    with pytest.raises(SystemExit):
        runner.main(["--configuration", "A0", "--campaign-dir", str(tmp_path), "--execute"])


@pytest.fixture
def import_workspace(tmp_path, monkeypatch):
    identity = module("holdout_identity")
    write(tmp_path / "frozen-runtime-manifest.json", {"configuration_label": "A0", "source_commit": identity.COMMITS["A0"]})
    modules = {}
    for name in identity.MODULES:
        path = tmp_path / "src" / (name.replace(".", "/") + ".py")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# fixture source\n", encoding="utf-8")
        modules[name] = SimpleNamespace(__file__=str(path))
    monkeypatch.setattr(identity.importlib, "import_module", lambda name: modules[name])
    return identity, tmp_path, modules


def test_import_identity_rejects_main_workspace_fallback(import_workspace):
    identity, root, modules = import_workspace
    proof = identity.capture_runtime_identity("A0", root)
    assert len(proof["modules"]) == 5 and all(row["relative_file"].startswith("src/aster/") for row in proof["modules"])
    modules["aster.engine.loop"].__file__ = str(PROJECT / "src/aster/engine/loop.py")
    with pytest.raises(ValueError, match="escaped"):
        identity.capture_runtime_identity("A0", root)


def test_import_identity_cannot_relabel_other_frozen_version(import_workspace):
    identity, root, _modules = import_workspace
    with pytest.raises(ValueError, match="selected label"):
        identity.capture_runtime_identity("A3", root)


def test_import_identity_checks_as_run_hash_not_only_origin(import_workspace, tmp_path):
    identity, root, _modules = import_workspace
    campaign = tmp_path / "campaign"
    write(campaign / "source-manifest.json", {"files": [
        {"file": row["relative_file"], "sha256": row["sha256"]}
        for row in identity.capture_runtime_identity("A0", root)["modules"]]})
    identity.capture_runtime_identity("A0", root, campaign=campaign)
    (root / "src/aster/engine/loop.py").write_text("# tampered\n", encoding="utf-8")
    with pytest.raises(ValueError, match="as-run"):
        identity.capture_runtime_identity("A0", root, campaign=campaign)
