"""Freeze the local BFCL V4 Multi-Turn development/holdout IDs, offline only.

No model clients, Runtime, network, or upstream executable code are imported.
Question/answer bodies are read only to validate source identity and grouping;
the output contains IDs, hashes, and provenance metadata only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
REVISION = "6ea57973c7a6097fd7c5915698c54c17c5b1b6c8"
SEED = 20261030
DOMAIN = "aster-bfcl-v4-multiturn-split-v1"
CATEGORIES = ("base", "miss_func", "miss_param", "long_context")
CORPUS = Path("artifacts/bfcl/v4-coverage-20261029")
DATA = CORPUS / "raw/berkeley-function-call-leaderboard/bfcl_eval/data"
CAMPAIGN = Path("artifacts/bfcl/multiturn20-no-thinking-rerun-20261030")
KNOWN_CONFLICT = "multi_turn_base_173"
NAMES = (
    "development-ids.txt", "holdout-ids.txt", "regression20-ids.txt",
    "split-manifest.json",
)


class SplitError(ValueError):
    """Source or frozen membership differs from this split's contract."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def read_json(path: Path) -> object:
    try:
        return json.loads(path.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SplitError(f"Cannot read JSON source: {path.name}") from exc


def read_rows(path: Path) -> list[tuple[dict, dict]]:
    try:
        lines = path.read_bytes().splitlines()
    except OSError as exc:
        raise SplitError(f"Cannot read source: {path.name}") from exc
    rows = []
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise SplitError(f"Invalid source JSON: {path.name}, line {line_number}") from exc
        if not isinstance(row, dict) or not isinstance(row.get("id"), str):
            raise SplitError(f"Invalid source row: {path.name}, line {line_number}")
        rows.append((row, {
            "line": line_number,
            "line_sha256_without_line_ending": sha256(line),
            "record_sha256_canonical_json": sha256(canonical(row)),
        }))
    return rows


def indexed_rows(path: Path, category: str, *, answers: bool = False) -> dict[int, tuple[dict, dict]]:
    result = {}
    prefix = f"multi_turn_{category}_"
    for row, source in read_rows(path):
        task_id = row["id"]
        suffix = task_id.removeprefix(prefix)
        if not task_id.startswith(prefix) or not suffix.isdecimal() or str(int(suffix)) != suffix:
            raise SplitError(f"Unexpected task ID in {path.name}")
        index = int(suffix)
        if index in result:
            raise SplitError(f"Duplicate task ID in {path.name}")
        if answers:
            if not isinstance(row.get("ground_truth"), list):
                raise SplitError(f"Missing answer structure in {path.name}")
        elif not (
            isinstance(row.get("question"), list) and row["question"]
            and isinstance(row.get("initial_config"), dict)
            and isinstance(row.get("path"), list)
            and isinstance(row.get("involved_classes"), list)
            and ("excluded_function" not in row or isinstance(row["excluded_function"], list))
        ):
            raise SplitError(f"Missing question structure in {path.name}")
        result[index] = (row, source)
    if set(result) != set(range(200)):
        raise SplitError(f"Expected exactly task indices 0..199 in {path.name}")
    return result


def build_files(repo_root: Path = REPO_ROOT, seed: int = SEED) -> dict[str, bytes]:
    """Build deterministic files without writing anything or evaluating tasks."""
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise SplitError("Seed must be a nonnegative integer")
    repo_root = repo_root.resolve()
    sources = {}

    def bind(relative: Path) -> str:
        name = relative.as_posix()
        try:
            data = (repo_root / relative).read_bytes()
        except OSError as exc:
            raise SplitError(f"Missing bound source: {name}") from exc
        sources[name] = {"sha256": sha256(data), "bytes": len(data)}
        return name

    provenance_path = CORPUS / "provenance.json"
    bind(provenance_path)
    provenance = read_json(repo_root / provenance_path)
    if not isinstance(provenance, dict) or not isinstance(provenance.get("files"), list):
        raise SplitError("Invalid original provenance")
    pinned = {item.get("local_file"): item for item in provenance["files"] if isinstance(item, dict)}
    questions, answers, source_names = {}, {}, {}
    for category in CATEGORIES:
        filename = f"BFCL_v4_multi_turn_{category}.json"
        for kind, relative in (("question", DATA / filename), ("answer", DATA / "possible_answer" / filename)):
            name = bind(relative)
            original_name = relative.relative_to(CORPUS).as_posix()
            entry = pinned.get(original_name, {})
            if entry.get("revision") != REVISION or entry.get("sha256") != sources[name]["sha256"]:
                raise SplitError(f"Original pinned source mismatch: {name}")
            rows = indexed_rows(repo_root / relative, category, answers=kind == "answer")
            (questions if kind == "question" else answers)[category] = rows
            source_names[category, kind] = name

    plan_path = CAMPAIGN / "campaign-plan.json"
    results_path = CAMPAIGN / "results/results.json"
    bind(plan_path)
    bind(results_path)
    plan, results = read_json(repo_root / plan_path), read_json(repo_root / results_path)
    try:
        prior_ids = [task_id for group in plan["groups"] for task_id in group["entry_ids"]]
        result_ids = [row["id"] for row in results["results"]]
    except (KeyError, TypeError) as exc:
        raise SplitError("Invalid prior campaign structure") from exc
    if len(prior_ids) != 20 or len(set(prior_ids)) != 20 or set(prior_ids) != set(result_ids) or len(result_ids) != 20:
        raise SplitError("Prior campaign must identify the same 20 unique tasks")

    all_ids = {row["id"] for rows in questions.values() for row, _ in rows.values()}
    if not set(prior_ids) <= all_ids or KNOWN_CONFLICT not in prior_ids:
        raise SplitError("Prior campaign or known quarantine is outside the source tasks")
    sample_ids = set()
    for category in CATEGORIES:
        relative = CORPUS / "samples/questions" / f"BFCL_v4_multi_turn_{category}.jsonl"
        bind(relative)
        sample = read_rows(repo_root / relative)
        if len(sample) != 5:
            raise SplitError("Prior sample must contain five tasks per category")
        for row, _ in sample:
            task_id = row["id"]
            expected = {item[0]["id"]: item[0] for item in questions[category].values()}
            if task_id not in expected or canonical(row) != canonical(expected[task_id]) or task_id in sample_ids:
                raise SplitError("Prior sample differs from original task source")
            sample_ids.add(task_id)
    if sample_ids != set(prior_ids):
        raise SplitError("Prior sample and executed campaign disagree")

    # Suffix membership is deliberately conservative. Do not split modified
    # missing-function/parameter and long-context variants of one source task.
    relations = {}
    exact_question_groups = defaultdict(list)
    for category in CATEGORIES:
        relations[category] = {
            field: sum(questions["base"][i][0].get(field) == questions[category][i][0].get(field) for i in range(200))
            for field in ("path", "involved_classes", "excluded_function", "initial_config", "question")
        }
        if any(relations[category][field] != 200 for field in ("path", "involved_classes", "excluded_function")):
            raise SplitError("Source-family structural mapping changed")
        for index, (row, _) in questions[category].items():
            exact_question_groups[sha256(canonical(row["question"]))].append(index)
    if any(len(set(indices)) > 1 for indices in exact_question_groups.values()):
        raise SplitError("Exact duplicate question appears across distinct source families")

    fixed_families = sorted({int(task_id.rsplit("_", 1)[1]) for task_id in prior_ids})
    if len(fixed_families) > 50:
        raise SplitError("Prior exposure exceeds development-family capacity")

    def rank(index: int) -> tuple[str, int]:
        return sha256(f"{DOMAIN}\0{seed}\0{index}".encode()), index

    eligible = sorted(set(range(200)) - set(fixed_families), key=rank)
    selected = eligible[:50 - len(fixed_families)]
    development = set(fixed_families) | set(selected)
    holdout = set(range(200)) - development
    tasks, development_ids, holdout_ids, families = [], [], [], []
    for category in CATEGORIES:
        for index in range(200):
            row, question_source = questions[category][index]
            _, answer_source = answers[category][index]
            task_id = row["id"]
            split = "development" if index in development else "holdout"
            (development_ids if index in development else holdout_ids).append(task_id)
            tasks.append({
                "id": task_id, "category": f"multi_turn_{category}",
                "family_index": index, "split": split,
                "previously_selected_20": task_id in prior_ids,
                "scoring_eligibility": "quarantined_known_conflict" if task_id == KNOWN_CONFLICT else "not_prevalidated",
                "question_source": {"path": source_names[category, "question"], **question_source},
                "answer_source": {"path": source_names[category, "answer"], **answer_source},
            })
    for index in range(200):
        families.append({
            "family_index": index, "split": "development" if index in development else "holdout",
            "fixed_by_prior_exposure": index in fixed_families,
            "members": [f"multi_turn_{category}_{index}" for category in CATEGORIES],
        })
    text_files = {
        "development-ids.txt": ("\n".join(development_ids) + "\n").encode("utf-8"),
        "holdout-ids.txt": ("\n".join(holdout_ids) + "\n").encode("utf-8"),
        "regression20-ids.txt": ("\n".join(prior_ids) + "\n").encode("utf-8"),
    }
    manifest = {
        "schema_version": 1, "dataset": "BFCL V4 Multi-Turn", "source_revision": REVISION,
        "seed": seed,
        "sampling": {
            "algorithm": "sha256_seeded_family_rank_v1", "domain": DOMAIN,
            "rank_input": "UTF-8(domain + NUL + decimal(seed) + NUL + decimal(family_index))",
            "sort": "ascending SHA-256 hex digest, then ascending integer family_index",
            "selection": "Pin all prior-20 source families; select the first remaining ranks to reach 50 development families",
            "fixed_development_families": fixed_families,
            "additional_development_families_in_rank_order": selected,
            "development_families": sorted(development), "holdout_families": sorted(holdout),
            "prior_20_ids_in_campaign_order": prior_ids,
        },
        "counts": {
            "total": 800, "development": 200, "holdout": 600,
            "families": 200, "development_families": 50, "holdout_families": 150,
            "prior_selected_tasks": 20, "prior_exposure_family_closure_tasks": 4 * len(fixed_families),
            "known_quarantined_development_tasks": 1,
            "per_category": {f"multi_turn_{category}": {"total": 200, "development": 50, "holdout": 150} for category in CATEGORIES},
        },
        "grouping_audit": {
            "rule": "Bind all four category variants with the same integer source-task suffix to one split",
            "structural_equivalence_to_base_counts": relations,
            "exact_question_duplicate_groups": sum(len(indices) > 1 for indices in exact_question_groups.values()),
            "exact_question_duplicates_across_source_families": 0,
            "semantic_near_duplicate_audit": "not_performed; no model or embedding calls",
            "limitation": "Mechanical source-family separation is not proof of complete semantic independence",
        },
        "quarantine": [{
            "id": KNOWN_CONFLICT, "split": "development", "score": None,
            "reason": "Previously documented ticket_id schema/gold type conflict; original data remains unchanged",
            "replacement": None,
        }],
        "policy": {
            "authority": "This frozen manifest and its three ID lists; artifacts contain verification evidence only",
            "source_bodies": "Never copied into split outputs; answers remain evaluator-only",
            "membership": "Frozen; do not move tasks between splits based on outcomes",
            "counts": "Allocation counts, not validated/scorable-task counts; other 799 tasks are not certified conflict-free",
            "holdout": "No model inference or capability evaluation performed by splitting; use only after configuration freeze",
        },
        "model_api_calls": 0, "judge_api_calls": 0, "capability_score": None,
        "sources": sources,
        "generator": {"path": "evals/bfcl_multiturn/splits/create_split.py", "sha256": sha256(Path(__file__).read_bytes())},
        "id_files": {name: {"sha256": sha256(data), "lines": len(data.splitlines())} for name, data in text_files.items()},
        "families": families, "tasks": tasks,
    }
    return {**text_files, "split-manifest.json": json_bytes(manifest)}


def write_split(output: Path, files: dict[str, bytes]) -> None:
    """Write only into a new directory after source checks have completed."""
    if output.exists():
        raise SplitError("Output already exists; frozen files are never overwritten")
    output.mkdir(parents=True, exist_ok=False)
    for name in NAMES:
        with (output / name).open("xb") as handle:
            handle.write(files[name])


def validate_split(directory: Path, files: dict[str, bytes]) -> dict:
    for name in NAMES:
        try:
            actual = (directory / name).read_bytes()
        except OSError as exc:
            raise SplitError(f"Missing frozen output: {name}") from exc
        if actual != files[name]:
            raise SplitError(f"Frozen output differs from reproducible source: {name}")
    return {
        "valid": True, "seed": json.loads(files["split-manifest.json"])["seed"],
        "development": 200, "holdout": 600, "regression": 20,
        "model_api_calls": 0, "judge_api_calls": 0,
        "files": {name: sha256(data) for name, data in files.items()},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--output", type=Path, help="Create a new split directory; never overwrite")
    mode.add_argument("--validate", type=Path, help="Recompute and byte-compare an existing split")
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args(argv)
    try:
        files = build_files(seed=args.seed)
        if args.output is not None:
            write_split(args.output, files)
            directory = args.output
        else:
            directory = args.validate
        print(json.dumps(validate_split(directory, files), indent=2, sort_keys=True))
    except (SplitError, OSError) as exc:
        parser.exit(2, f"Split refused: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
