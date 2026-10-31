"""Freeze an offline 100-task scope without changing the historical 200/600 split.

Only frozen membership, source provenance and source rows are read. No Runtime,
campaign results, model client, network or upstream executable code is used.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
FROZEN = Path("evals/bfcl_multiturn/splits/frozen")
ORIGINAL_MANIFEST_SHA256 = "dfdc29bb1b700a99537b42196814d3f1c415d2038ab13814c97763a9ab199e35"
REVISION = "6ea57973c7a6097fd7c5915698c54c17c5b1b6c8"
SEED = 20261030
DOMAIN = "aster-bfcl-v4-multiturn-compact100-v2"
CATEGORIES = ("multi_turn_base", "multi_turn_miss_func", "multi_turn_miss_param", "multi_turn_long_context")
EXCLUDED_FAMILIES = (173,)
NAMES = ("development-ids.txt", "holdout-ids.txt", "split-manifest.json")


class SplitError(ValueError):
    """Frozen source identity or membership is inconsistent."""


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _read(root: Path, relative: str | Path) -> bytes:
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root) or candidate.is_symlink():
        raise SplitError("Source path escapes project")
    try:
        return candidate.read_bytes()
    except OSError as exc:
        raise SplitError(f"Missing source: {candidate.name}") from exc


def _json(payload: bytes) -> object:
    try:
        return json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise SplitError("Invalid source JSON") from exc


def _original_membership(root: Path) -> tuple[dict, dict[str, list[str]], dict[str, bytes]]:
    payload = _read(root, FROZEN / "split-manifest.json")
    if sha256(payload) != ORIGINAL_MANIFEST_SHA256:
        raise SplitError("Original frozen split manifest changed")
    manifest = _json(payload)
    if manifest["source_revision"] != REVISION or manifest["seed"] != SEED:
        raise SplitError("Original frozen source identity differs")
    bound = {(FROZEN / "split-manifest.json").as_posix(): payload}
    memberships = {}
    for name, count in (("development-ids.txt", 200), ("holdout-ids.txt", 600), ("regression20-ids.txt", 20)):
        data = _read(root, FROZEN / name)
        ids = data.decode("utf-8").splitlines()
        identity = manifest["id_files"][name]
        if (sha256(data) != identity["sha256"] or identity["lines"] != count
                or len(ids) != count or len(set(ids)) != count):
            raise SplitError("Original frozen ID list changed")
        bound[(FROZEN / name).as_posix()] = data
        memberships[name] = ids
    development, holdout = (set(memberships[name]) for name in NAMES[:2])
    if development & holdout or not set(memberships["regression20-ids.txt"]) <= development:
        raise SplitError("Original exposure boundary changed")
    expected = {f"{category}_{index}" for category in CATEGORIES for index in range(200)}
    if development | holdout != expected:
        raise SplitError("Original split must cover exactly the pinned 800 tasks")
    return manifest, memberships, bound


def _family_pools(manifest: dict, memberships: dict[str, list[str]]) -> dict[str, set[int]]:
    entries = {task["id"]: task for task in manifest["tasks"]}
    if len(manifest["tasks"]) != len(entries) or len(entries) != 800:
        raise SplitError("Original source-task identities changed")
    pools = {}
    for label, required in (("development", 50), ("holdout", 150)):
        ids = set(memberships[label + "-ids.txt"])
        families = {int(task_id.rsplit("_", 1)[1]) for task_id in ids}
        if len(families) != required:
            raise SplitError("Original source-family allocation changed")
        for index in families:
            for category in CATEGORIES:
                task_id = f"{category}_{index}"
                entry = entries.get(task_id, {})
                if (task_id not in ids or entry.get("family_index") != index
                        or entry.get("category") != category or entry.get("split") != label):
                    raise SplitError("Original source-family exposure boundary changed")
        pools[label] = families
    if pools["development"] & pools["holdout"]:
        raise SplitError("Source families cross the exposure boundary")
    return pools


def build_files(repo_root: Path = REPO_ROOT, seed: int = SEED) -> dict[str, bytes]:
    """Build deterministic metadata only, independent of all model outcomes."""
    if type(seed) is not int or seed < 0:
        raise SplitError("Seed must be a nonnegative integer")
    root = repo_root.resolve()
    original, membership, bound = _original_membership(root)
    pools = _family_pools(original, membership)
    entries = {task["id"]: task for task in original["tasks"]}

    def rank(label: str, index: int) -> tuple[str, int]:
        return sha256(f"{DOMAIN}\0{seed}\0{label}\0{index}".encode()), index

    selected = {
        label: sorted(pools[label] - set(EXCLUDED_FAMILIES), key=lambda index: rank(label, index))[:count]
        for label, count in (("development", 10), ("holdout", 15))
    }
    if len(selected["development"]) != 10 or len(selected["holdout"]) != 15:
        raise SplitError("Insufficient eligible source families")
    # Verify the eight original raw files and provenance; historical campaign
    # result paths in the original manifest are deliberately never opened.
    source_names = {
        entries[f"{category}_0"][kind + "_source"]["path"]
        for category in CATEGORIES for kind in ("question", "answer")
    }
    provenance_name = "artifacts/bfcl/v4-coverage-20261029/provenance.json"
    for name in sorted(source_names | {provenance_name}):
        data = _read(root, name)
        identity = original["sources"][name]
        if sha256(data) != identity["sha256"] or len(data) != identity["bytes"]:
            raise SplitError("Pinned original question, answer or provenance changed")
        bound[name] = data
    tasks, families, id_lists = [], [], {}
    for label in ("development", "holdout"):
        ids = []
        # Preserve seeded family rank and interleave the four categories.
        for index in selected[label]:
            family_ids = [f"{category}_{index}" for category in CATEGORIES]
            families.append({"family_index": index, "split": label, "members": family_ids})
            for task_id in family_ids:
                entry = entries[task_id]
                for kind in ("question", "answer"):
                    source = entry[kind + "_source"]
                    if source["path"] not in source_names:
                        raise SplitError("Task source is not one of the pinned raw files")
                    lines = bound[source["path"]].splitlines()
                    line_number = source["line"]
                    if type(line_number) is not int or not 1 <= line_number <= len(lines):
                        raise SplitError("Task source line is out of bounds")
                    line = lines[line_number - 1]
                    row = _json(line)
                    if (row.get("id") != task_id
                            or sha256(line) != source["line_sha256_without_line_ending"]
                            or sha256(canonical(row)) != source["record_sha256_canonical_json"]):
                        raise SplitError("Task source row differs from frozen provenance")
                ids.append(task_id)
                tasks.append({
                    "id": task_id, "family_index": index, "category": entry["category"],
                    "split": label, "original_split": entry["split"],
                    "previously_selected_20": entry["previously_selected_20"],
                    "scoring_eligibility": "not_prevalidated_by_this_split",
                    "question_source": entry["question_source"], "answer_source": entry["answer_source"],
                })
        id_lists[label + "-ids.txt"] = ("\n".join(ids) + "\n").encode("utf-8")
    prior = set(membership["regression20-ids.txt"])
    active = {task["id"] for task in tasks}
    manifest = {
        "schema_version": 1, "scope": "compact100-v2", "dataset": "BFCL V4 Multi-Turn",
        "source_revision": REVISION, "seed": seed,
        "supersedes": {"scope": "compact100-v1", "reason": "User requested more holdout tasks: replace the unexecuted 60/40 proposal with 40/60", "prior_scope_preserved": True, "prior_scope_model_api_calls": 0},
        "original_split": {"path": (FROZEN / "split-manifest.json").as_posix(), "sha256": ORIGINAL_MANIFEST_SHA256},
        "sampling": {
            "algorithm": "sha256_seeded_separate_pool_family_rank_v1", "domain": DOMAIN,
            "rank_input": "UTF-8(domain + NUL + decimal(seed) + NUL + pool_label + NUL + decimal(family_index))",
            "pool_labels": ["development", "holdout"],
            "sort": "Ascending SHA-256 hex digest, then ascending integer family_index",
            "selection": "First 10 eligible original-development families; first 15 eligible original-holdout families",
            "development_families_in_rank_order": selected["development"],
            "holdout_families_in_rank_order": selected["holdout"],
            "task_order": "Seeded family rank within each split; Base, Missing Functions, Missing Parameters, Long Context within each family",
            "outcomes_used": False,
        },
        "counts": {
            "source_pool": 800, "active": 100, "inactive": 700,
            "development": 40, "holdout": 60, "active_families": 25,
            "development_families": 10, "holdout_families": 15,
            "original_development_not_selected": 160, "original_holdout_not_selected": 540,
            "historical_regression20_retained_in_active_development": len(prior & active),
            "historical_regression20_inactive": len(prior - active),
            "per_category": {
                category: {"active": 25, "development": 10, "holdout": 15}
                for category in CATEGORIES
            },
        },
        "predeclared_data_exclusions": [{
            "family_index": 173, "task_ids": [f"{category}_173" for category in CATEGORIES],
            "reason": "Previously documented schema/gold ticket_id type conflict in all four variants",
            "selection_basis": "Known source-data conflict, independent of Agent outcomes",
        }],
        "policy": {
            "authority": "This manifest and two ID lists define the new active scope; historical files remain unchanged",
            "exposure_boundary": "Development only from original development; holdout only from original holdout; all four same-suffix variants stay together",
            "regression20": "Historical 20 are not all forced into this scope: their 20 source families would require at least 80 development tasks; unselected records remain historical",
            "inactive": "The other 700 source tasks are outside this plan; no additional regression tasks are silently added",
            "holdout": "Original holdout membership is the local exposure boundary; use after strategy/configuration freeze; this is not proof of absence from model pretraining",
            "conflicts": "Allocation counts are not a certification of scorable counts; quarantine newly discovered source conflicts without resampling or modifying raw sources",
            "source_bodies": "No question or reference-answer bodies are written here; answers remain evaluator-only",
            "outcomes": "Never select or move tasks using pass/fail, tool traces, usage, latency or scores; no campaign result files are read",
            "comparison": "Report future paired A0/A1 scores on the exact same selected development tasks; old 200-task score is a separate population",
            "execution": "This module only creates/validates membership; existing 200-task execution entry is unchanged",
        },
        "grouping_audit": {
            "rule": "Keep four categories with the same integer source suffix in one split",
            "exact_question_duplicates_across_source_families": original["grouping_audit"]["exact_question_duplicates_across_source_families"],
            "semantic_near_duplicate_audit": "Not performed; inherited mechanical family boundary is not complete semantic independence",
        },
        "sources": {name: {"sha256": sha256(data), "bytes": len(data)} for name, data in sorted(bound.items())},
        "generator": {"path": "evals/bfcl_multiturn/splits/create_compact_split_v2.py", "sha256": sha256(Path(__file__).read_bytes())},
        "id_files": {name: {"sha256": sha256(data), "lines": len(data.splitlines())} for name, data in id_lists.items()},
        "families": families, "tasks": tasks,
        "model_api_calls": 0, "judge_api_calls": 0, "capability_score": None,
    }
    return {**id_lists, "split-manifest.json": json_bytes(manifest)}


def write_split(output: Path, files: dict[str, bytes]) -> None:
    """Write only a new directory; never overwrite a frozen scope."""
    if output.exists():
        raise SplitError("Output already exists; frozen files are never overwritten")
    output.mkdir(parents=True, exist_ok=False)
    for name in NAMES:
        with (output / name).open("xb") as stream:
            stream.write(files[name])


def validate_split(directory: Path, files: dict[str, bytes]) -> dict:
    for name in NAMES:
        try:
            data = (directory / name).read_bytes()
        except OSError as exc:
            raise SplitError(f"Missing frozen output: {name}") from exc
        if data != files[name]:
            raise SplitError(f"Frozen output differs from reproducible source: {name}")
    manifest = _json(files["split-manifest.json"])
    counts = Counter(task["split"] for task in manifest["tasks"])
    return {
        "valid": True, "scope": "compact100-v2", "seed": manifest["seed"],
        "development": counts["development"], "holdout": counts["holdout"],
        "active": sum(counts.values()), "inactive": 700,
        "model_api_calls": 0, "judge_api_calls": 0, "capability_score": None,
        "files": {name: sha256(data) for name, data in files.items()},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--output", type=Path, help="Create new membership directory; never overwrite")
    mode.add_argument("--validate", type=Path, help="Recompute and byte-compare frozen membership")
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args(argv)
    try:
        files = build_files(seed=args.seed)
        directory = args.output if args.output is not None else args.validate
        if args.output is not None:
            write_split(directory, files)
        print(json.dumps(validate_split(directory, files), sort_keys=True, indent=2))
    except (SplitError, OSError) as exc:
        parser.exit(2, f"Compact split refused: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
