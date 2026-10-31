"""Offline compact forty-task selection, native scheduling and paid admission."""
from __future__ import annotations

import copy
import importlib.util
import json
import shutil
from collections import Counter
from pathlib import Path

import pytest
from bfcl_test_prerequisites import require_corpus
from test_bfcl_current import source_at
from test_bfcl_promptfoo_development import collected as original_collected
from test_bfcl_promptfoo_development import provider as native_provider

PROJECT = Path(__file__).resolve().parents[1]
HERE = PROJECT / "evals/bfcl_multiturn/promptfoo"
SPEC = importlib.util.spec_from_file_location("compact_development_test", HERE / "run_compact_development.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(runner._helpers.encoded(value))


@pytest.fixture(scope="module")
def selection():
    require_corpus()
    return runner._dataset.load_development_dataset()


@pytest.fixture
def plan(selection, monkeypatch):
    monkeypatch.setattr(runner._dataset, "load_development_dataset", lambda: copy.deepcopy(selection))
    return runner.preflight()


@pytest.mark.bfcl_evidence
def test_exact_forty_complete_tasks_preserve_sources_and_leave_sixty_holdout_unplanned(selection):
    cases, metadata = selection
    frozen = runner._dataset.FROZEN
    ids = (frozen / "development-ids.txt").read_text().splitlines()
    heldout = set((frozen / "holdout-ids.txt").read_text().splitlines())
    assert [case["id"] for case in cases] == ids and len(ids) == len(set(ids)) == 40
    assert not set(ids) & heldout and len(heldout) == 60
    assert Counter(case["category"] for case in cases) == dict.fromkeys(runner._dataset.CATEGORIES, 10)
    assert metadata["allocated_development_tasks"] == metadata["admissible_task_count"] == 40
    assert metadata["allocated_holdout_tasks"] == 60 and metadata["defined_user_turns"] == 156
    assert metadata["known_admission_conflicts"] == []
    assert metadata["model_api_calls"] == metadata["judge_api_calls"] == metadata["holdout_inference_tasks"] == 0
    old, _ = runner._dataset._original.load_development_dataset()
    by_id = {case["id"]: case for case in old}
    for case in cases:
        assert {key: value for key, value in case.items() if key != "provenance"} == {
            key: value for key, value in by_id[case["id"]].items() if key != "provenance"}


def test_compact_membership_and_manifest_tamper_refuse_before_runtime(tmp_path):
    frozen = tmp_path / "compact"
    shutil.copytree(runner._dataset.FROZEN, frozen)
    ids = frozen / "development-ids.txt"
    ids.write_bytes(ids.read_bytes().replace(b"multi_turn_base_", b"other_", 1))
    with pytest.raises(ValueError, match="membership changed"):
        runner._dataset.load_development_dataset(frozen=frozen)
    manifest = frozen / "split-manifest.json"
    manifest.write_bytes(manifest.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="manifest changed"):
        runner._dataset.load_development_dataset(frozen=frozen)


@pytest.mark.bfcl_evidence
def test_offline_preflight_native_matrix_and_all_twenty_way_settings_agree(plan, tmp_path, monkeypatch):
    import aster.paths

    def forbidden():
        raise AssertionError("Offline preflight cannot read the user Runtime")

    monkeypatch.setattr(aster.paths, "resolve_data_dir", forbidden)
    config = runner.build_config(plan, tmp_path)
    assert plan["maximum_concurrency"] == plan["concurrency"] == plan["python_workers"] == 20
    assert plan["model"] == "deepseek-flash" and plan["reasoning_effort"] == "none"
    assert plan["max_output_tokens"] == 8192 and plan["official_max_steps_per_user_turn"] == 20
    assert plan["authorized_api_call_limit"] is plan["per_case_api_call_cap"] is None
    assert config["providers"][0]["config"]["workers"] == config["evaluateOptions"]["maxConcurrency"] == 20
    assert len(config["tests"]) == 40 and all(set(test["vars"]) == {"task_id"} for test in config["tests"])
    assert [test["vars"]["task_id"] for test in config["tests"]] == [group["group"] for group in plan["groups"]]
    assert runner.cli_command(tmp_path, 20)[-2:] == ["--max-concurrency", "20"]
    root = tmp_path / "preflight"
    assert runner.main(["--campaign-dir", str(root)]) == 0
    assert {path.name for path in root.iterdir()} == {"campaign-plan.json"}
    for invalid in (0, -1, 101, True, 1.5):
        with pytest.raises(ValueError, match="Concurrency"):
            runner.preflight(invalid)


@pytest.mark.bfcl_evidence
def test_real_paid_path_initializes_only_forty_tasks_and_stops_before_process_or_http(plan, selection, tmp_path, monkeypatch):
    import aster.paths

    source, root = source_at(tmp_path), tmp_path / "paid"
    monkeypatch.setattr(aster.paths, "resolve_data_dir", lambda: source)
    monkeypatch.setattr(runner, "load_development_cases", lambda: copy.deepcopy(selection[0]))

    class OfflineStop(RuntimeError):
        pass

    def stop(command, **kwargs):
        assert command[-2:] == ["--max-concurrency", "20"]
        assert kwargs["env"]["PROMPTFOO_PYTHON_WORKERS"] == "20"
        config = json.loads((root / "promptfoo/promptfooconfig.json").read_bytes())
        assert len(config["tests"]) == 40 and config["providers"][0]["config"]["workers"] == 20
        raise OfflineStop("Deliberate stop before any subprocess or HTTP")

    monkeypatch.setattr(runner.subprocess, "run", stop)
    with pytest.raises(OfflineStop):
        runner.main(["--campaign-dir", str(root), "--execute"])
    control = json.loads((root / "execution-control.json").read_bytes())
    assert control["test_only"] is False and control["concurrent_tasks"]["maximum"] == 20
    assert control["case_ids"] == [case["id"] for case in selection[0]]
    assert len(json.loads((root / "private-cases.json").read_bytes())) == 40
    assert json.loads((root / "results/http-budget.json").read_bytes())["calls"] == []
    names = {entry["file"] for entry in json.loads((root / "source-manifest.json").read_bytes())["files"]}
    assert {"evals/bfcl_multiturn/promptfoo/run_compact_development.py",
        "evals/bfcl_multiturn/promptfoo/compact_development_dataset.py",
        "evals/bfcl_multiturn/splits/compact100-v2/development-ids.txt",
        "evals/bfcl_multiturn/splits/compact100-v2/holdout-ids.txt",
        "evals/bfcl_multiturn/splits/compact100-v2/split-manifest.json"} <= names


@pytest.fixture
def collected(plan, tmp_path, monkeypatch):
    root, _plan, payload, output, rows = original_collected.__wrapped__(plan, tmp_path, monkeypatch)
    control = json.loads((root / "execution-control.json").read_bytes())
    control["concurrent_tasks"]["maximum"] = 20
    _write(root / "execution-control.json", control)
    ledger = json.loads((root / "results/http-budget.json").read_bytes())
    for index, call in enumerate(ledger["calls"]):
        first = 100 + (index // 20) * 200
        call["started_at"] = first
        key = runner._helpers.digest(call["case_id"].encode())[:24]
        for suffix in (".started.json", ".finished.json"):
            path = output / "dispatches" / (key + suffix)
            telemetry = json.loads(path.read_bytes())
            telemetry.update(started_at=first - 1, finished_at=first + 100, pid=index % 20 + 1)
            _write(path, telemetry)
    _write(root / "results/http-budget.json", ledger)
    payload["results"]["stats"]["errors"] = 0
    return root, plan, payload, output, rows


@pytest.mark.bfcl_evidence
def test_forty_task_report_can_form_fixed_development_baseline(collected):
    root, plan, payload, output, _rows = collected
    report = runner.collect_campaign(root, plan, payload, output)
    assert report["evidence_errors"] == []
    assert report["allocated_development_tasks"] == report["scorable_tasks"] == 40
    assert report["allocated_holdout_tasks"] == 60 and report["holdout_inference_tasks"] == 0
    assert report["main_success_rate"] == report["scorable_success_rate"] == 0.5
    assert report["peak_overlapping_tasks_with_http"] == report["maximum_concurrency"] == 20
    assert report["baseline_assessment"]["capability_baseline_ready"] is True


@pytest.mark.bfcl_evidence
def test_runtime_state_error_is_null_in_capability_report_without_rewriting_raw_result(collected):
    root, plan, payload, output, rows = collected
    row = next(row for row in rows if row["result"] == "fail")
    row["runs"][0] = {"runtime_status": "failed", "runtime_diagnostic": {"error_type": "StateError"}}
    item = next(item for item in payload["results"]["results"] if item["vars"]["task_id"] == row["id"])
    item["response"] = native_provider.render_response(row)
    _write(root / "results/results.json", {"results": rows, "dataset_binding": "fixture-binding",
        "model_api_calls": 40, "max_api_calls": None})
    report = runner.collect_campaign(root, plan, payload, output)
    assert report["evidence_errors"] == []
    assert report["main_success_rate"] is None and report["scorable_tasks"] == 39
    task = next(task for task in report["tasks"] if task["task_id"] == row["id"])
    assert task["formal_result"] == "fail" and task["main_score"] is None
    assert task["classification"] == "infrastructure_error"
    assert report["runtime_infrastructure_ids"] == [row["id"]]
    assert report["baseline_assessment"]["capability_baseline_ready"] is False


def test_fixed_budget_stop_is_an_agent_failure_not_null_infrastructure():
    row = {"result": "fail", "runs": [{"runtime_status": "budget_exhausted"}],
        "termination_kind": "repeated_no_progress"}
    assert runner._classification(row) == "budget_termination" and runner._scorable(row)


@pytest.mark.bfcl_evidence
def test_started_compact_campaign_cannot_resume_or_overwrite_evidence(plan, tmp_path):
    root = tmp_path / "started"
    root.mkdir()
    marker = root / "execution-started.json"
    marker.write_text("original")
    with pytest.raises(SystemExit):
        runner.main(["--campaign-dir", str(root), "--execute"])
    assert marker.read_text() == "original" and not (root / "results").exists()
