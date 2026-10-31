"""Offline admission, source integrity and native held-out metric contracts.

Synthetic metric fixtures use already exposed saved development records. They
never infer held-out outcomes or invoke a model.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from copy import deepcopy
from pathlib import Path

import pytest
from bfcl_test_prerequisites import require_campaign, require_frozen_runtimes

from aster.evaluation.bfcl_multiturn_export import _digest, validate_record
from evals.bfcl_multiturn.deepeval import comparison_data as saved
from evals.bfcl_multiturn.deepeval import holdout_comparison_data as data
from evals.bfcl_multiturn.deepeval.holdout_comparison_worker import evaluate_campaign

PROJECT = Path(__file__).resolve().parents[1]
SPLIT = PROJECT / saved.SPLIT_RELATIVE
IDS = (SPLIT / "holdout-ids.txt").read_text(encoding="utf-8").splitlines()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def sha(payload):
    return hashlib.sha256(payload).hexdigest()


@pytest.fixture
def membership(tmp_path):
    target = tmp_path / "as-run-source" / saved.SPLIT_RELATIVE
    target.mkdir(parents=True)
    for name in ("split-manifest.json", "development-ids.txt", "holdout-ids.txt"):
        shutil.copyfile(SPLIT / name, target / name)
    plan = {"scope": "compact100-v2", "evaluation_split": "holdout", "evaluation_task_count": 60,
            "planned_entries": 60, "groups": [{"entry_ids": [task_id]} for task_id in IDS]}
    control = {"case_ids": IDS.copy()}
    return tmp_path, plan, control, {"evaluation_split": "holdout"}


def test_membership_accepts_exact_sixty_and_fifteen_per_category(membership):
    assert data.validate_membership(*membership) == IDS


@pytest.mark.parametrize("change", ["dev", "order", "duplicate", "missing", "scope"])
def test_membership_rejects_relabelled_or_changed_selection(membership, change):
    root, plan, control, report = membership
    if change == "dev":
        plan["evaluation_split"] = "development"
    elif change == "order":
        control["case_ids"] = list(reversed(IDS))
    elif change == "duplicate":
        plan["groups"][0]["entry_ids"] = [IDS[1]]
    elif change == "missing":
        plan["groups"].pop()
    else:
        plan["evaluation_task_count"] = 80
    with pytest.raises(ValueError):
        data.validate_membership(root, plan, control, report)


def test_membership_refuses_changed_authority_bytes(membership):
    root, plan, control, report = membership
    path = root / "as-run-source" / saved.SPLIT_RELATIVE / "holdout-ids.txt"
    path.write_text("\n".join(reversed(IDS)) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="authority"):
        data.validate_membership(root, plan, control, report)


def test_unsealed_campaign_is_refused_before_mutable_control_read(tmp_path):
    write(tmp_path / "execution-control.json", {"do_not_read": True})
    with pytest.raises(ValueError, match="sealed"):
        data.load_campaign(tmp_path, label="A0")


@pytest.fixture(scope="module")
def frozen_runtime(tmp_path_factory):
    require_frozen_runtimes()
    root = tmp_path_factory.mktemp("s")
    expected = data.CONFIGURATIONS["A0"]
    original = PROJECT / expected["historical_campaign"]
    historical = json.loads((original / "source-manifest.json").read_bytes())
    shared_root = PROJECT / data.CONFIGURATIONS["MF-Gate-v1"]["historical_campaign"]
    shared = json.loads((shared_root / "source-manifest.json").read_bytes())
    entries = {row["file"]: row for row in historical["files"] if row["file"].startswith("src/aster/")}
    shared_entries = {row["file"]: row for row in shared["files"]}
    entries.update({name: shared_entries[name] for name in data.SHARED_BACKENDS})
    for name in entries:
        origin = shared_root if name in data.SHARED_BACKENDS else original
        target = root / "as-run-source" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(origin / "as-run-source" / name, target)
    origin_name, shared_name = "frozen-origin-source-manifest.json", "shared-infrastructure-source-manifest.json"
    for name, source in ((origin_name, original / "source-manifest.json"),
                         (shared_name, shared_root / "source-manifest.json")):
        target = root / "as-run-source" / name
        shutil.copyfile(source, target)
        entries[name] = {"file": name, "sha256": sha(target.read_bytes()), "bytes": target.stat().st_size}
    project_root = root / "no-longer-present-original-workspace"
    frozen = {"schema_version": 1, "configuration_label": "A0", "source_commit": expected["commit"],
        "historical_source_manifest_sha256": expected["historical_manifest"], "project_root": str(project_root),
        "frozen_files": [row for name, row in entries.items()
                         if name.startswith("src/aster/") and name not in data.SHARED_BACKENDS],
        "overlays": [entries[name] for name in sorted(data.SHARED_BACKENDS)]}
    frozen_path = root / "as-run-source/frozen-runtime-manifest.json"
    write(frozen_path, frozen)
    entries["frozen-runtime-manifest.json"] = {"file": "frozen-runtime-manifest.json",
        "sha256": sha(frozen_path.read_bytes()), "bytes": frozen_path.stat().st_size}
    write(root / "source-manifest.json", {"files": list(entries.values())})
    proof = {"configuration_label": "A0", "source_commit": expected["commit"], "project_root": str(project_root),
             "modules": []}
    for module in sorted({"aster", *data.CORE_MODULES}):
        name = "src/" + module.replace(".", "/") + ("/__init__.py" if module == "aster" else ".py")
        proof["modules"].append({"module": module, "relative_file": name,
            "origin": str(project_root / name), "sha256": entries[name]["sha256"]})
    proof_path = root / "runtime-import-identity.json"
    write(proof_path, proof)
    proof_hash = sha(proof_path.read_bytes())
    write(root / "execution-control.json", {"case_ids": IDS})
    for index, task_id in enumerate(IDS):
        for phase in ("started", "finished"):
            write(root / f"promptfoo/dispatches/{index}.{phase}.json", {
                "task_id": task_id, "runtime_import_identity_sha256": proof_hash})
    return root


@pytest.mark.bfcl_evidence
def test_frozen_source_guard_uses_historical_authority_not_current_label(frozen_runtime):
    result = data.validate_frozen_runtime(frozen_runtime, configuration="A0")
    assert result["source_commit"] == data.CONFIGURATIONS["A0"]["commit"]
    assert result["verified_runtime_files"] > 200
    assert result["callback_import_witnesses"] == 60


@pytest.mark.bfcl_evidence
def test_frozen_source_guard_rejects_other_configuration(frozen_runtime):
    with pytest.raises(ValueError, match="historical configuration"):
        data.validate_frozen_runtime(frozen_runtime, configuration="A3")


@pytest.mark.bfcl_evidence
def test_frozen_source_guard_rejects_self_consistent_engine_rewrite(frozen_runtime, tmp_path_factory):
    root = tmp_path_factory.mktemp("r")
    shutil.copytree(frozen_runtime, root, dirs_exist_ok=True)
    name = "src/aster/engine/loop.py"
    target = root / "as-run-source" / name
    target.write_bytes(target.read_bytes() + b"\n# synthetic tamper\n")
    source = json.loads((root / "source-manifest.json").read_bytes())
    row = next(item for item in source["files"] if item["file"] == name)
    row.update(sha256=sha(target.read_bytes()), bytes=target.stat().st_size)
    frozen_path = root / "as-run-source/frozen-runtime-manifest.json"
    frozen = json.loads(frozen_path.read_bytes())
    next(item for item in frozen["frozen_files"] if item["file"] == name).update(row)
    write(frozen_path, frozen)
    next(item for item in source["files"] if item["file"] == "frozen-runtime-manifest.json").update(
        sha256=sha(frozen_path.read_bytes()), bytes=frozen_path.stat().st_size)
    write(root / "source-manifest.json", source)
    with pytest.raises(ValueError, match="historical version"):
        data.validate_frozen_runtime(root, configuration="A0")


@pytest.mark.bfcl_evidence
def test_frozen_source_guard_rejects_missing_callback(frozen_runtime, tmp_path_factory):
    root = tmp_path_factory.mktemp("c")
    shutil.copytree(frozen_runtime, root, dirs_exist_ok=True)
    (root / "promptfoo/dispatches/0.started.json").unlink()
    with pytest.raises(ValueError, match="callback"):
        data.validate_frozen_runtime(root, configuration="A0")


@pytest.mark.parametrize("change", ["root", "hash", "missing", "alias", "module_file"])
@pytest.mark.bfcl_evidence
def test_import_proof_requires_actual_bound_module_origins(frozen_runtime, change):
    proof = json.loads((frozen_runtime / "runtime-import-identity.json").read_bytes())
    source = {item["file"]: item for item in json.loads((frozen_runtime / "source-manifest.json").read_bytes())["files"]}
    original_root = proof["project_root"]
    if change == "root":
        proof["modules"][0]["origin"] = str(frozen_runtime / "unrelated-loop.py")
    elif change == "hash":
        proof["modules"][0]["sha256"] = "0" * 64
    elif change == "missing":
        proof["modules"].pop()
    elif change == "alias":
        proof["modules"][0]["file"] = "src/aster/unrelated.py"
    else:
        proof["modules"][0]["relative_file"] = "src/aster/paths.py"
        proof["modules"][0]["origin"] = str(Path(original_root) / "src/aster/paths.py")
        proof["modules"][0]["sha256"] = source["src/aster/paths.py"]["sha256"]
    with pytest.raises(ValueError):
        data.validate_import_proof(proof, configuration="A0", sources=source, project_root=original_root)


@pytest.fixture(scope="module")
def old_baseline():
    require_campaign("compact-dev40-c20-20261030")
    return saved.load_campaign(PROJECT / "artifacts/bfcl/compact-dev40-c20-20261030", label="A0")


def synthetic_holdout(old_baseline, label, root):
    campaign = deepcopy(old_baseline)
    campaign.update(label=label, root=str(root))
    campaign["identity"].update(evaluation_split="holdout", evaluation_task_count=60,
                                agent_policy=data.CONFIGURATIONS[label]["policy"])
    original = old_baseline["records"][0]
    original_row = old_baseline["rows"][0]
    records, rows, references = [], [], {}
    for task_id in IDS:
        record = deepcopy(original)
        record.update(task_id=task_id, config_label=label,
            subcategory=next(category for category in saved.SUBCATEGORIES if task_id.startswith(category + "_")))
        record["config_identity"]["snapshot"].update(agent_policy=campaign["identity"]["agent_policy"])
        record["config_identity"]["sha256"] = _digest(record["config_identity"]["snapshot"])
        record["record_sha256"] = _digest({key: value for key, value in record.items() if key != "record_sha256"})
        validate_record(record)
        records.append(record)
        row = deepcopy(original_row)
        row["id"] = task_id
        rows.append(row)
        references[task_id] = old_baseline["reference_call_counts"][original["task_id"]]
    campaign.update(records=records, rows=rows, reference_call_counts=references)
    campaign["summary"] = saved.summarize_campaign(records, campaign["report"])
    return campaign


def replace_result(record, passed):
    record["scoring"]["passed"] = passed
    record["scoring"]["formal_result"] = "case_error" if passed is None else "pass" if passed else "fail"
    record["outcome"]["category"] = "infrastructure_error" if passed is None else "pass" if passed else "task_failed"
    record["record_sha256"] = _digest({key: value for key, value in record.items() if key != "record_sha256"})
    validate_record(record)


@pytest.mark.bfcl_evidence
def test_pairing_requires_heldout_scope_not_a_development_relabel(old_baseline):
    with pytest.raises(ValueError, match="sixty-task"):
        data.compare_campaigns(old_baseline, deepcopy(old_baseline))


@pytest.mark.bfcl_evidence
def test_holdout_pairing_retains_unknown_and_budget_classification(old_baseline, tmp_path):
    before = synthetic_holdout(old_baseline, "A0", tmp_path / "before")
    after = synthetic_holdout(old_baseline, "A3", tmp_path / "after")
    replace_result(after["records"][0], None)
    replace_result(after["records"][1], False)
    after["records"][1]["outcome"]["category"] = "budget_terminated"
    after["records"][1]["record_sha256"] = _digest({key: value for key, value in after["records"][1].items()
                                                   if key != "record_sha256"})
    after["summary"] = saved.summarize_campaign(after["records"], after["report"])
    result = data.compare_campaigns(before, after)
    assert result["unique_tasks"] == 60
    assert result["paired_scorable_tasks"] == 59
    assert result["main_success_rate_delta"] is None
    assert result["transitions"]["unscorable_pair"] == [IDS[0]]
    assert result["transitions"]["regressed"] == [IDS[1]]
    assert result["evaluation_split"] == "holdout"
    assert result["new_holdout_inference_tasks"] == 0


@pytest.mark.bfcl_evidence
def test_actual_native_framework_does_not_turn_unknown_into_failure(old_baseline, tmp_path):
    campaign = synthetic_holdout(old_baseline, "A0", tmp_path / "synthetic-saved-source")
    replace_result(campaign["records"][0], None)
    replace_result(campaign["records"][1], False)
    campaign["summary"] = saved.summarize_campaign(campaign["records"], campaign["report"])
    write(Path(campaign["root"]) / "results/results.json", {"synthetic_fixture": True})
    result = evaluate_campaign(campaign, tmp_path / "native-output")
    assert result["native_main_case_count"] == 59
    assert result["native_main_pass_count"] == 58
    assert result["native_main_fail_count"] == 1
    assert result["saved_result_summary"]["main_success_rate"] is None
    assert result["excluded"][0]["task_id"] == IDS[0]
    assert result["judge_enabled"] is False
    assert result["new_model_api_calls"] == result["new_judge_api_calls"] == 0
    assert result["gate_projection_cross_check"]["native_cases_verified"] == 59


@pytest.mark.bfcl_evidence
def test_a3_control_contract_failure_is_zero_but_runtime_failure_is_null(old_baseline):
    rows = [{"id": old_baseline["records"][0]["task_id"], "result": "fail", "agent_control_failure": True,
             "runs": [{"runtime_status": "failed", "agent_control_failure": True,
                       "runtime_diagnostic": {"error_type": "TaskControlError"}}]}]
    record = deepcopy(old_baseline["records"][0])
    replace_result(record, False)
    result = saved.normalize_runtime_outcomes([record], rows)
    assert result[0]["scoring"]["passed"] is False
    assert result[0]["outcome"]["category"] == "task_failed"
    rows[0]["runs"][0]["runtime_diagnostic"]["error_type"] = "StateError"
    rows[0]["agent_control_failure"] = False
    result = saved.normalize_runtime_outcomes([record], rows)
    assert result[0]["scoring"]["passed"] is None


@pytest.mark.bfcl_evidence
def test_uncounted_tokens_stay_unknown(old_baseline):
    records = deepcopy(old_baseline["records"])
    records[0]["recorded_usage"]["input_tokens"] = None
    result = saved.summarize_campaign(records, old_baseline["report"])
    assert result["recorded_tokens"]["input_tokens"] is None


