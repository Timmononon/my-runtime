import copy
import hashlib
import json

import pytest

from aster.evaluation import bfcl_dataset


@pytest.fixture
def pinned_pool(tmp_path, monkeypatch):
    questions, answers = {}, {}
    for category in bfcl_dataset.CATEGORIES:
        ids = [category + "_0"]
        if category == "parallel_multiple":
            ids += ["parallel_multiple_12", "parallel_multiple_26"]
        questions[category], answers[category] = [], []
        for case_id in ids:
            name, extra = bfcl_dataset._QUARANTINE.get(case_id, ("count", None))
            function = {"name": name, "description": "Count.", "parameters": {
                "type": "dict", "properties": {"number": {"type": "integer"}}, "required": ["number"]}}
            truth = [{name: {"number": [5]}}]
            if extra:
                truth += [{name: {"number": [6], extra: ["", 0]}}]
            questions[category].append({"id": case_id, "question": [[{"role": "user", "content": "Count 5."}]],
                                        "function": [function]})
            answers[category].append({"id": case_id, "ground_truth": truth})
    monkeypatch.setattr(bfcl_dataset, "SOURCE_HASHES", {})
    monkeypatch.setattr(bfcl_dataset, "_EXPECTED_COUNTS", {key: len(value) for key, value in questions.items()})

    def write():
        for category in bfcl_dataset.CATEGORIES:
            for suffix, rows in (("", questions[category]), (".answers", answers[category])):
                name = f"BFCL_v4_{category}{suffix}.json"
                payload = ("\n".join(json.dumps(row) for row in rows) + "\n").encode()
                (tmp_path / name).write_bytes(payload)
                bfcl_dataset.SOURCE_HASHES[name] = hashlib.sha256(payload).hexdigest()
    write()
    return tmp_path, questions, answers, write


def test_quarantine_is_explicit_and_original_bytes_are_unchanged(pinned_pool):
    root, questions, answers, _ = pinned_pool
    before = {path.name: path.read_bytes() for path in root.iterdir()}
    original_questions, original_answers = copy.deepcopy(questions), copy.deepcopy(answers)
    pool = bfcl_dataset.load_pinned_pool(root)
    assert len(pool["questions"]) == len(pool["answers"]) == len(pool["cases"]) == 4
    assert {item["id"] for item in pool["quarantined"]} == set(bfcl_dataset._QUARANTINE)
    assert [item["undeclared_parameters"] for item in pool["quarantined"]] == [["permeability"], ["type"]]
    assert all(item["action"] == "quarantine" for item in pool["quarantined"])
    assert len(pool["sources"]) == 8
    assert pool["revision"] == bfcl_dataset.PINNED_REVISION
    assert questions == original_questions and answers == original_answers
    assert before == {path.name: path.read_bytes() for path in root.iterdir()}
    case = pool["cases"]["simple_python_0"]
    assert case["provenance"]["source_version"] == "v4"
    assert case["expected"] == [{"count": {"number": [5]}}]


def test_changed_source_bytes_rejected_without_rewriting(pinned_pool):
    root, _, _, _ = pinned_pool
    path = root / "BFCL_v4_simple_python.json"
    changed = path.read_bytes() + b"\n"
    path.write_bytes(changed)
    with pytest.raises(ValueError, match="hash mismatch"):
        bfcl_dataset.load_pinned_pool(root)
    assert path.read_bytes() == changed


def test_unknown_schema_gold_conflict_is_not_silently_skipped(pinned_pool):
    root, _, answers, write = pinned_pool
    answers["multiple"][0]["ground_truth"][0]["count"]["unknown"] = [1]
    write()
    with pytest.raises(ValueError, match="undeclared parameter"):
        bfcl_dataset.load_pinned_pool(root)


@pytest.mark.parametrize("change", ["different_parameter", "extra_conflict", "missing_required", "fixed"])
def test_quarantine_requires_exact_known_defect_and_no_other_error(pinned_pool, change):
    root, _, answers, write = pinned_pool
    args = answers["parallel_multiple"][1]["ground_truth"][1]["calculate_voltage_difference"]
    if change == "different_parameter":
        args["other"] = args.pop("permeability")
    elif change == "extra_conflict":
        args["other"] = [3]
    elif change == "missing_required":
        del args["number"]
    else:
        del args["permeability"]
    write()
    with pytest.raises(ValueError, match="documented defect|omits a required"):
        bfcl_dataset.load_pinned_pool(root)


def test_wrong_ids_are_rejected_even_with_individually_valid_files(pinned_pool):
    root, _, answers, write = pinned_pool
    answers["multiple"][0]["id"] = "multiple_1"
    write()
    with pytest.raises(ValueError, match="IDs must match"):
        bfcl_dataset.load_pinned_pool(root)


def test_wrong_category_and_count_are_rejected(pinned_pool):
    root, questions, answers, write = pinned_pool
    questions["multiple"][0]["id"] = answers["multiple"][0]["id"] = "parallel_8"
    write()
    with pytest.raises(ValueError, match="category mismatch"):
        bfcl_dataset.load_pinned_pool(root)
    questions["multiple"][0]["id"] = answers["multiple"][0]["id"] = "multiple_0"
    questions["multiple"].append(copy.deepcopy(questions["multiple"][0]))
    questions["multiple"][1]["id"] = "multiple_1"
    write()
    with pytest.raises(ValueError, match="count mismatch"):
        bfcl_dataset.load_pinned_pool(root)


def test_malformed_question_in_quarantine_is_still_rejected(pinned_pool):
    root, questions, _, write = pinned_pool
    questions["parallel_multiple"][1]["question"][0][0]["role"] = "assistant"
    write()
    with pytest.raises(ValueError, match="user text"):
        bfcl_dataset.load_pinned_pool(root)
