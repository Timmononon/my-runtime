"""Load the frozen 200 Multi-Turn development tasks from pinned raw sources.

This module never selects a model, reads a user Runtime, or sends requests.
Answers stay in evaluator-only normalized cases, not promptfoo test variables.
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
FROZEN = PROJECT / "evals/bfcl_multiturn/splits/frozen"
SPLIT_SHA256 = "dfdc29bb1b700a99537b42196814d3f1c415d2038ab13814c97763a9ab199e35"
CATEGORIES = (
    "multi_turn_base", "multi_turn_miss_func", "multi_turn_miss_param", "multi_turn_long_context",
)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def load_development_dataset(*, project=PROJECT, frozen=FROZEN, corpus=CORPUS):
    """Return complete normalized cases and reproducible selection metadata."""
    project, frozen, corpus = Path(project).resolve(), Path(frozen).resolve(), Path(corpus).resolve()
    manifest_payload = (frozen / "split-manifest.json").read_bytes()
    if _sha(manifest_payload) != SPLIT_SHA256:
        raise ValueError("Frozen development split manifest changed")
    manifest = strict_json(manifest_payload)
    if manifest.get("seed") != 20261030 or manifest.get("source_revision") != REVISION:
        raise ValueError("Frozen development source identity differs")
    memberships = {}
    for name, count in (("development-ids.txt", 200), ("holdout-ids.txt", 600), ("regression20-ids.txt", 20)):
        payload = (frozen / name).read_bytes()
        ids = payload.decode("utf-8").splitlines()
        entry = manifest["id_files"][name]
        if (_sha(payload) != entry["sha256"] or entry["lines"] != count
                or len(ids) != count or len(set(ids)) != count):
            raise ValueError("Frozen development/holdout membership changed")
        memberships[name] = ids
    development = memberships["development-ids.txt"]
    heldout = memberships["holdout-ids.txt"]
    if (set(development) & set(heldout)
            or not set(memberships["regression20-ids.txt"]) <= set(development)):
        raise ValueError("Development selection includes held-out or omits exposed tasks")
    bound = {}
    for name, entry in manifest["sources"].items():
        path = (project / name).resolve()
        if not path.is_relative_to(project) or path.is_symlink():
            raise ValueError("Frozen source path escapes the project")
        payload = path.read_bytes()
        if _sha(payload) != entry["sha256"] or len(payload) != entry["bytes"]:
            raise ValueError("Frozen task or prior-selection source changed")
        bound[name] = payload
    root, _samples, archive_sources = _verified_archive(corpus)
    documents = {
        name: read_jsonl(root / (DATA_PREFIX + "multi_turn_func_doc/" + filename))
        for name, filename in FUNCTION_DOCUMENTS.items()
    }
    entries = {item["id"]: item for item in manifest["tasks"]}
    if len(entries) != 800 or set(development) | set(heldout) != set(entries):
        raise ValueError("Frozen split must cover exactly its original 800 tasks")
    categories = {category: [] for category in CATEGORIES}
    for task_id in development:
        entry = entries[task_id]
        if entry["split"] != "development" or entry["category"] not in categories:
            raise ValueError("Task is not a frozen Multi-Turn development member")
        categories[entry["category"]].append(task_id)
    if any(len(ids) != 50 for ids in categories.values()):
        raise ValueError("Each development subcategory must retain exactly 50 tasks")
    order = [categories[category][index] for index in range(50) for category in CATEGORIES]
    cases, groups, issues, row_hashes = [], [], {}, {}
    for task_id in order:
        entry = entries[task_id]
        records = {}
        for kind in ("question", "answer"):
            source = entry[kind + "_source"]
            if source["path"] not in bound:
                raise ValueError("Task source is not bound by frozen provenance")
            lines = bound[source["path"]].splitlines()
            if type(source["line"]) is not int or not 1 <= source["line"] <= len(lines):
                raise ValueError("Task source line is outside its pinned file")
            line = lines[source["line"] - 1]
            row = strict_json(line)
            if (row.get("id") != task_id or _sha(line) != source["line_sha256_without_line_ending"]
                    or _sha(_canonical(row)) != source["record_sha256_canonical_json"]):
                raise ValueError("Frozen task/answer line no longer matches its source")
            records[kind] = row
        row, answer = records["question"], records["answer"]
        question_sha = entry["question_source"]["record_sha256_canonical_json"]
        doc_hashes = {
            name: archive_sources[DATA_PREFIX + "multi_turn_func_doc/" + FUNCTION_DOCUMENTS[name]]["sha256"]
            for name in row.get("involved_classes", [])
        }
        case = normalize_case(row, answer, category=entry["category"], documents=documents, provenance={
            "revision": REVISION, "source_kind": "official_fixed_revision_complete_task",
            "question_sha256": question_sha, "corpus_dir": str(root),
            "question_source": entry["question_source"], "answer_source": entry["answer_source"],
            "function_document_sha256": doc_hashes, "development_split_sha256": SPLIT_SHA256,
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
    declared = {item["id"] for item in manifest["quarantine"]}
    if not declared <= set(issues):
        raise ValueError("A previously quarantined source conflict unexpectedly disappeared")
    metadata = {
        "source_revision": REVISION, "split_manifest_sha256": SPLIT_SHA256,
        "development_ids_sha256": manifest["id_files"]["development-ids.txt"]["sha256"],
        "holdout_ids_sha256": manifest["id_files"]["holdout-ids.txt"]["sha256"],
        "regression20_ids_sha256": manifest["id_files"]["regression20-ids.txt"]["sha256"],
        "allocated_development_tasks": 200, "allocated_holdout_tasks": 600,
        "per_category_allocation": dict(Counter(case["category"] for case in cases)),
        "defined_user_turns": sum(len(case["question"]) for case in cases),
        "reference_admission_issues": issues, "known_admission_conflicts": list(issues),
        "declared_quarantine_ids": sorted(declared),
        "newly_detected_conflict_ids": sorted(set(issues) - declared),
        "admissible_task_count": 200 - len(issues), "groups": groups,
        "question_record_sha256": row_hashes,
        "order_policy": "Round-robin four categories; preserve frozen per-category ID order; never use outcomes",
        "membership_policy": "Exactly frozen 200 development IDs; no replacement for quarantined data",
        "normalized_cases_bytes": len(json.dumps(cases, ensure_ascii=False, allow_nan=False).encode("utf-8")),
        "holdout_inference_tasks": 0, "model_api_calls": 0, "judge_api_calls": 0,
    }
    return cases, metadata


def load_development_cases():
    return load_development_dataset()[0]
