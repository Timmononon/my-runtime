"""Frozen full development selection, 100-way admission, and baseline reporting."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import shutil
from collections import Counter
from pathlib import Path

import pytest
from bfcl_test_prerequisites import require_corpus
from test_bfcl_current import source_at
from test_bfcl_multiturn_concurrent import case_at, factory_for, setup_campaign

from aster.evaluation import bfcl_multiturn_concurrent as backend
from aster.evaluation.bfcl_current import capture_current_profile
from aster.evaluation.bfcl_v4_data import load_corpus

PROJECT = Path(__file__).resolve().parents[1]
HERE = PROJECT / "evals/bfcl_multiturn/promptfoo"


def _module(name):
    spec = importlib.util.spec_from_file_location("development_test_" + name, HERE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner, provider = (_module(name) for name in ("run_development", "concurrent_provider"))


@pytest.fixture(scope="module")
def selection():
    require_corpus()
    return runner._dataset.load_development_dataset()


@pytest.fixture
def plan(selection, monkeypatch):
    cases, metadata = selection
    monkeypatch.setattr(runner._dataset, "load_development_dataset", lambda: (cases, copy.deepcopy(metadata)))
    return runner.preflight()


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(runner._helpers.encoded(value))


def _snapshot(root):
    source = Path(backend.__file__)
    relative = source.relative_to(PROJECT)
    destination = root / "as-run-source" / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(source.read_bytes())
    _write(root / "source-manifest.json", {"files": [{"file": relative.as_posix(),
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}]})


@pytest.mark.bfcl_evidence
def test_all_two_hundred_are_complete_frozen_members_and_four_conflicts_are_kept(selection):
    cases, metadata = selection
    frozen = set((runner._dataset.FROZEN / "development-ids.txt").read_text(encoding="utf-8").splitlines())
    heldout = set((runner._dataset.FROZEN / "holdout-ids.txt").read_text(encoding="utf-8").splitlines())
    assert len(cases) == len({case["id"] for case in cases}) == 200
    assert {case["id"] for case in cases} == frozen and frozen.isdisjoint(heldout)
    assert Counter(case["category"] for case in cases) == dict.fromkeys(runner._dataset.CATEGORIES, 50)
    assert [case["category"] for case in cases[:4]] == list(runner._dataset.CATEGORIES)
    assert sum(len(case["question"]) for case in cases) == metadata["defined_user_turns"] == 840
    assert set(metadata["known_admission_conflicts"]) == {
        f"{category}_173" for category in runner._dataset.CATEGORIES}
    assert metadata["admissible_task_count"] == 196 and len(metadata["newly_detected_conflict_ids"]) == 3
    assert all(case["functions"] and case["expected"] is not None for case in cases)
    assert metadata["model_api_calls"] == metadata["judge_api_calls"] == 0


@pytest.mark.bfcl_evidence
def test_old_twenty_normalize_to_identical_execution_and_scoring_inputs(selection):
    cases, _metadata = selection
    old = load_corpus(runner._dataset.CORPUS, tracks=list(runner._dataset.CATEGORIES))
    new = {case["id"]: case for case in cases}
    assert len(old) == 20
    for case in old:
        assert {key: value for key, value in new[case["id"]].items() if key != "provenance"} == {
            key: value for key, value in case.items() if key != "provenance"}


def test_changed_membership_or_manifest_refuses_before_model_or_runtime(tmp_path):
    frozen = tmp_path / "frozen"
    shutil.copytree(runner._dataset.FROZEN, frozen)
    ids = frozen / "development-ids.txt"
    ids.write_bytes(ids.read_bytes().replace(b"multi_turn_base_", b"multi_turn_other_", 1))
    with pytest.raises(ValueError, match="membership changed"):
        runner._dataset.load_development_dataset(frozen=frozen)
    manifest = frozen / "split-manifest.json"
    manifest.write_bytes(manifest.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="manifest changed"):
        runner._dataset.load_development_dataset(frozen=frozen)


@pytest.mark.bfcl_evidence
def test_one_hundred_all_scheduling_parameters_and_offline_preflight_agree(plan, tmp_path):
    config = runner.build_config(plan, tmp_path)
    assert plan["maximum_concurrency"] == plan["python_workers"] == plan["concurrency"] == 100
    assert plan["backend_mode"] == backend.MODE
    assert len(config["tests"]) == 200
    assert len({row["metadata"]["campaign_dir"] for row in config["tests"]}) == 1
    assert config["providers"][0]["config"]["workers"] == 100
    assert config["evaluateOptions"]["maxConcurrency"] == 100
    assert runner.cli_command(tmp_path, 100)[-2:] == ["--max-concurrency", "100"]
    assert plan["reasoning_effort"] == "none" and plan["max_output_tokens"] == 8192
    root = tmp_path / "preflight"
    assert runner.main(["--campaign-dir", str(root)]) == 0
    assert {path.name for path in root.iterdir()} == {"campaign-plan.json"}
    for number in (0, -1, 101, True, 1.5):
        with pytest.raises(ValueError, match="Concurrency"):
            runner.preflight(number)


@pytest.mark.bfcl_evidence
def test_real_paid_admission_is_one_two_hundred_task_campaign_with_empty_ledger(plan, selection, tmp_path):
    cases, _metadata = selection
    root = tmp_path / "campaign"
    _write(root / "campaign-plan.json", plan)
    _snapshot(root)
    profile = capture_current_profile(source_at(tmp_path), model="deepseek-flash", reasoning_effort="none")
    control = backend.initialize_campaign(cases, root, profile=profile, maximum_concurrency=100)
    assert control["test_only"] is False and control["concurrent_tasks"]["maximum"] == 100
    assert control["case_ids"] == [group["group"] for group in plan["groups"]]
    assert len(json.loads((root / "private-cases.json").read_bytes())) == 200
    assert json.loads((root / "results/http-budget.json").read_bytes())["calls"] == []
    assert len(list(root.rglob("execution-control.json"))) == len(list(root.rglob("http-budget.json"))) == 1
    assert {key for key, value in control["reference_admission_issues"].items() if value} == set(plan["known_admission_conflicts"])


@pytest.mark.bfcl_evidence
def test_launcher_uses_full_raw_cases_and_new_backend_before_any_native_process(plan, selection, tmp_path, monkeypatch):
    import aster.paths

    source = source_at(tmp_path)
    root = tmp_path / "launch"
    monkeypatch.setattr(aster.paths, "resolve_data_dir", lambda: source)
    monkeypatch.setattr(runner, "load_development_cases", lambda: copy.deepcopy(selection[0]))
    monkeypatch.setattr(runner._helpers, "snapshot_sources", _snapshot)

    class OfflineStop(RuntimeError):
        pass

    def stop(command, **kwargs):
        assert command[-2:] == ["--max-concurrency", "100"]
        assert kwargs["env"]["PROMPTFOO_PYTHON_WORKERS"] == "100"
        config = json.loads((root / "promptfoo/promptfooconfig.json").read_bytes())
        assert len(config["tests"]) == 200 and config["providers"][0]["config"]["workers"] == 100
        raise OfflineStop("Deliberate fixture stop; no child or network dispatch")

    monkeypatch.setattr(runner.subprocess, "run", stop)
    with pytest.raises(OfflineStop):
        runner.main(["--campaign-dir", str(root), "--execute"])
    assert len(json.loads((root / "private-cases.json").read_bytes())) == 200
    assert json.loads((root / "results/http-budget.json").read_bytes())["calls"] == []
    assert not list((root / "results/intents").glob("*.json"))


@pytest.fixture
def collected(plan, tmp_path, monkeypatch):
    root = tmp_path / "campaign"
    output = root / "promptfoo"
    ids = [group["group"] for group in plan["groups"]]
    conflicts = set(plan["known_admission_conflicts"])
    control = {"case_ids": ids, "dataset_binding": "fixture-binding", "test_only": False, "fatal": None,
        "concurrency_performance_policy": plan["concurrency_performance_policy"],
        "reference_admission_issues": {task_id: plan["reference_admission_issues"].get(task_id, []) for task_id in ids},
        "concurrent_tasks": {"mode": backend.MODE, "maximum": 100, "finalized": True,
            "states": dict.fromkeys(ids, "completed")}}
    rows, native, calls = [], [], []
    valid_index = 0
    for group in plan["groups"]:
        task_id = group["group"]
        blocked = task_id in conflicts
        passed = valid_index % 2 == 0
        first = 100 if valid_index < 100 else 201
        row = {"id": task_id, "backend_mode": backend.MODE, "model_quality": "unsupported" if blocked else "evaluated",
            "result": "blocked_dataset_conflict" if blocked else "pass" if passed else "fail",
            "http_attempts": 0 if blocked else 1, "model_api_calls": 0 if blocked else 1,
            "termination_kind": "dataset_conflict" if blocked else "completed"}
        if blocked:
            row["reference_admission_issues"] = plan["reference_admission_issues"][task_id]
        else:
            row.update(session_id=f"fixture-session-{valid_index}", protocol_result="pass",
                scoring_verification={"valid": passed, "result": row["result"]},
                runs=[{"runtime_status": "completed"}] * group["user_turns"],
                usage=[{"input_tokens": 20, "output_tokens": 5, "reasoning_tokens": 0,
                    "cache_hit_tokens": 10, "cache_miss_tokens": 10}])
            calls.append({"case_id": task_id, "started_at": first, "status": "response_headers_received",
                "status_code": 200, "thinking_disabled_verified": True, "wire_model": "deepseek-flash",
                "wire_reasoning_effort": "none", "wire_max_output_tokens": 8192})
            valid_index += 1
        rows.append(row)
        key = runner._helpers.digest(task_id.encode())[:24]
        _write(root / "results/intents" / (key + ".json"), {"task_id": task_id})
        telemetry = {"task_id": task_id, "pid": valid_index % 100 + 1, "campaign_dir": str(root.resolve()),
            "backend_mode": backend.MODE, "started_at": first - 1, "finished_at": first + 100,
            "outcome": "backend_returned", "error_type": None, "http_attempts_for_task": row["model_api_calls"]}
        for suffix in (".started.json", ".finished.json"):
            _write(output / "dispatches" / (key + suffix), telemetry)
        response = provider.render_response(row)
        item = {"vars": {"task_id": task_id}, "provider": {"label": runner.PROVIDER_LABEL},
            "success": passed and not blocked, "response": response, "gradingResult": {"componentResults": [
                {"reason": "Live BFCL official complete-task result: " + row["result"]}]}}
        if blocked:
            item["error"] = response["error"]
        native.append(item)
    _write(root / "execution-control.json", control)
    _write(root / "results/results.json", {"results": rows, "dataset_binding": "fixture-binding",
        "model_api_calls": len(calls), "max_api_calls": None})
    _write(root / "results/http-budget.json", {"binding": "fixture-binding", "maximum": None, "calls": calls})
    by_id = {row["id"]: row for row in rows}
    monkeypatch.setattr(backend, "read_task_receipt", lambda _root, task_id: by_id[task_id])
    return root, plan, {"results": {"results": native, "stats": {"errors": 4}}}, output, rows


@pytest.mark.bfcl_evidence
def test_baseline_counts_quarantines_as_null_and_reports_micro_macro_delivery(collected):
    root, plan, native, output, _rows = collected
    report = runner.collect_campaign(root, plan, native, output)
    assert report["evidence_errors"] == []
    assert report["allocated_development_tasks"] == report["native_provider_invocations"] == 200
    assert report["data_conflict_tasks"] == 4 and report["admissible_tasks"] == report["scorable_tasks"] == 196
    assert report["passed_tasks"] == 98 and report["scorable_success_rate"] == 0.5
    assert report["multiturn_category_macro_success_rate"] == report["admissible_delivery_success_rate"] == 0.5
    assert report["baseline_assessment"]["capability_baseline_ready"] is True
    assert report["baseline_assessment"]["usage_baseline_ready"] is True
    assert report["independent_session_count"] == report["complete_runtime_tasks"] == 196
    assert report["peak_overlapping_tasks_with_http"] == report["maximum_concurrency"] == 100
    assert all(group["allocated_tasks"] == 50 and group["data_conflicts"] == 1
               and group["admissible_tasks"] == 49 for group in report["subcategories"].values())
    assert all(task["main_score"] is None for task in report["tasks"] if task["classification"] == "data_conflict")


@pytest.mark.bfcl_evidence
def test_infrastructure_is_not_a_model_zero_and_prevents_clean_capability_baseline(collected):
    root, plan, native, output, rows = collected
    row = next(row for row in rows if row["result"] == "fail")
    row["result"] = "case_error"
    item = next(item for item in native["results"]["results"] if item["vars"]["task_id"] == row["id"])
    item["response"] = provider.render_response(row)
    item["error"] = item["response"]["error"]
    native["results"]["stats"]["errors"] += 1
    _write(root / "results/results.json", {"results": rows, "dataset_binding": "fixture-binding",
        "model_api_calls": 196, "max_api_calls": None})
    report = runner.collect_campaign(root, plan, native, output)
    assert report["evidence_errors"] == []
    assert report["scorable_tasks"] == 195 and report["passed_tasks"] == 98
    assert report["scorable_success_rate"] == 98 / 195
    assert report["admissible_delivery_success_rate"] == 98 / 196
    assert report["failure_classifications"]["infrastructure_error"] == 1
    assert report["baseline_assessment"]["capability_baseline_ready"] is False
    assert "infrastructure_or_uninvoked_tasks" in report["baseline_assessment"]["readiness_limitations"]


@pytest.mark.bfcl_evidence
def test_unknown_request_outcome_or_corrupt_native_evidence_blocks_baseline(collected):
    root, plan, native, output, _rows = collected
    ledger_path = root / "results/http-budget.json"
    ledger = json.loads(ledger_path.read_bytes())
    ledger["calls"][0]["status"] = "outcome_unknown"
    _write(ledger_path, ledger)
    report = runner.collect_campaign(root, plan, native, output)
    assert report["baseline_assessment"]["capability_baseline_ready"] is False
    assert "unknown_request_outcomes" in report["baseline_assessment"]["readiness_limitations"]
    native["results"]["results"][0]["response"]["output"] = "{}"
    assert runner.collect_campaign(root, plan, native, output)["evidence_errors"]


@pytest.mark.bfcl_evidence
def test_started_campaign_refuses_without_overwriting_evidence(plan, tmp_path):
    root = tmp_path / "started"
    root.mkdir()
    marker = root / "execution-started.json"
    marker.write_text("existing execution")
    with pytest.raises(SystemExit) as stopped:
        runner.main(["--campaign-dir", str(root), "--execute"])
    assert stopped.value.code == 2 and marker.read_text() == "existing execution"
    assert not (root / "results").exists()


@pytest.mark.asyncio
@pytest.mark.bfcl_evidence
async def test_paid_run_one_resolves_profile_and_reaches_execute_without_any_http(plan, selection, tmp_path, monkeypatch):
    root = tmp_path / "paid-admission"
    case = selection[0][0]
    one = copy.deepcopy(plan)
    one["groups"] = [group for group in plan["groups"] if group["group"] == case["id"]]
    _write(root / "campaign-plan.json", one)
    _snapshot(root)
    profile = capture_current_profile(source_at(tmp_path), model="deepseek-flash", reasoning_effort="none")
    backend.initialize_campaign([case], root, profile=profile, maximum_concurrency=100)
    reached = []

    async def controlled_stop(actual_root, actual_case, captured, control, factory):
        assert actual_root == root and actual_case["id"] == case["id"]
        assert captured.model == "deepseek-flash" and captured.reasoning_effort == "none"
        assert control["test_only"] is False and factory is None
        reached.append(actual_case["id"])
        return {"id": case["id"], "result": "case_error", "status": "case_error",
            "model_quality": "unsupported", "reason": "fixture_stopped_before_http",
            "model_api_calls": 0, "http_attempts": 0, "backend_mode": backend.MODE}, None

    monkeypatch.setattr(backend, "_execute", controlled_stop)
    row = await backend.run_one_current_multiturn(case["id"], root)
    assert reached == [case["id"]] and row["reason"] == "fixture_stopped_before_http"
    assert json.loads((root / "results/http-budget.json").read_bytes())["calls"] == []
    assert backend.read_task_receipt(root, case["id"]) == row


@pytest.mark.asyncio
async def test_full_static_verification_never_runs_under_shared_batch_lock(tmp_path, monkeypatch):
    root, profile = setup_campaign(tmp_path, count=1)
    original_lock, original_verify = backend._batch_lock, backend.serial._verified_control
    held = 0

    class ObservedLock:
        def __enter__(self):
            nonlocal held
            self.lock = original_lock(root)
            self.lock.__enter__()
            held += 1
            return self

        def __exit__(self, *args):
            nonlocal held
            held -= 1
            return self.lock.__exit__(*args)

    def verified(path):
        assert held == 0, "Full matrix/source validation may not hold the shared batch lock"
        return original_verify(path)

    monkeypatch.setattr(backend, "_batch_lock", lambda _root: ObservedLock())
    monkeypatch.setattr(backend.serial, "_verified_control", verified)
    factory, calls = factory_for(0)
    row = await backend.run_one_current_multiturn("multi_turn_base_0", root,
        _profile=profile, _test_transport_factory=factory)
    assert row["verification_result"] == "pass" and len(calls) == 4
    assert backend.read_task_receipt(root, row["id"]) == row
    assert backend.finalize_campaign(root)["http_transport_attempts"] == 4


@pytest.mark.asyncio
async def test_commit_publishes_only_receipt_and_control_until_final_aggregation(tmp_path, monkeypatch):
    root, profile = setup_campaign(tmp_path, count=1)
    aggregate = backend._merge_report
    aggregated = []

    def observed(*args):
        aggregated.append(True)
        return aggregate(*args)

    monkeypatch.setattr(backend, "_merge_report", observed)
    factory, calls = factory_for(0)
    row = await backend.run_one_current_multiturn("multi_turn_base_0", root,
        _profile=profile, _test_transport_factory=factory)
    assert aggregated == [] and row["verification_result"] == "pass" and len(calls) == 4
    assert json.loads((root / "execution-control.json").read_bytes())["concurrent_tasks"]["states"][row["id"]] == "completed"
    final = backend.finalize_campaign(root)
    assert aggregated == [True] and final["results"] == [row]
    assert final["concurrency_performance_policy"] == backend.PERFORMANCE_POLICY
    assert backend._batch_lock(root).timeout == 300
    assert backend._TaskHTTPBudget(root, json.loads((root / "execution-control.json").read_bytes()), row["id"]).lock().timeout == 300


def test_fresh_control_preserves_latest_fatal_and_rejects_mutated_static_binding(tmp_path):
    root, _profile = setup_campaign(tmp_path, count=1)
    verified, cases = backend._verified(root)
    del cases
    latest = json.loads((root / "execution-control.json").read_bytes())
    latest["fatal"] = "unknown_provider_request_outcome"
    latest["concurrent_tasks"]["finalized"] = True
    _write(root / "execution-control.json", latest)
    with backend._batch_lock(root):
        current = backend._fresh_control(root, verified)
    assert current["fatal"] == "unknown_provider_request_outcome"
    assert current["concurrent_tasks"]["finalized"] is True
    latest["profile_guard"] = "mutated-static-binding"
    _write(root / "execution-control.json", latest)
    with backend._batch_lock(root), pytest.raises(ValueError, match="immutable control"):
        backend._fresh_control(root, verified)


@pytest.mark.asyncio
async def test_changed_source_plan_is_still_rejected_before_model_dispatch(tmp_path):
    source = source_at(tmp_path)
    profile = capture_current_profile(source, model="deepseek-flash", reasoning_effort="none")
    root = tmp_path / "campaign"
    _write(root / "campaign-plan.json", {"fixture": True})
    _write(root / "source-manifest.json", {"files": []})
    backend.initialize_campaign([case_at(0)], root, profile=profile, _test_only=True)
    _write(root / "campaign-plan.json", {"fixture": "changed"})
    factory, calls = factory_for(0)
    with pytest.raises(ValueError, match="source/plan binding changed"):
        await backend.run_one_current_multiturn("multi_turn_base_0", root,
            _profile=profile, _test_transport_factory=factory)
    assert calls == [] and not list((root / "results/intents").glob("*.json"))


@pytest.mark.asyncio
async def test_completed_receipts_are_checked_on_explicit_read_and_final_seal(tmp_path):
    root, profile = setup_campaign(tmp_path, count=2)
    first_factory, _first_calls = factory_for(0)
    await backend.run_one_current_multiturn("multi_turn_base_0", root,
        _profile=profile, _test_transport_factory=first_factory)
    path = backend.serial._receipt_path(root, "multi_turn_base_0")
    receipt = json.loads(path.read_bytes())
    receipt["dataset_binding"] = "corrupted-receipt"
    _write(path, receipt)
    second_factory, second_calls = factory_for(1)
    second = await backend.run_one_current_multiturn("multi_turn_base_1", root,
        _profile=profile, _test_transport_factory=second_factory)
    assert second["verification_result"] == "pass" and len(second_calls) == 4
    with pytest.raises(ValueError, match="receipt identity"):
        backend.read_task_receipt(root, "multi_turn_base_0")
    with pytest.raises(ValueError, match="receipt identity"):
        backend.finalize_campaign(root)
    assert json.loads((root / "execution-control.json").read_bytes())["concurrent_tasks"]["finalized"] is False


def _replace_error(target, *, winerror=5, source=None, destination=None):
    failure = PermissionError(13, "controlled local replace failure", str(
        source or target.parent / ("." + target.name + ".fixture.tmp")))
    failure.winerror = winerror
    failure.filename2 = str(destination or target)
    return failure


@pytest.mark.skipif(os.name != "nt", reason="Windows typed sharing violations")
def test_known_windows_replace_collision_retries_local_write_then_succeeds(tmp_path, monkeypatch):
    root = tmp_path / "campaign"
    (root / "results").mkdir(parents=True)
    target = root / "execution-control.json"
    target.write_text("original")
    original, calls, pauses = backend._raw_write, [], []

    def controlled(path, value):
        calls.append(path)
        if len(calls) == 1:
            raise _replace_error(target)
        return original(path, value)

    monkeypatch.setattr(backend, "_raw_write", controlled)
    monkeypatch.setattr(backend, "_io_pause", pauses.append)
    backend._write(target, {"phase": "committed"})
    assert len(calls) == 2 and pauses == [0.005]
    assert json.loads(target.read_bytes()) == {"phase": "committed"}
    records = [json.loads(line) for line in next((root / "results/io-retries").glob("*.jsonl")).read_text().splitlines()]
    assert [record["event"] for record in records] == ["atomic_replace_retry_started", "atomic_replace_retry_succeeded"]
    assert all(set(record) == {"event", "pid", "errno", "winerror", "target_basename", "retry_count", "elapsed_seconds"}
               for record in records)
    assert records[-1]["retry_count"] == 1 and records[-1]["target_basename"] == target.name


@pytest.mark.skipif(os.name != "nt", reason="Windows typed sharing violations")
def test_other_permission_or_replace_paths_fail_immediately_without_retry(tmp_path, monkeypatch):
    target = tmp_path / "control.json"
    target.write_text("original")
    pauses = []
    monkeypatch.setattr(backend, "_io_pause", pauses.append)
    for failure in (
        _replace_error(target, winerror=123),
        _replace_error(target, source=tmp_path / "outside" / ".control.json.fixture.tmp"),
        _replace_error(target, source=tmp_path / ".different.json.fixture.tmp"),
        _replace_error(target, destination=tmp_path / "another.json"),
        PermissionError(13, "actual denied permission", str(target)),
    ):
        calls = []

        def rejected(path, value, *, error=failure, events=calls):
            events.append(path)
            raise error

        monkeypatch.setattr(backend, "_raw_write", rejected)
        with pytest.raises(PermissionError) as raised:
            backend._write(target, {"must_not_commit": True})
        assert raised.value is failure and len(calls) == 1
    assert pauses == [] and target.read_text() == "original"


@pytest.mark.skipif(os.name != "nt", reason="Windows typed sharing violations")
def test_local_replace_retry_exhaustion_is_bounded_and_never_claims_success(tmp_path, monkeypatch):
    root = tmp_path / "campaign"
    (root / "results").mkdir(parents=True)
    target = root / "execution-control.json"
    target.write_text("original")
    clock, calls = [0.0], []
    failure = _replace_error(target, winerror=32)

    def rejected(path, value):
        calls.append(path)
        raise failure

    def pause(delay):
        assert 0 < delay <= 0.05
        clock[0] += delay

    monkeypatch.setattr(backend, "_raw_write", rejected)
    monkeypatch.setattr(backend, "_io_clock", lambda: clock[0])
    monkeypatch.setattr(backend, "_io_pause", pause)
    with pytest.raises(PermissionError) as raised:
        backend._write(target, {"must_not_commit": True})
    assert raised.value is failure and clock[0] == pytest.approx(5.0)
    assert 1 < len(calls) < 1500 and target.read_text() == "original"
    records = [json.loads(line) for line in next((root / "results/io-retries").glob("*.jsonl")).read_text().splitlines()]
    assert records[-1]["event"] == "atomic_replace_retry_exhausted"
    assert records[-1]["elapsed_seconds"] == pytest.approx(5.0)
