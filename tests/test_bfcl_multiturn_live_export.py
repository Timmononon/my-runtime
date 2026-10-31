import copy
import hashlib
import json

import pytest
from test_bfcl_multiturn_export import campaign, save

from aster.evaluation.bfcl_multiturn_export import load_bundle, write_bundle
from aster.evaluation.bfcl_multiturn_live_export import load_live_campaign, merge_uninvoked_campaigns


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def suffix(identifier):
    return hashlib.sha256(identifier.encode()).hexdigest()[:24]


def sync_row(root, row):
    report = read(root / "results/results.json")
    report["results"] = [row if item["id"] == row["id"] else item for item in report["results"]]
    save(root / "results/results.json", report)
    save(root / f"results/receipts/{suffix(row['id'])}.json", {"dataset_binding": report["dataset_binding"], "result": row})


def make_infra(tmp_path):
    root = campaign(tmp_path)
    report = read(root / "results/results.json")
    report["model_api_calls"] = 2
    save(root / "results/results.json", report)
    row = report["results"][0]
    core_path = root / f"results/cases/{suffix(row['id'])}/result.json"
    core = read(core_path)
    core.pop("result")  # Actual core reports predate the outer driver result.
    save(core_path, core)
    row.update(status="case_error", result="case_error", model_quality="unsupported",
               model_failures=[{"type": "ProviderError", "category": "incomplete"}])
    core["model_quality"] = "evaluated"
    save(core_path, core)
    sync_row(root, row)
    return root, core_path, row


def make_pair(tmp_path):
    original = campaign(tmp_path / "initial")
    report, ledger, plan = [read(original / name) for name in (
        "results/results.json", "results/http-budget.json", "campaign-plan.json")]
    pending = {"id": "multi_turn_base_2", "status": "not_run", "result": "not_run", "model_quality": "unsupported",
               "model_api_calls": 0, "case_provenance": copy.deepcopy(report["results"][0]["case_provenance"])}
    report["results"].append(pending)
    report.update(model_api_calls=2, max_api_calls=200)
    save(original / "results/results.json", report)
    save(original / f"results/receipts/{suffix(pending['id'])}.json", {"dataset_binding": "fixture-binding", "result": pending})
    plan["groups"].append({"entry_ids": [pending["id"]], "category": "multi_turn_base", "user_turns": 2,
                           "source_kind": "official_fixed_revision_complete_task"})
    save(original / "campaign-plan.json", plan)
    ledger["maximum"] = 200
    save(original / "results/http-budget.json", ledger)
    child = campaign(tmp_path / "child")
    child_report = read(child / "results/results.json")
    row = child_report["results"][0]
    row["id"] = pending["id"]
    child_report.update(model_api_calls=2, max_api_calls=198)
    save(child / "results/results.json", child_report)
    save(child / f"results/receipts/{suffix(row['id'])}.json", {"dataset_binding": "fixture-binding", "result": row})
    save(child / f"results/cases/{suffix(row['id'])}/result.json", row)
    child_http = read(child / "results/http-budget.json")
    for call in child_http["calls"]:
        call["case_id"] = row["id"]
    child_http["maximum"] = 198
    save(child / "results/http-budget.json", child_http)
    child_plan = read(child / "campaign-plan.json")
    child_plan["groups"][0]["entry_ids"] = [row["id"]]
    child_plan["supplement"] = {"original_campaign": str(original), "original_dataset_binding": "fixture-binding",
        "original_actual_http_attempts": 2, "remaining_api_call_limit": 198, "selected_task_ids": [row["id"]],
        "original_evidence_sha256": {"results/results.json": hashlib.sha256((original / "results/results.json").read_bytes()).hexdigest()}}
    save(child / "campaign-plan.json", child_plan)
    return original, child


def test_narrow_quality_override_preserves_original_core_and_unknown_score(tmp_path):
    root, core_path, _row = make_infra(tmp_path)
    before = core_path.read_bytes()
    record = load_live_campaign(root)[0]
    assert record["scoring"]["passed"] is None
    assert record["outcome"]["category"] == "infrastructure_error"
    assert record["provenance"]["core_driver_overrides"][0]["field"] == "model_quality"
    assert core_path.read_bytes() == before
    assert record["provenance"]["evidence_sha256"][core_path.relative_to(root).as_posix()] == hashlib.sha256(before).hexdigest()


def test_quality_override_cannot_hide_another_core_difference(tmp_path):
    root, core_path, _row = make_infra(tmp_path)
    core = read(core_path)
    core["protocol_result"] = "unknown-mutated"
    save(core_path, core)
    with pytest.raises(ValueError, match="Unexpected core/driver"):
        load_live_campaign(root)


def test_quality_override_needs_typed_nonbudget_provider_failure(tmp_path):
    root, _core_path, row = make_infra(tmp_path)
    row["model_failures"] = [{"type": "AsterRuntimeBudgetStop", "category": "budget"}]
    sync_row(root, row)
    with pytest.raises(ValueError, match="Unexpected core/driver"):
        load_live_campaign(root)


def test_merge_replaces_only_never_dispatched_row_and_roundtrips_bundle(tmp_path):
    original, child = make_pair(tmp_path)
    records, report = merge_uninvoked_campaigns(original, child)
    assert len(records) == 2
    assert [row["config_label"] for row in records] == ["initial-as-run", "supplement-as-run"]
    assert records[1]["provenance"]["replaced_uninvoked_parent_record_sha256"]
    assert report["total_model_http_attempts"] == 4
    assert report["native_promptfoo_provider_invocations"] == 3
    assert report["not_an_optimization_comparison"]
    write_bundle(records, tmp_path / "bundle")
    assert load_bundle(tmp_path / "bundle") == records


def test_merge_rejects_changed_parent_proof(tmp_path):
    original, child = make_pair(tmp_path)
    with (original / "results/results.json").open("a", encoding="utf-8") as stream:
        stream.write("\n")
    with pytest.raises(ValueError, match="Original evidence changed"):
        merge_uninvoked_campaigns(original, child)


def test_merge_rejects_supplement_budget_reset(tmp_path):
    original, child = make_pair(tmp_path)
    ledger = read(child / "results/http-budget.json")
    ledger["maximum"] = 200
    save(child / "results/http-budget.json", ledger)
    with pytest.raises(ValueError, match="shared 200"):
        merge_uninvoked_campaigns(original, child)


def test_merge_rejects_changed_question_even_when_ids_match(tmp_path):
    original, child = make_pair(tmp_path)
    row = read(child / "results/results.json")["results"][0]
    row["case_provenance"]["question_sha256"] = "different-question"
    sync_row(child, row)
    save(child / f"results/cases/{suffix(row['id'])}/result.json", row)
    with pytest.raises(ValueError, match="model/question/category"):
        merge_uninvoked_campaigns(original, child)


def test_merge_rejects_repeating_a_previously_dispatched_task(tmp_path):
    original, child = make_pair(tmp_path)
    original_report = read(original / "results/results.json")
    row = original_report["results"][1]
    row["result"] = "case_error"
    row["status"] = "case_error"
    row["model_api_calls"] = 1
    sync_row(original, row)
    http = read(original / "results/http-budget.json")
    http["calls"].append({"case_id": row["id"], "status_code": 200})
    save(original / "results/http-budget.json", http)
    original_report = read(original / "results/results.json")
    original_report["model_api_calls"] = 3
    save(original / "results/results.json", original_report)
    child_plan = read(child / "campaign-plan.json")
    child_plan["supplement"].update(original_actual_http_attempts=3, remaining_api_call_limit=197,
        original_evidence_sha256={"results/results.json": hashlib.sha256((original / "results/results.json").read_bytes()).hexdigest()})
    save(child / "campaign-plan.json", child_plan)
    child_http = read(child / "results/http-budget.json")
    child_http["maximum"] = 197
    save(child / "results/http-budget.json", child_http)
    with pytest.raises(ValueError, match="previously entered"):
        merge_uninvoked_campaigns(original, child)
