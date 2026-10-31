"""Load exactly the sixty frozen compact100-v2 holdout tasks, offline.

The user explicitly authorized this measurement. Selection is fixed before
execution and does not inspect model outcomes. Gold remains evaluator-only.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

from aster.evaluation.bfcl_v4_data import (
    DATA_PREFIX,
    FUNCTION_DOCUMENTS,
    REVISION,
    _canonical,
    _verified_archive,
    normalize_case,
    read_jsonl,
    strict_json,
)
from aster.evaluation.bfcl_v4_runner import reference_admission_issues

PROJECT = Path(__file__).resolve().parents[3]
CORPUS = PROJECT / "artifacts/bfcl/v4-coverage-20261029"
FROZEN = PROJECT / "evals/bfcl_multiturn/splits/compact100-v2"
SPLIT_SHA256 = "477aa07cff732f1158ce59138e5f1b4bf8f701c3f2de211a43a2c0a01f6d58c8"
PARENT_SPLIT_SHA256 = "dfdc29bb1b700a99537b42196814d3f1c415d2038ab13814c97763a9ab199e35"
CATEGORIES = (
    "multi_turn_base", "multi_turn_miss_func", "multi_turn_miss_param", "multi_turn_long_context",
)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _membership(directory, manifest, sizes):
    result = {}
    for name, count in sizes.items():
        payload = (directory / name).read_bytes()
        ids = payload.decode("utf-8").splitlines()
        expected = manifest["id_files"][name]
        if (_sha(payload) != expected["sha256"] or expected["lines"] != count
                or len(ids) != count or len(set(ids)) != count):
            raise ValueError("Frozen holdout/development membership changed")
        result[name] = ids
    if set(result["development-ids.txt"]) & set(result["holdout-ids.txt"]):
        raise ValueError("Frozen development and holdout overlap")
    return result


def _entries(manifest, count):
    entries = {entry["id"]: entry for entry in manifest["tasks"]}
    if len(entries) != count or len(manifest["tasks"]) != count:
        raise ValueError("Frozen split task membership is not unique")
    return entries


def load_holdout_dataset(*, project=PROJECT, frozen=FROZEN, corpus=CORPUS):
    """Return the existing sixty complete tasks and their pinned provenance."""
    project, frozen, corpus = Path(project).resolve(), Path(frozen).resolve(), Path(corpus).resolve()
    compact_bytes = (frozen / "split-manifest.json").read_bytes()
    if _sha(compact_bytes) != SPLIT_SHA256:
        raise ValueError("Compact holdout split manifest changed")
    compact = strict_json(compact_bytes)
    parent_dir = project / "evals/bfcl_multiturn/splits/frozen"
    parent_bytes = (parent_dir / "split-manifest.json").read_bytes()
    if _sha(parent_bytes) != PARENT_SPLIT_SHA256:
        raise ValueError("Parent frozen split manifest changed")
    parent = strict_json(parent_bytes)
    if (compact.get("scope") != "compact100-v2" or any(
            item.get("seed") != 20261030 or item.get("source_revision") != REVISION
            for item in (compact, parent))):
        raise ValueError("Frozen holdout source identity differs")
    membership = _membership(frozen, compact, {"development-ids.txt": 40, "holdout-ids.txt": 60})
    original_membership = _membership(parent_dir, parent, {
        "development-ids.txt": 200, "holdout-ids.txt": 600, "regression20-ids.txt": 20})
    selected_ids = membership["holdout-ids.txt"]
    if (not set(selected_ids) <= set(original_membership["holdout-ids.txt"])
            or not set(membership["development-ids.txt"]) <= set(original_membership["development-ids.txt"])
            or not set(original_membership["regression20-ids.txt"]) <= set(original_membership["development-ids.txt"])):
        raise ValueError("Holdout tasks escape their original unexposed boundary")
    entries, parent_entries = _entries(compact, 100), _entries(parent, 800)
    if (set(entries) != set(membership["development-ids.txt"]) | set(selected_ids)
            or set(parent_entries) != set(original_membership["development-ids.txt"])
                | set(original_membership["holdout-ids.txt"])):
        raise ValueError("Frozen split files and task rows differ")
    development_families = {task_id.rsplit("_", 1)[1] for task_id in membership["development-ids.txt"]}
    holdout_families = {task_id.rsplit("_", 1)[1] for task_id in selected_ids}
    if len(development_families) != 10 or len(holdout_families) != 15 or development_families & holdout_families:
        raise ValueError("Frozen source families cross the development boundary")

    bound = {}
    for manifest in (parent, compact):
        for name, expected in manifest["sources"].items():
            path = (project / name).resolve()
            if not path.is_relative_to(project) or path.is_symlink():
                raise ValueError("Frozen source path escapes the project")
            payload = path.read_bytes()
            if _sha(payload) != expected["sha256"] or len(payload) != expected["bytes"]:
                raise ValueError("Frozen holdout source bytes changed")
            bound[name] = payload
    root, _samples, archive_sources = _verified_archive(corpus)
    documents = {name: read_jsonl(root / (DATA_PREFIX + "multi_turn_func_doc/" + filename))
                 for name, filename in FUNCTION_DOCUMENTS.items()}
    cases, groups, issues, row_hashes = [], [], {}, {}
    for task_id in selected_ids:
        entry, original = entries[task_id], parent_entries[task_id]
        if (entry["split"] != "holdout" or entry["original_split"] != "holdout"
                or original["split"] != "holdout" or entry["category"] not in CATEGORIES
                or entry["category"] != original["category"]):
            raise ValueError("Task is not a frozen holdout member")
        records = {}
        for kind in ("question", "answer"):
            source = entry[kind + "_source"]
            if source != original[kind + "_source"] or source["path"] not in bound:
                raise ValueError("Compact holdout row differs from its frozen parent")
            lines = bound[source["path"]].splitlines()
            if type(source["line"]) is not int or not 1 <= source["line"] <= len(lines):
                raise ValueError("Frozen holdout source line is outside its file")
            line = lines[source["line"] - 1]
            row = strict_json(line)
            if (row.get("id") != task_id or _sha(line) != source["line_sha256_without_line_ending"]
                    or _sha(_canonical(row)) != source["record_sha256_canonical_json"]):
                raise ValueError("Frozen holdout task/answer row hash changed")
            records[kind] = row
        row = records["question"]
        question_sha = entry["question_source"]["record_sha256_canonical_json"]
        doc_hashes = {name: archive_sources[DATA_PREFIX + "multi_turn_func_doc/" + FUNCTION_DOCUMENTS[name]]["sha256"]
                      for name in row.get("involved_classes", [])}
        case = normalize_case(row, records["answer"], category=entry["category"], documents=documents, provenance={
            "revision": REVISION, "source_kind": "official_fixed_revision_complete_task",
            "question_sha256": question_sha, "corpus_dir": str(root),
            "question_source": entry["question_source"], "answer_source": entry["answer_source"],
            "function_document_sha256": doc_hashes, "parent_split_sha256": PARENT_SPLIT_SHA256,
            "holdout_split_sha256": SPLIT_SHA256, "compact_split_sha256": SPLIT_SHA256,
            "compact_scope": "compact100-v2", "evaluation_split": "holdout",
        })
        cases.append(case)
        groups.append({"group": task_id, "entry_ids": [task_id], "category": entry["category"],
            "source_kind": "official_fixed_revision_complete_task", "user_turns": len(case["question"]),
            "question_sha256": question_sha, "api_call_cap": None,
            "official_model_step_bound": 20 * len(case["question"])})
        row_hashes[task_id] = question_sha
        found = reference_admission_issues(case)
        if found:
            issues[task_id] = found
    if Counter(case["category"] for case in cases) != dict.fromkeys(CATEGORIES, 15):
        raise ValueError("Holdout must retain fifteen complete tasks in each category")
    metadata = {
        "source_revision": REVISION, "scope": "compact100-v2", "evaluation_split": "holdout",
        "split_manifest_sha256": SPLIT_SHA256, "original_split_sha256": PARENT_SPLIT_SHA256,
        "original_development_split_sha256": PARENT_SPLIT_SHA256,
        "development_ids_sha256": compact["id_files"]["development-ids.txt"]["sha256"],
        "holdout_ids_sha256": compact["id_files"]["holdout-ids.txt"]["sha256"],
        "allocated_development_tasks": 40, "allocated_holdout_tasks": 60,
        "evaluation_task_count": 60, "planned_holdout_tasks": 60,
        "per_category_allocation": dict(Counter(case["category"] for case in cases)),
        "defined_user_turns": sum(len(case["question"]) for case in cases),
        "reference_admission_issues": issues, "known_admission_conflicts": list(issues),
        "declared_quarantine_ids": [], "newly_detected_conflict_ids": sorted(issues),
        "admissible_task_count": 60 - len(issues), "groups": groups,
        "question_record_sha256": row_hashes, "order_policy": compact["sampling"]["task_order"],
        "membership_policy": "Exactly compact100-v2 sixty holdout IDs; no development inference or replacements",
        "normalized_cases_bytes": len(json.dumps(cases, ensure_ascii=False, allow_nan=False).encode("utf-8")),
        "holdout_inference_tasks": 0, "model_api_calls": 0, "judge_api_calls": 0,
    }
    return cases, metadata


def load_holdout_cases():
    return load_holdout_dataset()[0]
