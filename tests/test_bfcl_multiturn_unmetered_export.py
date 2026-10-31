import json

import pytest
from test_bfcl_multiturn_export import campaign, save
from test_bfcl_multiturn_live_export import make_infra, suffix, sync_row

from aster.evaluation.bfcl_multiturn_export import load_bundle, write_bundle
from aster.evaluation.bfcl_multiturn_unmetered_export import campaign_summary, load_unmetered_campaign


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def unmetered(root):
    report, plan, ledger = [read(root / name) for name in (
        "results/results.json", "campaign-plan.json", "results/http-budget.json")]
    profile = {"provider_id": "deepseek", "protocol": "deepseek_responses", "model": "deepseek-flash",
               "reasoning_effort": "none", "identity_revision": 1}
    bounds = {identifier: 20 * group["user_turns"] for group in plan["groups"] for identifier in group["entry_ids"]}
    report.update(model_api_calls=len(ledger["calls"]), max_api_calls=None, authorized_unlimited_api=True,
                  api_unlimited=True, profile=profile, max_output_tokens=8192, case_official_step_bounds=bounds,
                  inherited_runtime_safety={"duplicate_limit": 3})
    ledger.update(maximum=None, authorized_unlimited_api=True, api_unlimited=True, case_caps=dict.fromkeys(bounds),
                  case_official_step_bounds=bounds, wire_max_output_tokens=8192, official_max_steps_per_turn=20)
    for call in ledger["calls"]:
        call.update(thinking_disabled_verified=True, wire_model="deepseek-flash", wire_reasoning_effort="none",
                    wire_max_output_tokens=8192, method="POST", path="/responses", status="response_headers_received")
    plan.update(model="deepseek-flash", reasoning_effort="none", authorized_unlimited_api=True,
                authorized_api_call_limit=None)
    control = {"dataset_binding": report["dataset_binding"], "max_api_calls": None,
               "authorized_unlimited_api": True, "api_unlimited": True,
               "per_case_maximum": None, "future_valid_case_reserve": 0, "case_official_step_bounds": bounds,
               "official_max_steps_per_turn": 20, "inherited_runtime_safety": {"duplicate_limit": 3}}
    save(root / "execution-control.json", control)
    save(root / "campaign-plan.json", plan)
    save(root / "results/http-budget.json", ledger)
    save(root / "results/results.json", report)
    return root


def test_strict_unmetered_export_roundtrips_full_task_and_original_core(tmp_path):
    root = unmetered(campaign(tmp_path))
    row = read(root / "results/results.json")["results"][0]
    core = root / f"results/cases/{suffix(row['id'])}/result.json"
    original = core.read_bytes()
    records = load_unmetered_campaign(root)
    assert len(records) == 1 and len(records[0]["turns"]) == 2
    assert records[0]["model_identity"]["reasoning_effort"] == "none"
    assert records[0]["provenance"]["allocated_api_call_cap"] is None
    assert records[0]["provenance"]["official_model_step_bound"] == 40
    assert core.read_bytes() == original
    write_bundle(records, tmp_path / "bundle")
    assert load_bundle(tmp_path / "bundle") == records
    report = campaign_summary(root, records)
    assert report["actual_model_http_attempts"] == 2 and report["api_call_limit"] is None
    assert report["new_model_api_calls_during_export"] == report["new_judge_api_calls"] == 0


@pytest.mark.parametrize("kind", ["quota", "missing_maximum", "thinking", "model", "witness", "step_bound", "task_cap"])
def test_changed_no_thinking_or_unmetered_policy_is_rejected(tmp_path, kind):
    root = unmetered(campaign(tmp_path))
    ledger = read(root / "results/http-budget.json")
    if kind == "quota":
        ledger["maximum"] = 200
    elif kind == "missing_maximum":
        ledger.pop("maximum")
    elif kind == "thinking":
        ledger["calls"][0]["wire_reasoning_effort"] = "low"
    elif kind == "model":
        ledger["calls"][0]["wire_model"] = "different-model"
    elif kind == "witness":
        ledger["calls"][0].pop("thinking_disabled_verified")
    elif kind == "step_bound":
        ledger["case_official_step_bounds"]["multi_turn_base_1"] = 20
    else:
        ledger["case_caps"]["multi_turn_base_1"] = 20
    save(root / "results/http-budget.json", ledger)
    with pytest.raises(ValueError):
        load_unmetered_campaign(root)


def test_unmetered_unknown_usage_stays_unknown_and_not_new_billing(tmp_path):
    root = unmetered(campaign(tmp_path, unknown_usage=True))
    records = load_unmetered_campaign(root)
    assert records[0]["recorded_usage"]["input_tokens"] is None
    report = campaign_summary(root, records)
    assert report["unknown_paid_response_usage"] == 2 and report["token_subtotal_is_complete"] is False
    assert report["new_model_api_calls_during_export"] == 0


def test_only_typed_driver_quality_override_is_allowed(tmp_path):
    root, core_path, row = make_infra(tmp_path)
    unmetered(root)
    before = core_path.read_bytes()
    record = load_unmetered_campaign(root)[0]
    assert record["scoring"]["passed"] is None and record["outcome"]["category"] == "infrastructure_error"
    assert record["provenance"]["core_driver_overrides"][0]["field"] == "model_quality"
    assert core_path.read_bytes() == before
    row["model_failures"] = [{"type": "AsterRuntimeBudgetStop", "category": "budget"}]
    sync_row(root, row)
    with pytest.raises(ValueError, match="Unexpected core/driver"):
        load_unmetered_campaign(root)


def test_changed_formal_score_and_incomplete_plan_are_rejected(tmp_path):
    root = unmetered(campaign(tmp_path))
    report = read(root / "results/results.json")
    report["results"][0]["scoring_verification"]["valid"] = False
    save(root / "results/results.json", report)
    with pytest.raises(ValueError):
        load_unmetered_campaign(root)


def test_original_evidence_never_contains_injected_provider_credentials(tmp_path):
    root = unmetered(campaign(tmp_path))
    record = load_unmetered_campaign(root)[0]
    encoded = json.dumps(record)
    assert "fixture-secret" not in encoded and "never-export-this" not in encoded and "opaque" not in encoded
    assert record["replay"]["model_capability_measurement"] is False


def test_omitted_planned_task_is_not_silently_removed(tmp_path):
    root = unmetered(campaign(tmp_path))
    plan = read(root / "campaign-plan.json")
    group = dict(plan["groups"][0])
    group["entry_ids"] = ["multi_turn_base_2"]
    plan["groups"].append(group)
    save(root / "campaign-plan.json", plan)
    for name, key in (("results/results.json", "case_official_step_bounds"),
                      ("execution-control.json", "case_official_step_bounds"),
                      ("results/http-budget.json", "case_official_step_bounds")):
        data = read(root / name)
        data[key]["multi_turn_base_2"] = 40
        if name == "results/http-budget.json":
            data["case_caps"]["multi_turn_base_2"] = None
        save(root / name, data)
    with pytest.raises(ValueError, match="omitted planned task"):
        load_unmetered_campaign(root)
