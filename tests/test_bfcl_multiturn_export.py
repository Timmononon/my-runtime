from __future__ import annotations

import copy
import hashlib
import json

import pytest

from aster.evaluation.bfcl_multiturn_export import (
    SUBCATEGORIES,
    classify_outcome,
    load_bundle,
    load_saved_campaign,
    summarize_records,
    validate_record,
    write_bundle,
)


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def campaign(tmp_path, *, fail=False, count=2, category="multi_turn_base", unknown_usage=False):
    root = tmp_path / "campaign"
    source = root / "as-run-source/src/aster/example.py"
    source.parent.mkdir(parents=True)
    source.write_text("# immutable example\n", encoding="utf-8")
    save(root / "source-manifest.json", {"files": [{"file": "src/aster/example.py", "bytes": source.stat().st_size,
        "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}]})
    task_id = category + "_1"
    check = {"valid": not fail, "result": "fail" if fail else "pass",
             "scoring_rule": "bfcl_v4_pinned_official_checker", "source_revision": "fixture-revision"}
    runs = [{"turn_index": i, "run_id": f"run{i}", "runtime_status": "failed" if fail else "completed", "answer": "done"}
            for i in range(count)]
    messages = []
    for _i in range(count):
        messages.extend([{"role": "user", "content": "Fixture password fixture-secret; help"},
                         {"role": "assistant", "content": "done", "provider_items": [{"secret": "opaque"}]}])
    row = {"id": task_id, "model_api_calls": count, "result": "fail" if fail else "pass",
           "protocol_result": "fail" if fail else "pass", "scoring_verification": {
               "valid": not fail, "result": "fail" if fail else "pass", "turns": [check for _ in range(count)]},
           "forced_termination": fail, "runs": runs, "messages": messages,
           "tool_ledger": [{"run_id": "run0", "tool_name": "authenticate", "tool_call_id": "call0",
                            "arguments": {"password": "fixture-secret"}, "state": "checkpointed", "result": {"content": "ok"}}],
           "usage": [{"input_tokens": None if unknown_usage else 10,
                      "output_tokens": None if unknown_usage else 2} for _ in range(count)],
           "elapsed_seconds": None, "budget": {"limits": {"max_requests": 10}},
           "case_provenance": {"revision": "fixture-revision", "question_sha256": "fixture-question"}}
    if fail:
        row["model_failures"] = [{"category": "budget", "type": "ProviderError"}]
    report = {"mode": "live_current_model", "dataset_binding": "fixture-binding", "results": [row],
              "profile": {"provider_id": "fixture", "model": "fixture", "api_key": "never-export-this"},
              "case_api_caps": {task_id: 10}, "case_groups": {task_id: task_id}, "max_api_calls": 10}
    save(root / "results/results.json", report)
    save(root / "results/http-budget.json", {"binding": "fixture-binding", "calls": [
        {"case_id": task_id, "status_code": 200} for _ in range(count)]})
    save(root / "campaign-plan.json", {"groups": [{"entry_ids": [task_id], "category": category,
         "source_kind": "official_fixed_revision_complete_task", "user_turns": 3 if fail else count}]})
    suffix = hashlib.sha256(task_id.encode()).hexdigest()[:24]
    save(root / f"results/receipts/{suffix}.json", {"dataset_binding": "fixture-binding", "result": row})
    save(root / f"results/cases/{suffix}/result.json", row)
    return root


@pytest.mark.parametrize("category", SUBCATEGORIES)
def test_one_complete_task_not_one_case_per_turn(tmp_path, category):
    records = load_saved_campaign(campaign(tmp_path, count=3, category=category), config_label="actual-config")
    assert len(records) == 1
    row = records[0]
    assert row["subcategory"] == category
    assert len(row["turns"]) == 3
    assert row["recorded_usage"]["model_requests"] == 3
    assert row["scoring"]["passed"] is True
    assert row["replay"]["new_model_api_calls"] == 0
    assert row["replay"]["model_capability_measurement"] is False


def test_stopped_task_retains_planned_turns_and_formal_failure(tmp_path):
    record = load_saved_campaign(campaign(tmp_path, count=1, fail=True, category="multi_turn_miss_func"))[0]
    assert len(record["turns"]) == 3
    assert [turn["runtime_status"] for turn in record["turns"]] == ["failed", "not_run", "not_run"]
    assert record["provenance"]["executed_user_turns"] == 1
    assert record["provenance"]["expected_user_turns"] == 3
    assert record["scoring"]["passed"] is False
    assert record["outcome"]["category"] == "budget_terminated"


def test_allowlist_redacts_authentication_and_excludes_provider_secrets(tmp_path):
    record = load_saved_campaign(campaign(tmp_path))[0]
    text = json.dumps(record)
    assert "fixture-secret" not in text
    assert "never-export-this" not in text
    assert "opaque" not in text
    assert "[REDACTED]" in text
    assert record["turns"][0]["tool_executions"][0]["arguments"]["password"] == "[REDACTED]"


def test_bundle_hashes_and_record_coherence(tmp_path):
    records = load_saved_campaign(campaign(tmp_path))
    output = tmp_path / "bundle"
    manifest = write_bundle(records, output)
    assert manifest["task_count"] == 1
    assert load_bundle(output) == records
    with pytest.raises(FileExistsError):
        write_bundle(records, output)
    with (output / "records.jsonl").open("a", encoding="utf-8") as stream:
        stream.write("\n")
    with pytest.raises(ValueError, match="integrity"):
        load_bundle(output)


def test_record_digest_rejects_modified_metric(tmp_path):
    record = load_saved_campaign(campaign(tmp_path))[0]
    record["recorded_usage"]["model_requests"] = 999
    with pytest.raises(ValueError, match="digest"):
        validate_record(record)


def test_missing_usage_and_duration_remain_unknown(tmp_path):
    records = load_saved_campaign(campaign(tmp_path, unknown_usage=True))
    usage = records[0]["recorded_usage"]
    assert usage["input_tokens"] is None
    assert usage["elapsed_seconds"] is None
    assert usage["unknown_paid_response_usage"] == 2
    summary = summarize_records(records)
    assert summary["groups"][0]["recorded_usage_totals"]["input_tokens"] is None
    assert summary["groups"][0]["recorded_usage_totals"]["elapsed_seconds"] is None
    assert summary["new_model_capability_score"] is None


def test_actual_source_snapshot_tamper_is_rejected(tmp_path):
    root = campaign(tmp_path)
    (root / "as-run-source/src/aster/example.py").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="source integrity"):
        load_saved_campaign(root)


def test_manifest_path_escape_is_rejected(tmp_path):
    root = campaign(tmp_path)
    manifest = json.loads((root / "source-manifest.json").read_text(encoding="utf-8"))
    manifest["files"][0]["file"] = "../escape"
    save(root / "source-manifest.json", manifest)
    with pytest.raises(ValueError, match="relative"):
        load_saved_campaign(root)


def test_result_cannot_be_changed_without_receipt_mismatch(tmp_path):
    root = campaign(tmp_path)
    report = json.loads((root / "results/results.json").read_text(encoding="utf-8"))
    report["results"][0]["result"] = "fail"
    save(root / "results/results.json", report)
    with pytest.raises(ValueError, match="receipt"):
        load_saved_campaign(root)


def test_user_label_does_not_change_configuration_identity(tmp_path):
    root = campaign(tmp_path)
    left = load_saved_campaign(root, config_label="first-tag")
    right = load_saved_campaign(root, config_label="second-tag")
    assert left[0]["config_identity"] == right[0]["config_identity"]
    assert left[0]["source_identity"] == right[0]["source_identity"]
    summary = summarize_records(left + right)
    assert len(summary["groups"]) == 2
    assert summary["optimization_gain_computed"] is False
    with pytest.raises(ValueError, match="Duplicate"):
        write_bundle(left + left, tmp_path / "duplicate-bundle")


@pytest.mark.parametrize(("row", "expected"), [
    ({"result": "pass"}, "pass"),
    ({"result": "fail"}, "task_failed"),
    ({"result": "fail", "model_failures": [{"category": "budget"}]}, "budget_terminated"),
    ({"status": "blocked_dataset_conflict"}, "dataset_conflict"),
    ({"status": "case_error", "model_failures": [{"category": "transport"}]}, "infrastructure_error"),
    ({"status": "not_run"}, "not_run"),
])
def test_failure_taxonomy_separates_scoring_and_stop_cause(row, expected):
    assert classify_outcome(copy.deepcopy(row))["category"] == expected


def test_replay_cannot_claim_model_or_judge_calls(tmp_path):
    record = load_saved_campaign(campaign(tmp_path))[0]
    record["replay"]["new_judge_api_calls"] = 1
    with pytest.raises(ValueError, match="cannot claim"):
        validate_record(record)


def test_no_network_provider_or_default_runtime_reads(tmp_path, monkeypatch):
    import socket

    def deny(*_args, **_kwargs):
        raise AssertionError("Network forbidden in saved-result export")

    monkeypatch.setattr(socket, "getaddrinfo", deny)
    monkeypatch.setattr(socket.socket, "connect", deny)
    root = campaign(tmp_path)
    records = load_saved_campaign(root)
    write_bundle(records, tmp_path / "offline-bundle")
    assert load_bundle(tmp_path / "offline-bundle") == records
