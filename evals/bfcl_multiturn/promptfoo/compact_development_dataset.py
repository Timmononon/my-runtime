"""Read only the frozen compact100-v2 development membership, offline.

The existing verified raw-source loader supplies normalization. Only the forty
selected complete cases leave this module; holdout cases never enter a plan.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[2]
FROZEN = PROJECT / "evals/bfcl_multiturn/splits/compact100-v2"
SPLIT_SHA256 = "477aa07cff732f1158ce59138e5f1b4bf8f701c3f2de211a43a2c0a01f6d58c8"
_SPEC = importlib.util.spec_from_file_location("aster_compact_original_dataset", HERE / "development_dataset.py")
_original = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_original)
CORPUS, CATEGORIES = _original.CORPUS, _original.CATEGORIES


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def load_development_dataset(*, project=PROJECT, frozen=FROZEN, corpus=CORPUS):
    """Verify the new scope and return forty complete tasks in frozen order."""
    project, frozen = Path(project).resolve(), Path(frozen).resolve()
    payload = (frozen / "split-manifest.json").read_bytes()
    if _sha(payload) != SPLIT_SHA256:
        raise ValueError("Compact split manifest changed")
    manifest = json.loads(payload)
    if (manifest.get("scope") != "compact100-v2" or manifest.get("seed") != 20261030
            or manifest.get("source_revision") != _original.REVISION):
        raise ValueError("Compact split source identity differs")
    membership = {}
    for name, count in (("development-ids.txt", 40), ("holdout-ids.txt", 60)):
        content = (frozen / name).read_bytes()
        ids = content.decode("utf-8").splitlines()
        pinned = manifest["id_files"][name]
        if (_sha(content) != pinned["sha256"] or len(ids) != count or len(set(ids)) != count
                or pinned["lines"] != count):
            raise ValueError("Compact split membership changed")
        membership[name] = ids
    ids, heldout = membership["development-ids.txt"], set(membership["holdout-ids.txt"])
    if set(ids) & heldout:
        raise ValueError("Compact development and holdout overlap")
    entries = {entry["id"]: entry for entry in manifest["tasks"]}
    if len(entries) != 100 or set(entries) != set(ids) | heldout:
        raise ValueError("Compact split must bind exactly one hundred tasks")
    # This also verifies the old frozen split and each raw source/answer hash.
    cases, original_metadata = _original.load_development_dataset(
        project=project, frozen=project / "evals/bfcl_multiturn/splits/frozen", corpus=corpus)
    by_id = {case["id"]: case for case in cases}
    original_groups = {group["group"]: group for group in original_metadata["groups"]}
    selected = []
    for task_id in ids:
        entry = entries[task_id]
        if (task_id not in by_id or entry["split"] != "development"
                or entry["original_split"] != "development"):
            raise ValueError("Compact case escapes the original development boundary")
        case = by_id[task_id]
        if any(entry[kind + "_source"] != case["provenance"][kind + "_source"]
               for kind in ("question", "answer")):
            raise ValueError("Compact case differs from its verified source row")
        case["provenance"].update(compact_split_sha256=SPLIT_SHA256, compact_scope="compact100-v2")
        selected.append(case)
    if Counter(case["category"] for case in selected) != dict.fromkeys(CATEGORIES, 10):
        raise ValueError("Compact development must retain ten tasks per category")
    development_families = {task_id.rsplit("_", 1)[1] for task_id in ids}
    holdout_families = {task_id.rsplit("_", 1)[1] for task_id in heldout}
    if len(development_families) != 10 or len(holdout_families) != 15 or development_families & holdout_families:
        raise ValueError("Compact source families cross the development boundary")
    issues = {task_id: issue for task_id, issue in original_metadata["reference_admission_issues"].items()
              if task_id in ids}
    metadata = {
        "source_revision": manifest["source_revision"], "scope": "compact100-v2",
        "split_manifest_sha256": SPLIT_SHA256,
        "original_development_split_sha256": original_metadata["split_manifest_sha256"],
        "development_ids_sha256": manifest["id_files"]["development-ids.txt"]["sha256"],
        "holdout_ids_sha256": manifest["id_files"]["holdout-ids.txt"]["sha256"],
        "allocated_development_tasks": 40, "allocated_holdout_tasks": 60,
        "per_category_allocation": dict(Counter(case["category"] for case in selected)),
        "defined_user_turns": sum(len(case["question"]) for case in selected),
        "reference_admission_issues": issues, "known_admission_conflicts": list(issues),
        "declared_quarantine_ids": [], "newly_detected_conflict_ids": sorted(issues),
        "admissible_task_count": 40 - len(issues),
        "groups": [original_groups[task_id] for task_id in ids],
        "question_record_sha256": {task_id: original_metadata["question_record_sha256"][task_id] for task_id in ids},
        "order_policy": manifest["sampling"]["task_order"],
        "membership_policy": "Exactly compact100-v2 development forty IDs; no holdout inference or replacement",
        "normalized_cases_bytes": len(json.dumps(selected, ensure_ascii=False, allow_nan=False).encode("utf-8")),
        "holdout_inference_tasks": 0, "model_api_calls": 0, "judge_api_calls": 0,
    }
    return selected, metadata


def load_development_cases():
    return load_development_dataset()[0]
