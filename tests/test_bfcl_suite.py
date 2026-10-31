import copy
import hashlib
import json

import pytest

from aster.evaluation import bfcl_suite
from aster.evaluation.bfcl import load_cases
from aster.evaluation.bfcl_dataset import CATEGORIES


@pytest.fixture
def pool_fixture(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    license_content = b"Synthetic test license."
    (source / "LICENSE-GORILLA.txt").write_bytes(license_content)
    monkeypatch.setattr(bfcl_suite, "LICENSE_SHA256", hashlib.sha256(license_content).hexdigest())
    questions, answers, cases = {}, {}, {}
    for category in CATEGORIES:
        for number in range(45):
            case_id = f"{category}_{number}"
            function = {"name": f"{category}_function_{number}", "description": "Synthetic function.",
                        "parameters": {"type": "dict", "properties": {"value": {"type": "integer"}},
                                       "required": ["value"]}}
            prompt = f"Synthetic question {category} {number}"
            questions[case_id] = {"id": case_id, "question": [[{"role": "user", "content": prompt}]],
                                  "function": [function]}
            answers[case_id] = {"id": case_id, "ground_truth": [{function["name"]: {"value": [number]}}]}
            cases[case_id] = {"id": case_id, "category": category, "prompt": prompt, "functions": [function]}
    # Include a pilot and a cousin in a different category, connected through
    # a shared tool, plus a third task linked only through normalized prompt.
    pilot = "simple_python_92"
    cases[pilot] = copy.deepcopy(cases["simple_python_0"])
    cases[pilot]["id"] = pilot
    cases[pilot]["prompt"] = "Previously observed pilot"
    questions[pilot] = copy.deepcopy(questions["simple_python_0"])
    questions[pilot]["id"] = pilot
    answers[pilot] = copy.deepcopy(answers["simple_python_0"])
    answers[pilot]["id"] = pilot
    cases["multiple_0"]["functions"].append(copy.deepcopy(cases[pilot]["functions"][0]))
    cases["parallel_0"]["prompt"] = "  SYNTHETIC   QUESTION multiple 0 "
    pool = {"questions": questions, "answers": answers, "cases": cases,
            "sources": [{"file": "fixture.jsonl", "sha256": "fixture-only", "records": len(cases)}],
            "quarantined": [{"id": "parallel_multiple_12", "reason": "undeclared parameter"},
                            {"id": "parallel_multiple_26", "reason": "undeclared parameter"}],
            "revision": "fixture-revision"}
    # Real pool excludes quarantine IDs; preserve that loader contract.
    for case_id in ("parallel_multiple_12", "parallel_multiple_26"):
        for rows in (questions, answers, cases):
            rows.pop(case_id)
    original = b"immutable synthetic input\n"
    (source / "fixture.jsonl").write_bytes(original)
    monkeypatch.setattr(bfcl_suite, "SOURCE_HASHES", {"fixture.jsonl": hashlib.sha256(original).hexdigest()})
    monkeypatch.setattr(bfcl_suite, "load_pinned_pool", lambda directory: copy.deepcopy(pool))
    return source, pool


def test_fixed_counts_no_exposure_no_leak_and_originals_preserved(pool_fixture, tmp_path):
    source, pool = pool_fixture
    before = {path.name: path.read_bytes() for path in source.iterdir()}
    output = tmp_path / "suite"
    manifest = bfcl_suite.prepare_suite(source, output)
    assert manifest["counts"]["all"] == 100
    assert manifest["counts"]["dev"] == 40 and manifest["counts"]["holdout"] == 60
    selected = manifest["cases"]
    assert len({entry["id"] for entry in selected}) == 100
    assert len({entry["source_group"] for entry in selected}) == 100
    for category in CATEGORIES:
        assert sum(entry["category"] == category and entry["split"] == "dev" for entry in selected) == 10
        assert sum(entry["category"] == category and entry["split"] == "holdout" for entry in selected) == 15
    ids = {entry["id"] for entry in selected}
    assert not ids & bfcl_suite.PILOT_IDS
    assert not ids & {"simple_python_0", "multiple_0", "parallel_0", "parallel_multiple_12", "parallel_multiple_26"}
    dev_groups = {entry["source_group"] for entry in selected if entry["split"] == "dev"}
    holdout_groups = {entry["source_group"] for entry in selected if entry["split"] == "holdout"}
    assert not dev_groups & holdout_groups
    assert {entry["id"] for entry in manifest["pilot_exposure_exclusions"]} == {
        "simple_python_92", "simple_python_0", "multiple_0", "parallel_0"}
    assert before == {path.name: path.read_bytes() for path in source.iterdir()}
    assert manifest["model_requests"] == 0 and manifest["paid_calls_authorized"] is False
    assert (output / "LICENSE-GORILLA.txt").read_bytes() == before["LICENSE-GORILLA.txt"]
    assert "ground_truth" not in (output / "manifest.json").read_text(encoding="utf-8")
    for split, count in (("all", 100), ("dev", 40), ("holdout", 60)):
        data_path = output / f"BFCL_v4_{split}.jsonl"
        answer_path = output / f"BFCL_v4_{split}.answers.jsonl"
        cases = load_cases(data_path, answer_path)
        assert len(cases) == count
        assert "ground_truth" not in data_path.read_text(encoding="utf-8")
        for line in data_path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            assert row == pool["questions"][row["id"]]
        for line in answer_path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            assert row == pool["answers"][row["id"]]
    for name, info in manifest["files"].items():
        assert hashlib.sha256((output / name).read_bytes()).hexdigest() == info["sha256"]


def test_freeze_is_byte_deterministic_and_not_answer_selected(pool_fixture, tmp_path):
    source, pool = pool_fixture
    first = tmp_path / "first"
    second = tmp_path / "second"
    bfcl_suite.prepare_suite(source, first)
    bfcl_suite.prepare_suite(source, second)
    assert {path.name: path.read_bytes() for path in first.iterdir()} == {
        path.name: path.read_bytes() for path in second.iterdir()}
    selected = json.loads((first / "manifest.json").read_text(encoding="utf-8"))["cases"]
    selected_ids = [entry["id"] for entry in selected]
    for answer in pool["answers"].values():
        answer["ground_truth"] = [{"different_answer": {"value": [999999]}}]
    changed = bfcl_suite.prepare_suite(source, tmp_path / "changed-answers")
    assert [entry["id"] for entry in changed["cases"]] == selected_ids


def test_source_groups_join_any_shared_tool_transitively_without_mutation(pool_fixture):
    _source, pool = pool_fixture
    original = copy.deepcopy(pool["cases"])
    groups, by_id = bfcl_suite.source_groups(pool["cases"])
    assert by_id["simple_python_92"] == by_id["simple_python_0"] == by_id["multiple_0"] == by_id["parallel_0"]
    assert len(groups[by_id["simple_python_92"]]) == 4
    assert pool["cases"] == original


def test_rejects_existing_or_source_related_paths(pool_fixture, tmp_path):
    source, _pool = pool_fixture
    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "keep.txt").write_text("keep", encoding="utf-8")
    for target in (source, source / "nested", source.parent, existing):
        with pytest.raises(ValueError):
            bfcl_suite.prepare_suite(source, target)
    assert (existing / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert not (source / "nested").exists()


def test_quota_failure_publishes_nothing(pool_fixture, tmp_path):
    source, pool = pool_fixture
    shared = copy.deepcopy(next(iter(pool["cases"].values()))["functions"])
    for case in pool["cases"].values():
        case["functions"] = shared
    output = tmp_path / "impossible"
    with pytest.raises(ValueError, match="Insufficient independent"):
        bfcl_suite.prepare_suite(source, output)
    assert not output.exists()
    assert not list(tmp_path.glob("bfcl-prepare-*"))


def test_changed_source_cannot_publish_or_leave_staging_data(pool_fixture, tmp_path, monkeypatch):
    source, _pool = pool_fixture
    original_select = bfcl_suite._select

    def change_source(*args):
        (source / "fixture.jsonl").write_bytes(b"changed during selection")
        return original_select(*args)

    monkeypatch.setattr(bfcl_suite, "_select", change_source)
    output = tmp_path / "changed-source"
    with pytest.raises(ValueError, match="changed while preparing"):
        bfcl_suite.prepare_suite(source, output)
    assert not output.exists()
    assert not list(tmp_path.glob("bfcl-prepare-*"))


def test_license_must_match_pinned_hash(pool_fixture, tmp_path):
    source, _pool = pool_fixture
    (source / "LICENSE-GORILLA.txt").write_bytes(b"wrong license")
    with pytest.raises(ValueError, match="license hash"):
        bfcl_suite.prepare_suite(source, tmp_path / "invalid-license")


def test_seed_is_explicit_integer(pool_fixture, tmp_path):
    source, _pool = pool_fixture
    for seed in (True, "20261029", None):
        with pytest.raises(TypeError, match="seed"):
            bfcl_suite.prepare_suite(source, tmp_path / "invalid-seed", seed=seed)
