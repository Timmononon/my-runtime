"""Offline split scenarios: exposure closure, provenance, and frozen membership."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "bfcl_multiturn_split", Path(__file__).resolve().parents[1]
    / "evals/bfcl_multiturn/splits/create_split.py",
)
split = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(split)

PRIOR = {
    "base": (151, 173, 95, 123, 160),
    "miss_func": (54, 178, 188, 141, 41),
    "miss_param": (107, 51, 134, 24, 100),
    "long_context": (29, 153, 91, 22, 132),
}


def _json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(split.json_bytes(value))


def _jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\n".join(split.canonical(row) for row in rows) + b"\n")


def _repin(root, relative):
    path = root / split.CORPUS / "provenance.json"
    provenance = json.loads(path.read_bytes())
    for entry in provenance["files"]:
        if entry["local_file"] == relative.relative_to(split.CORPUS).as_posix():
            entry["sha256"] = split.sha256((root / relative).read_bytes())
    _json(path, provenance)


@pytest.fixture
def corpus(tmp_path):
    entries, prior = [], []
    for category in split.CATEGORIES:
        questions = [{
            "id": f"multi_turn_{category}_{index}",
            "question": [[{"role": "user", "content": f"Task {index} {category if category.startswith('miss') else 'base'}"}]],
            "initial_config": {"Example": {"value": index}},
            "path": ["Example"], "involved_classes": ["Example"], "excluded_function": [],
        } for index in range(200)]
        # Official excluded_function metadata is optional, with the same
        # omission across the four variants of a source task.
        for index, row in enumerate(questions):
            if index % 3:
                row.pop("excluded_function")
        answers = [{"id": row["id"], "ground_truth": [[f"set_value({index})"]]} for index, row in enumerate(questions)]
        filename = f"BFCL_v4_multi_turn_{category}.json"
        for relative, rows in (
            (split.DATA / filename, questions),
            (split.DATA / "possible_answer" / filename, answers),
        ):
            _jsonl(tmp_path / relative, rows)
            entries.append({
                "local_file": relative.relative_to(split.CORPUS).as_posix(),
                "revision": split.REVISION, "sha256": split.sha256((tmp_path / relative).read_bytes()),
            })
        _jsonl(tmp_path / split.CORPUS / "samples/questions" / f"BFCL_v4_multi_turn_{category}.jsonl", [questions[i] for i in PRIOR[category]])
        prior.extend(questions[i]["id"] for i in PRIOR[category])
    _json(tmp_path / split.CORPUS / "provenance.json", {"files": entries})
    _json(tmp_path / split.CAMPAIGN / "campaign-plan.json", {"groups": [{"entry_ids": [task_id]} for task_id in prior]})
    _json(tmp_path / split.CAMPAIGN / "results/results.json", {"results": [{"id": task_id} for task_id in prior]})
    return tmp_path, prior


def test_exact_stratification_preserves_prior_exposure_and_variants(corpus):
    root, prior = corpus
    files = split.build_files(root)
    manifest = json.loads(files["split-manifest.json"])
    development = set(files["development-ids.txt"].decode().splitlines())
    holdout = set(files["holdout-ids.txt"].decode().splitlines())
    assert len(development) == 200 and len(holdout) == 600
    assert development.isdisjoint(holdout)
    assert development | holdout == {row["id"] for row in manifest["tasks"]}
    assert set(prior) <= development
    assert files["regression20-ids.txt"].decode().splitlines() == prior
    assert manifest["counts"]["prior_exposure_family_closure_tasks"] == 80
    for category in split.CATEGORIES:
        assert sum(task.startswith(f"multi_turn_{category}_") for task in development) == 50
        assert sum(task.startswith(f"multi_turn_{category}_") for task in holdout) == 150
    for family in manifest["families"]:
        assert all(task in (development if family["split"] == "development" else holdout) for task in family["members"])
    assert manifest["quarantine"][0]["id"] == split.KNOWN_CONFLICT
    assert manifest["quarantine"][0]["score"] is None
    assert manifest["quarantine"][0]["replacement"] is None
    assert manifest["model_api_calls"] == 0
    assert manifest["judge_api_calls"] == 0
    assert all("question" not in row and "ground_truth" not in row for row in manifest["tasks"])


def test_seed_is_stable_and_different_seed_keeps_exposed_families(corpus):
    root, prior = corpus
    original = split.build_files(root)
    assert split.build_files(root) == original
    another = split.build_files(root, seed=split.SEED + 1)
    assert another["development-ids.txt"] != original["development-ids.txt"]
    assert set(prior) <= set(another["development-ids.txt"].decode().splitlines())
    assert json.loads(original["split-manifest.json"])["sampling"]["algorithm"] == "sha256_seeded_family_rank_v1"


def test_pinned_source_change_refuses_before_output(corpus):
    root, _ = corpus
    source = root / split.DATA / "BFCL_v4_multi_turn_base.json"
    source.write_bytes(source.read_bytes() + b"\n")
    with pytest.raises(split.SplitError, match="pinned source mismatch"):
        split.build_files(root)
    assert not (root / "output").exists()


def test_duplicate_source_ids_refuse_even_with_updated_file_provenance(corpus):
    root, _ = corpus
    relative = split.DATA / "possible_answer/BFCL_v4_multi_turn_base.json"
    rows = [row for row, _ in split.read_rows(root / relative)]
    rows[1]["id"] = rows[0]["id"]
    _jsonl(root / relative, rows)
    _repin(root, relative)
    with pytest.raises(split.SplitError, match="Duplicate task ID"):
        split.build_files(root)


def test_prior_campaign_disagreement_refuses(corpus):
    root, prior = corpus
    _json(root / split.CAMPAIGN / "results/results.json", {"results": [{"id": task_id} for task_id in prior[:-1]]})
    with pytest.raises(split.SplitError, match="same 20 unique tasks"):
        split.build_files(root)


def test_cross_family_exact_duplicate_refuses(corpus):
    root, _ = corpus
    relative = split.DATA / "BFCL_v4_multi_turn_base.json"
    rows = [row for row, _ in split.read_rows(root / relative)]
    rows[1]["question"] = rows[0]["question"]
    _jsonl(root / relative, rows)
    _repin(root, relative)
    with pytest.raises(split.SplitError, match="distinct source families"):
        split.build_files(root)


def test_frozen_outputs_never_overwrite_and_tampering_is_detected(corpus):
    root, _ = corpus
    files = split.build_files(root)
    output = root / "frozen"
    split.write_split(output, files)
    assert split.validate_split(output, files)["valid"] is True
    with pytest.raises(split.SplitError, match="already exists"):
        split.write_split(output, files)
    assert (output / "development-ids.txt").read_bytes() == files["development-ids.txt"]
    (output / "holdout-ids.txt").write_bytes(files["development-ids.txt"])
    with pytest.raises(split.SplitError, match="differs"):
        split.validate_split(output, files)
