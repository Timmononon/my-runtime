"""Frozen holdout selection, source authority and isolated Runtime evidence."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
from collections import Counter
from pathlib import Path

import pytest
from bfcl_test_prerequisites import require_corpus, require_frozen_runtimes

PROJECT = Path(__file__).resolve().parents[1]
HERE = PROJECT / "evals/bfcl_multiturn/promptfoo"


def module(name):
    spec = importlib.util.spec_from_file_location("holdout_test_" + name, HERE / (name + ".py"))
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


dataset = module("compact_holdout_dataset")
runtime = module("frozen_holdout_runtime")


@pytest.fixture(scope="module")
def selection():
    require_corpus()
    return dataset.load_holdout_dataset()


@pytest.fixture(scope="module")
def materialized(tmp_path_factory):
    require_frozen_runtimes()
    parent = tmp_path_factory.mktemp("frozen-holdout-runtimes")
    roots = {}
    for label in runtime.VERSIONS:
        root = parent / label
        runtime.materialize_runtime(label, root)
        roots[label] = root
    return roots


@pytest.mark.bfcl_evidence
def test_exact_sixty_original_holdout_tasks_in_pinned_family_order(selection):
    cases, meta = selection
    ids = (dataset.FROZEN / "holdout-ids.txt").read_text().splitlines()
    dev = set((dataset.FROZEN / "development-ids.txt").read_text().splitlines())
    original_holdout = set((PROJECT / "evals/bfcl_multiturn/splits/frozen/holdout-ids.txt").read_text().splitlines())
    assert [case["id"] for case in cases] == ids and len(ids) == len(set(ids)) == 60
    assert set(ids) <= original_holdout and not set(ids) & dev
    assert Counter(case["category"] for case in cases) == dict.fromkeys(dataset.CATEGORIES, 15)
    assert len({task_id.rsplit("_", 1)[1] for task_id in ids}) == 15
    assert meta["evaluation_split"] == "holdout" and meta["defined_user_turns"] == 262
    assert meta["allocated_development_tasks"] == 40 and meta["allocated_holdout_tasks"] == 60
    assert meta["evaluation_task_count"] == meta["admissible_task_count"] == 60
    assert meta["known_admission_conflicts"] == []
    assert meta["model_api_calls"] == meta["judge_api_calls"] == meta["holdout_inference_tasks"] == 0
    assert all(case["provenance"]["evaluation_split"] == "holdout" for case in cases)
    assert [group["group"] for group in meta["groups"]] == ids


@pytest.mark.bfcl_evidence
def test_loader_never_resolves_user_runtime(monkeypatch):
    require_corpus()
    import aster.paths

    def forbidden(*args, **kwargs):
        raise AssertionError("Dataset reads must not resolve a user's Runtime")

    monkeypatch.setattr(aster.paths, "resolve_data_dir", forbidden)
    assert len(dataset.load_holdout_cases()) == 60


@pytest.mark.parametrize("filename", ["holdout-ids.txt", "development-ids.txt"])
def test_changed_membership_refused_before_any_execution(tmp_path, filename):
    frozen = tmp_path / "compact"
    shutil.copytree(dataset.FROZEN, frozen)
    target = frozen / filename
    target.write_bytes(target.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="membership changed"):
        dataset.load_holdout_dataset(frozen=frozen)


def test_changed_compact_manifest_refused(tmp_path):
    frozen = tmp_path / "compact"
    shutil.copytree(dataset.FROZEN, frozen)
    target = frozen / "split-manifest.json"
    target.write_bytes(target.read_bytes() + b"\n")
    with pytest.raises(ValueError, match="manifest changed"):
        dataset.load_holdout_dataset(frozen=frozen)


@pytest.mark.bfcl_evidence
def test_changed_raw_answer_source_refused(tmp_path):
    require_corpus()
    project = tmp_path / "source-project"
    shutil.copytree(PROJECT / "evals/bfcl_multiturn/splits", project / "evals/bfcl_multiturn/splits")
    parent = json.loads((project / "evals/bfcl_multiturn/splits/frozen/split-manifest.json").read_bytes())
    for name in parent["sources"]:
        target = project / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PROJECT / name, target)
    name = next(name for name in parent["sources"] if "/possible_answer/" in name)
    (project / name).write_bytes((project / name).read_bytes() + b"\n")
    with pytest.raises(ValueError, match="source bytes changed"):
        dataset.load_holdout_dataset(project=project, frozen=project / "evals/bfcl_multiturn/splits/compact100-v2")


@pytest.mark.bfcl_evidence
def test_three_materialized_engines_keep_distinct_frozen_bytes_and_share_only_backends(materialized):
    loop_hashes = set()
    for label, root in materialized.items():
        manifest = runtime.validate_runtime(root)
        assert manifest["configuration_label"] == label
        assert manifest["source_commit"] == runtime.VERSIONS[label]["source_commit"]
        records = {item["file"]: item for item in manifest["frozen_files"] + manifest["overlays"]}
        loop_hashes.add(records["src/aster/engine/loop.py"]["sha256"])
        assert records["src/aster/service/coordinator.py"]["role"] == "frozen_runtime"
        assert records["src/aster/evaluation/bfcl_v4_runner.py"]["role"] == "frozen_runtime"
        assert {item["file"] for item in manifest["overlays"] if item["file"].startswith("src/")} == runtime.SHARED_BACKENDS
        assert not any(Path(item["file"]).name in {"credentials.yaml", "runtime.db", ".env"}
                       for item in records.values())
    assert len(loop_hashes) == 3
    for name in runtime.SHARED_BACKENDS:
        assert len({(root / name).read_bytes() for root in materialized.values()}) == 1


@pytest.mark.bfcl_evidence
def test_materialization_never_overwrites_an_existing_workspace(materialized):
    root = materialized["A0"]
    before = (root / "frozen-runtime-manifest.json").read_bytes()
    with pytest.raises(ValueError, match="must be new"):
        runtime.materialize_runtime("A0", root)
    assert (root / "frozen-runtime-manifest.json").read_bytes() == before


@pytest.mark.bfcl_evidence
def test_forging_new_engine_hash_cannot_replace_historical_authority(tmp_path, materialized):
    root = tmp_path / "forged"
    shutil.copytree(materialized["A0"], root)
    path = root / "frozen-runtime-manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest["project_root"] = str(root.resolve())
    target = root / "src/aster/engine/loop.py"
    target.write_bytes(target.read_bytes() + b"\n# unauthorized policy change\n")
    record = next(item for item in manifest["frozen_files"] if item["file"] == "src/aster/engine/loop.py")
    record.update(sha256=runtime._sha(target.read_bytes()), bytes=target.stat().st_size)
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="Agent source was replaced"):
        runtime.validate_runtime(root)


@pytest.mark.bfcl_evidence
def test_extra_runtime_module_is_not_silently_importable(tmp_path, materialized):
    root = tmp_path / "expanded"
    shutil.copytree(materialized["A0"], root)
    path = root / "frozen-runtime-manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest["project_root"] = str(root.resolve())
    path.write_text(json.dumps(manifest), encoding="utf-8")
    (root / "src/aster/unrecorded.py").write_text("EXTRA = True\n", encoding="utf-8")
    with pytest.raises(ValueError, match="source membership changed"):
        runtime.validate_runtime(root)


@pytest.mark.bfcl_evidence
def test_main_workspace_aster_imports_cannot_be_relabelled_as_frozen(materialized):
    before_cwd, before_env = Path.cwd(), dict(os.environ)
    with pytest.raises(ValueError, match="escaped its frozen runtime"):
        runtime.capture_import_origins(materialized["A0"])
    assert Path.cwd() == before_cwd and dict(os.environ) == before_env


def test_unknown_configuration_is_not_accepted(tmp_path):
    with pytest.raises(ValueError, match="three explicitly frozen"):
        runtime.materialize_runtime("current", tmp_path / "not-a-baseline")
