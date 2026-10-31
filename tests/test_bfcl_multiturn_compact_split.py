"""Offline compact-scope scenarios: frozen exposure, grouping and score blindness."""
from __future__ import annotations

import importlib.util
import json
import shutil
from collections import Counter
from pathlib import Path

import pytest
from bfcl_test_prerequisites import require_corpus

PROJECT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "bfcl_compact_split", PROJECT / "evals/bfcl_multiturn/splits/create_compact_split.py",
)
split = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(split)


@pytest.fixture
def corpus(tmp_path):
    require_corpus()
    frozen = PROJECT / split.FROZEN
    original = json.loads((frozen / "split-manifest.json").read_bytes())
    source_paths = {split.FROZEN / name for name in (
        "split-manifest.json", "development-ids.txt", "holdout-ids.txt", "regression20-ids.txt",
    )}
    source_paths.add(Path("artifacts/bfcl/v4-coverage-20261029/provenance.json"))
    for task in original["tasks"]:
        for kind in ("question", "answer"):
            source_paths.add(Path(task[kind + "_source"]["path"]))
    for relative in source_paths:
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PROJECT / relative, destination)
    return tmp_path, original


def _manifest(files):
    return json.loads(files["split-manifest.json"])


@pytest.mark.bfcl_evidence
def test_compact100_balanced_exposure_and_family_boundary(corpus):
    root, original = corpus
    files = split.build_files(root)
    manifest = _manifest(files)
    development = set(files["development-ids.txt"].decode().splitlines())
    holdout = set(files["holdout-ids.txt"].decode().splitlines())
    old_development = {task["id"] for task in original["tasks"] if task["split"] == "development"}
    old_holdout = {task["id"] for task in original["tasks"] if task["split"] == "holdout"}
    assert len(development) == 60 and len(holdout) == 40
    assert development.isdisjoint(holdout)
    assert development <= old_development and holdout <= old_holdout
    assert len(manifest["tasks"]) == 100 and manifest["counts"]["inactive"] == 700
    for label, ids, per_category in (("development", development, 15), ("holdout", holdout, 10)):
        counts = Counter(task["category"] for task in manifest["tasks"] if task["split"] == label)
        assert counts == {category: per_category for category in split.CATEGORIES}
        for family in manifest["families"]:
            if family["split"] == label:
                assert set(family["members"]) <= ids
    assert not any(task_id.endswith("_173") for task_id in development | holdout)
    prior = set((root / split.FROZEN / "regression20-ids.txt").read_text().splitlines())
    assert not prior <= development
    assert manifest["counts"]["historical_regression20_retained_in_active_development"] == len(prior & development)
    assert all("question" not in task and "ground_truth" not in task for task in manifest["tasks"])
    assert manifest["model_api_calls"] == manifest["judge_api_calls"] == 0
    assert manifest["capability_score"] is None


@pytest.mark.bfcl_evidence
def test_seed_stable_and_alternative_seed_preserves_exposure_pools(corpus):
    root, _ = corpus
    first = split.build_files(root)
    assert split.build_files(root) == first
    second = split.build_files(root, seed=split.SEED + 1)
    assert second["development-ids.txt"] != first["development-ids.txt"]
    assert second["holdout-ids.txt"] != first["holdout-ids.txt"]
    assert _manifest(second)["sampling"]["outcomes_used"] is False


@pytest.mark.bfcl_evidence
def test_scores_are_not_read_or_used(corpus, monkeypatch):
    root, _ = corpus
    original_read = Path.read_bytes
    opened = []

    def guarded_read(path):
        opened.append(path.as_posix())
        assert path.name not in {"results.json", "report.json", "verified-baseline-summary.json"}
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", guarded_read)
    first = split.build_files(root)
    fabricated = root / "artifacts/bfcl/dev200-c100-20261030-r2/results/results.json"
    fabricated.parent.mkdir(parents=True)
    fabricated.write_text('{"results": [{"id": "multi_turn_base_3", "score": 0}]}')
    assert split.build_files(root) == first
    assert all("dev200-c100" not in path for path in opened)


@pytest.mark.parametrize("relative", [
    split.FROZEN / "split-manifest.json",
    split.FROZEN / "development-ids.txt",
    Path("artifacts/bfcl/v4-coverage-20261029/raw/berkeley-function-call-leaderboard/bfcl_eval/data/possible_answer/BFCL_v4_multi_turn_base.json"),
])
@pytest.mark.bfcl_evidence
def test_source_tamper_rejected_without_output(corpus, relative):
    root, _ = corpus
    source = root / relative
    source.write_bytes(source.read_bytes() + b"\n")
    with pytest.raises(split.SplitError, match="changed"):
        split.build_files(root)
    assert not (root / "new-scope").exists()


@pytest.mark.bfcl_evidence
def test_never_overwrite_and_frozen_tamper_detected(corpus):
    root, _ = corpus
    files = split.build_files(root)
    output = root / "new-scope"
    split.write_split(output, files)
    assert split.validate_split(output, files)["active"] == 100
    with pytest.raises(split.SplitError, match="already exists"):
        split.write_split(output, files)
    (output / "holdout-ids.txt").write_bytes(files["development-ids.txt"])
    with pytest.raises(split.SplitError, match="differs"):
        split.validate_split(output, files)


@pytest.mark.parametrize("seed", [-1, True, "20261030"])
@pytest.mark.bfcl_evidence
def test_invalid_seed_refused(corpus, seed):
    root, _ = corpus
    with pytest.raises(split.SplitError, match="nonnegative integer"):
        split.build_files(root, seed=seed)
