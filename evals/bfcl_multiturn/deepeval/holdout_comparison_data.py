"""Strict saved-result projection of the fixed sixty-task held-out evaluation.

The original development loader stays unchanged. This module never executes
captured code, starts a provider, imports DeepEval, or calls a model.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from copy import deepcopy
from pathlib import Path

from aster.evaluation.bfcl_multiturn_export import SUBCATEGORIES, _digest, _hash, _read, validate_record
from aster.evaluation.bfcl_multiturn_unmetered_export import load_unmetered_campaign
from evals.bfcl_multiturn.deepeval import comparison_data as saved

PROJECT = Path(__file__).resolve().parents[3]
CONFIGURATIONS = {
    "A0": {"policy": "off", "commit": "453f4bd0293dba764eca609915c633ab382bcc50",
        "historical_campaign": "artifacts/bfcl/compact-dev40-c20-20261030",
        "historical_manifest": "74e8895ca84446789edd1705057da726695444ac9a3c207333ee5788d1a1e137"},
    "A3": {"policy": "readiness_state_v1", "commit": "04d98823bec6d0222047ef1c04f27b49975fd74e",
        "historical_campaign": "artifacts/bfcl/compact-dev40-a3-c20-20261030",
        "historical_manifest": "b6dc5707883ce2ac6a6fb582b60d2fffb5b24f0baa981ad566a51d95715fe3b4"},
    "MF-Gate-v1": {"policy": "capability_check_v1", "commit": "2beefb554487c03ce9f6f9bf59672b6c3ecabb39",
        "historical_campaign": "artifacts/bfcl/compact-dev40-mf-gate-c20-20261030",
        "historical_manifest": "50ce2a235bb71519376a5f75b500d4a4b32dc24928e817272b8030d8f7570e66"},
}
SHARED_BACKENDS = frozenset({
    "src/aster/evaluation/bfcl_multiturn_concurrent.py",
    "src/aster/evaluation/bfcl_multiturn_unmetered.py",
})
CORE_MODULES = frozenset({
    "aster.engine.loop", "aster.service.coordinator", "aster.evaluation.bfcl_v4_runner",
    "aster.evaluation.bfcl_multiturn_concurrent", "aster.evaluation.bfcl_multiturn_unmetered",
})


def _entry_map(entries):
    if not isinstance(entries, list) or any(not isinstance(row, dict) for row in entries):
        raise ValueError("Source identity requires file records")
    result = {}
    for row in entries:
        name = row.get("file")
        if (not isinstance(name, str) or Path(name).is_absolute() or ".." in Path(name).parts
                or name in result):
            raise ValueError("Source identity has an unsafe or duplicate relative path")
        result[name] = row
    return result


def validate_membership(root, plan, control, report):
    """Bind exactly the original holdout IDs, in their frozen order."""
    split = root / "as-run-source" / saved.SPLIT_RELATIVE
    for name, expected in (
        ("split-manifest.json", saved.COMPACT_MANIFEST_SHA256),
        ("development-ids.txt", saved.DEVELOPMENT_SHA256),
        ("holdout-ids.txt", saved.HOLDOUT_SHA256),
    ):
        if _hash(split / name) != expected:
            raise ValueError("Held-out membership authority changed")
    ids = (split / "holdout-ids.txt").read_text(encoding="utf-8").splitlines()
    development = (split / "development-ids.txt").read_text(encoding="utf-8").splitlines()
    if (len(ids) != 60 or len(set(ids)) != 60 or len(development) != 40
            or len(set(development)) != 40 or set(ids) & set(development)):
        raise ValueError("Expected sixty held-out and forty disjoint development tasks")
    categories = Counter(next((category for category in SUBCATEGORIES
                               if task_id.startswith(category + "_")), None) for task_id in ids)
    if categories != dict.fromkeys(SUBCATEGORIES, 15):
        raise ValueError("Held-out categories must each retain fifteen tasks")
    planned = [task_id for group in plan.get("groups", []) for task_id in group["entry_ids"]]
    if planned != ids or control.get("case_ids") != ids:
        raise ValueError("Execution did not retain the frozen sixty-task order")
    if (plan.get("scope") != "compact100-v2" or plan.get("evaluation_split") != "holdout"
            or plan.get("evaluation_task_count") != 60 or plan.get("planned_entries") != 60
            or report.get("evaluation_split") != "holdout"):
        raise ValueError("This loader accepts only the declared holdout60 experiment")
    return ids


def _historical_sources(configuration, *, root=None, captured_name=None):
    expected = CONFIGURATIONS[configuration]
    path = (root / "as-run-source" / captured_name if root is not None and captured_name is not None
            else PROJECT / expected["historical_campaign"] / "source-manifest.json")
    if _hash(path) != expected["historical_manifest"]:
        raise ValueError("Historical frozen-source authority changed")
    if root is not None:
        sources = _entry_map(_read(root / "source-manifest.json")["files"])
        if captured_name not in sources or sources[captured_name]["sha256"] != expected["historical_manifest"]:
            raise ValueError("Historical source authority is not bound in the execution snapshot")
    return _entry_map(_read(path)["files"])


def validate_import_proof(proof, *, configuration, sources, project_root):
    """Check the captured module origins against frozen saved source bytes."""
    expected = CONFIGURATIONS[configuration]
    if (proof.get("configuration_label") != configuration or proof.get("source_commit") != expected["commit"]
            or Path(proof.get("project_root", "")).resolve() != Path(project_root).resolve()):
        raise ValueError("Runtime import identity differs from the frozen configuration")
    required = set(CORE_MODULES)
    if configuration == "A3":
        required.add("aster.engine.task_control")
    elif configuration == "MF-Gate-v1":
        required.update({"aster.engine.capability_gate", "aster.engine.readiness_prompt"})
    seen = set()
    for row in proof.get("modules", []):
        module, relative = row.get("module"), row.get("relative_file", row.get("file"))
        if row.get("relative_file", relative) != relative or row.get("file", relative) != relative:
            raise ValueError("Runtime proof relative path aliases disagree")
        if (module in seen or not isinstance(module, str)
                or module != "aster" and not module.startswith("aster.")):
            raise ValueError("Runtime proof has unknown or duplicate modules")
        seen.add(module)
        module_path = "src/" + module.replace(".", "/")
        if relative not in {module_path + ".py", module_path + "/__init__.py"}:
            raise ValueError("Runtime proof module name does not match its source file")
        origin = Path(row.get("origin", "")).resolve()
        base = Path(project_root).resolve()
        if (not isinstance(relative, str) or relative not in sources or not origin.is_relative_to(base)
                or origin.relative_to(base).as_posix() != relative
                or row.get("sha256") != sources[relative]["sha256"]):
            raise ValueError("Imported module does not match its saved source origin")
    if not required <= seen:
        raise ValueError("Runtime proof omits a required frozen execution module")


def validate_frozen_runtime(root, *, configuration):
    """Require exact historical runtime bytes plus the two declared common backends."""
    if configuration not in CONFIGURATIONS:
        raise ValueError("Only A0, A3 and MF-Gate-v1 are approved configurations")
    expected = CONFIGURATIONS[configuration]
    source = _entry_map(_read(root / "source-manifest.json")["files"])
    name = "frozen-runtime-manifest.json"
    path = root / "as-run-source" / name
    if name not in source or _hash(path) != source[name]["sha256"]:
        raise ValueError("Frozen-runtime manifest must be bound into the execution snapshot")
    manifest = _read(path)
    if (manifest.get("schema_version") != 1 or manifest.get("configuration_label") != configuration
            or manifest.get("source_commit") != expected["commit"]
            or manifest.get("historical_source_manifest_sha256") != expected["historical_manifest"]):
        raise ValueError("Frozen runtime does not identify the requested historical configuration")
    historical = _historical_sources(configuration, root=root, captured_name="frozen-origin-source-manifest.json")
    shared = _historical_sources("MF-Gate-v1", root=root, captured_name="shared-infrastructure-source-manifest.json")
    actual = {name: row for name, row in source.items() if name.startswith("src/aster/")}
    expected_src = {name: row for name, row in historical.items() if name.startswith("src/aster/")}
    expected_src.update({name: shared[name] for name in SHARED_BACKENDS})
    if set(actual) != set(expected_src):
        raise ValueError("Execution package contains unexpected or missing runtime files")
    for relative, row in actual.items():
        if any(row.get(key) != expected_src[relative].get(key) for key in ("sha256", "bytes")):
            raise ValueError("An Agent runtime file changed from its frozen historical version")
        if _hash(root / "as-run-source" / relative) != row["sha256"]:
            raise ValueError("Saved frozen execution bytes changed")
    declared = _entry_map(manifest.get("frozen_files", []))
    overlays = _entry_map(manifest.get("overlays", []))
    if set(declared) & set(overlays):
        raise ValueError("A frozen source cannot also be an overlay")
    declared_src = {name for name in declared if name.startswith("src/aster/")}
    overlay_src = {name for name in overlays if name.startswith("src/aster/")}
    if overlay_src != SHARED_BACKENDS or declared_src | overlay_src != set(actual):
        raise ValueError("Only the two common evaluation backends may overlay frozen Agent source")
    for relative in actual:
        if (declared | overlays)[relative]["sha256"] != actual[relative]["sha256"]:
            raise ValueError("Materialization declaration differs from the execution snapshot")
    proof_path = root / "runtime-import-identity.json"
    proof = _read(proof_path)
    validate_import_proof(proof, configuration=configuration, sources=source, project_root=manifest["project_root"])
    proof_hash = _hash(proof_path)
    dispatches = root / "promptfoo/dispatches"
    started, finished = list(dispatches.glob("*.started.json")), list(dispatches.glob("*.finished.json"))
    if len(started) != 60 or len(finished) != 60:
        raise ValueError("Every complete-task callback requires an import-identity witness")
    observed = set()
    for file in started:
        row = _read(file)
        if row.get("task_id") in observed or row.get("runtime_import_identity_sha256") != proof_hash:
            raise ValueError("Callback runtime import witness differs or is duplicated")
        observed.add(row.get("task_id"))
    for file in finished:
        if _read(file).get("runtime_import_identity_sha256") != proof_hash:
            raise ValueError("Callback completion changed its runtime source identity")
    expected_ids = set(_read(root / "execution-control.json")["case_ids"])
    ended = [_read(file).get("task_id") for file in finished]
    if observed != expected_ids or len(set(ended)) != 60 or set(ended) != expected_ids:
        raise ValueError("Callback source witnesses do not cover the frozen task IDs")
    return {"configuration_label": configuration, "source_commit": expected["commit"],
        "historical_source_manifest_sha256": expected["historical_manifest"],
        "frozen_runtime_manifest_sha256": _hash(path), "runtime_import_identity_sha256": proof_hash,
        "verified_runtime_files": len(actual), "shared_backend_files": sorted(SHARED_BACKENDS),
        "callback_import_witnesses": len(started), "actual_execution_source_verified": True}


def _validate_policy(root, plan, control, report, rows, configuration):
    policy = CONFIGURATIONS[configuration]["policy"]
    if (control.get("agent_policy") or "off") != policy:
        raise ValueError("Saved Agent policy differs from the requested historical configuration")
    if configuration == "MF-Gate-v1":
        return saved.validate_gate_campaign(root, plan, control, report, rows)
    if configuration == "A3":
        if plan.get("agent_policy") != policy or any(row.get("agent_policy") != policy for row in rows):
            raise ValueError("The A3 policy must be bound in every task receipt")
        for row in rows:
            for run in row.get("runs", []):
                receipts, purposes = run.get("model_request_receipts"), run.get("model_request_purposes")
                if (not isinstance(receipts, list) or not isinstance(purposes, dict) or len(receipts) > 20
                        or set(purposes) - {"main", "task_control"}
                        or any(type(value) is not int or value < 0 for value in purposes.values())
                        or sum(purposes.values()) != len(receipts)
                        or Counter(item.get("purpose") for item in receipts) != Counter(purposes)):
                    raise ValueError("A3 helper and main accounting must share the twenty-step bound")
    elif plan.get("agent_policy") not in {None, "off"} or any(row.get("agent_policy") not in {None, "off"} for row in rows):
        raise ValueError("A0 cannot contain another Agent policy")
    return None


def load_campaign(campaign_dir, *, label):
    """Load all sixty saved outcomes; infrastructure stays null, never silently dropped."""
    if label not in CONFIGURATIONS:
        raise ValueError("An explicit frozen configuration label is required")
    root = Path(campaign_dir).resolve()
    report, control, mode = saved._sealed(root)
    plan = _read(root / "campaign-plan.json")
    for name in ("source-manifest.json", "campaign-plan.json"):
        if control.get(name + "_sha256") != _hash(root / name):
            raise ValueError("Captured execution source or plan identity changed")
    ids = validate_membership(root, plan, control, report)
    if any(item.get("maximum_concurrency") != 20 for item in (plan, report)) or mode.get("maximum") != 20:
        raise ValueError("The held-out comparison requires twenty workers")
    model = saved._profile_identity(control)
    if any(_read(root / "model-selection.json").get(key) != value for key, value in model.items()):
        raise ValueError("Model-selection and captured execution identities differ")
    cases = _read(root / "private-cases.json")
    admitted_hash = hashlib.sha256(json.dumps(
        cases, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    if admitted_hash != control.get("cases_sha256") or report.get("dataset_binding") != control.get("dataset_binding"):
        raise ValueError("Private cases or reported dataset binding changed")
    rows = _read(root / "results/results.json").get("results", [])
    if [row.get("id") for row in rows] != ids or [case.get("id") for case in cases] != ids:
        raise ValueError("Saved cases/results must retain every held-out task in frozen order")
    runtime_identity = validate_frozen_runtime(root, configuration=label)
    prompt_identity = _validate_policy(root, plan, control, report, rows, label)
    records = saved.normalize_runtime_outcomes(load_unmetered_campaign(root, config_label=label), rows)
    if [record["task_id"] for record in records] != ids:
        raise ValueError("The exported record order differs from frozen held-out membership")
    reference = {}
    for case in cases:
        expected = case.get("expected")
        if not isinstance(expected, list) or any(not isinstance(turn, list) for turn in expected):
            raise ValueError("Invalid evaluator-only reference turn boundary")
        reference[case["id"]] = [len(turn) for turn in expected]
    identity = {"model": model, "maximum_concurrency": 20,
        "agent_policy": CONFIGURATIONS[label]["policy"], "evaluation_split": "holdout", "evaluation_task_count": 60,
        "runtime_policy": {field: control.get(field) for field in saved.COMPARABLE_POLICY_FIELDS},
        "source_manifest_sha256": _hash(root / "source-manifest.json"),
        "source_files_digest": _digest(_read(root / "source-manifest.json")["files"]),
        "plan_sha256": _hash(root / "campaign-plan.json"), "control_sha256": _hash(root / "execution-control.json"),
        "split_manifest_sha256": saved.COMPACT_MANIFEST_SHA256,
        "development_ids_sha256": saved.DEVELOPMENT_SHA256, "holdout_ids_sha256": saved.HOLDOUT_SHA256,
        "frozen_runtime": runtime_identity}
    if prompt_identity is not None:
        identity["agent_policy_identity"] = prompt_identity
    for record, row in zip(records, rows, strict=True):
        snapshot = record["config_identity"]["snapshot"]
        snapshot.update(maximum_concurrency=20, agent_policy=identity["agent_policy"], evaluation_split="holdout",
                        source_manifest_sha256=identity["source_manifest_sha256"], frozen_runtime=deepcopy(runtime_identity))
        if prompt_identity is not None:
            snapshot["agent_policy_identity"] = deepcopy(prompt_identity)
            record["capability_gate"] = {"policy_identity": deepcopy(prompt_identity),
                "turns": [item for run in row.get("runs", []) if (item := saved._gate_projection(run)) is not None],
                "semantic_correctness_authority": "Saved official BFCL checks, not a gate self-report"}
        record["config_identity"]["sha256"] = _digest(snapshot)
        record["record_sha256"] = _digest({key: value for key, value in record.items() if key != "record_sha256"})
        validate_record(record)
    summary = saved.summarize_campaign(records, report)
    summary.update(evaluation_split="holdout", saved_holdout_tasks=60, new_model_api_calls=0, new_judge_api_calls=0)
    return {"label": label, "root": str(root), "records": records, "rows": rows,
        "reference_call_counts": reference, "identity": identity, "report": report, "summary": summary}


def compare_campaigns(baseline, candidate):
    for campaign in (baseline, candidate):
        if campaign["identity"].get("evaluation_split") != "holdout" or len(campaign["records"]) != 60:
            raise ValueError("Held-out pairing requires the complete fixed sixty-task membership")
    result = saved.compare_campaigns(baseline, candidate)
    result.update(evaluation_split="holdout", saved_holdout_tasks_compared=60,
        new_holdout_inference_tasks=0,
        interpretation="One fixed held-out paired measurement of frozen Agent versions; no tuning or new model measurement in this offline replay")
    result.pop("holdout_tasks_evaluated", None)
    return result
