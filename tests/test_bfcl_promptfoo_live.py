import atexit
import importlib.util
import json
import socket
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from bfcl_test_prerequisites import require_corpus

ADAPTER = Path(__file__).resolve().parents[1] / "evals/bfcl_multiturn/promptfoo"


def _module(name):
    spec = importlib.util.spec_from_file_location("test_bfcl_promptfoo_live_" + name, ADAPTER / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


live_assertion, live_provider, run_live = (_module(name) for name in ("live_assertion", "live_provider", "run_live"))


def saved_row():
    return {"id": "multi_turn_base_151", "result": "pass", "protocol_result": "pass",
            "scoring_verification": {"valid": True}, "model_api_calls": 1,
            "usage": [{"input_tokens": 20, "output_tokens": 5}], "messages": [{"content": "long" * 20000}]}


def test_provider_runs_backend_once_and_keeps_native_output_compact(monkeypatch, tmp_path):
    from aster.evaluation import bfcl_multiturn_live as backend

    row, calls = saved_row(), []

    @contextmanager
    def phase():
        yield

    async def run_one(task_id, campaign):
        calls.append((task_id, campaign))
        return row

    monkeypatch.setitem(sys.modules, "sitecustomize", SimpleNamespace(authorized_backend=phase))
    monkeypatch.setattr(backend, "run_one_current_multiturn", run_one)
    result = live_provider.call_api("Untrusted replacement prompt", {"config": {"campaign_dir": str(tmp_path)}},
                                    {"vars": {"task_id": row["id"]}})
    compact = json.loads(result["output"])
    assert calls == [(row["id"], tmp_path)]
    assert len(result["output"]) < 1000
    assert compact["backend_result_sha256"] == run_live.compact_digest(row)
    assert result["tokenUsage"] == {"prompt": 20, "completion": 5, "total": 25}
    monkeypatch.setattr(backend, "read_task_receipt", lambda *_: row)
    context = {"vars": {"task_id": row["id"]}, "test": {"metadata": {"campaign_dir": str(tmp_path)}}}
    assert live_assertion.get_assert(result["output"], context)["pass"] is True
    compact["backend_result_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="durable"):
        live_assertion.get_assert(json.dumps(compact), context)


def test_native_exclusion_is_not_an_official_zero():
    row = {"id": "multi_turn_base_173", "result": "blocked_dataset_conflict", "model_api_calls": 0}
    compact = {"id": row["id"], "result": row["result"], "main_score": None, "model_api_calls": 0,
               "judge_api_calls": 0, "backend_result_sha256": run_live.compact_digest(row)}
    native = {"provider": {"label": "current-aster-multiturn20"}, "vars": {"task_id": row["id"]},
              "response": {"output": json.dumps(compact)}, "error": "EXCLUDED blocked_dataset_conflict", "success": False}
    assert run_live.validate_native({"results": {"results": [native], "stats": {"errors": 1}}}, [row]) == []
    compact["main_score"] = 0
    native["response"]["output"] = json.dumps(compact)
    assert "unscorable_outcome_not_excluded" in run_live.validate_native(
        {"results": {"results": [native], "stats": {"errors": 1}}}, [row])


def test_live_guard_dns_bytes_and_scope_without_network(monkeypatch):
    monkeypatch.setenv("ASTER_PF_LIVE_ENDPOINTS", '[["api.example.invalid",443]]')
    monkeypatch.setattr(sys, "addaudithook", lambda _: None)
    monkeypatch.setattr(atexit, "register", lambda _: None)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.20", 443))])
    path = Path(run_live.__file__).parent / "live_guards/sitecustomize.py"
    spec = importlib.util.spec_from_file_location("test_live_network_guard", path)
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    with pytest.raises(PermissionError):
        guard._dns(b"api.example.invalid", 443)
    with guard.authorized_backend():
        assert guard._dns(b"api.example.invalid", 443)[0][4][0] == "203.0.113.20"
        guard._audit("socket.connect", (None, ("203.0.113.20", 443)))
        with pytest.raises(PermissionError):
            guard._audit("socket.connect", (None, ("203.0.113.20", 80)))
    with pytest.raises(PermissionError):
        guard._audit("socket.connect", (None, ("203.0.113.20", 443)))


@pytest.mark.bfcl_evidence
def test_preflight_preserves_twenty_frozen_tasks_and_complete_boundaries():
    require_corpus()
    plan = run_live.preflight()
    assert len(plan["groups"]) == 20
    assert len({group["group"] for group in plan["groups"]}) == 20
    assert all(group["user_turns"] > 1 for group in plan["groups"])
    assert [group["category"] for group in plan["groups"][:4]] == list(run_live.CATEGORIES)
    assert plan["known_admission_conflicts"] == ["multi_turn_base_173"]


@pytest.mark.bfcl_evidence
def test_supplement_selects_only_never_dispatched_tasks_with_remaining_budget(tmp_path):
    require_corpus()
    plan = run_live.preflight()
    identifiers = [group["group"] for group in plan["groups"]]
    source = tmp_path / "completed-initial"
    (source / "results").mkdir(parents=True)
    (source / "promptfoo").mkdir()
    paid_ids = [identifier for identifier in identifiers[:10] if identifier != "multi_turn_base_173"][:6]
    counts = dict(zip(paid_ids, (20, 20, 20, 20, 20, 18), strict=True))
    rows = [{"id": identifier, "result": "not_run" if index >= 10 else "pass",
             "model_api_calls": counts.get(identifier, 0)} for index, identifier in enumerate(identifiers)]
    rows[identifiers.index("multi_turn_miss_func_188")]["result"] = "case_error"
    report = {"max_api_calls": 200, "dataset_binding": "fixture", "model_api_calls": 118, "results": rows}
    values = {"campaign-plan.json": plan, "execution-started.json": {},
        "execution-finished.json": {"model_api_calls": 118, "http_transport_attempts": 118},
        "execution-control.json": {"max_api_calls": 200, "dataset_binding": "fixture"},
        "source-manifest.json": {"files": []}, "model-selection.json": {"model": "fixture-model"},
        "promptfoo/results.json": {}, "results/results.json": report,
        "results/http-budget.json": {"maximum": 200, "binding": "fixture", "calls": [
            {"case_id": identifier, "status": "response_headers_received"}
            for identifier, count in counts.items() for _ in range(count)]}}
    for name, value in values.items():
        (source / name).write_text(json.dumps(value), encoding="utf-8")
    selected = run_live.supplement_plan(source)
    assert selected["authorized_api_call_limit"] == 82
    assert selected["planned_entries"] == 10
    assert selected["supplement"]["selected_task_ids"] == identifiers[10:]
    assert "multi_turn_miss_func_188" not in selected["supplement"]["selected_task_ids"]
    assert selected["supplement"]["original_evidence_sha256"]["results/http-budget.json"]
    rows[-1]["model_api_calls"] = 1
    (source / "results/results.json").write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError, match="accounting"):
        run_live.supplement_plan(source)
    rows[-1]["model_api_calls"] = 0
    rows[-1]["runs"] = [{"turn_index": 0}]
    (source / "results/results.json").write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError, match="previously entered"):
        run_live.supplement_plan(source)
