"""Objective answer, call-count, receipt and side-effect checks from Coco's contract."""
import copy
import importlib.resources
import json
import unicodedata


def load_dataset():
    value = json.loads(importlib.resources.files("aster.evaluation").joinpath("scenarios.json").read_text(encoding="utf-8"))
    if len(value["tasks"]) != 24 or len({task["category"] for task in value["tasks"]}) != 7:
        raise ValueError("The pinned Coco evaluation must have 24 scenarios in 7 categories")
    return value


def _normal(value):
    return unicodedata.normalize("NFKC", value).casefold()


def evaluate(task, observation, *, include_answer=True):
    oracle, failures = task["oracle"], []
    if observation["status"] != oracle["expected_status"]:
        failures.append("status:" + observation["status"] + " expected:" + oracle["expected_status"])
    if include_answer:
        rules = oracle["answer"]
        answer = observation["answer"].strip()
        normal = _normal(answer)
        if "exact" in rules and answer != rules["exact"]:
            failures.append("answer:not_exact")
        failures.extend("answer:missing:" + value for value in rules.get("contains_all", []) if _normal(value) not in normal)
        failures.extend("answer:forbidden:" + value for value in rules.get("forbidden", []) if _normal(value) in normal)
        for index, options in enumerate(rules.get("contains_any", [])):
            if not any(_normal(value) in normal for value in options):
                failures.append("answer:missing_alternative:" + str(index))
        if rules.get("max_lines") and len(answer.splitlines()) > rules["max_lines"]:
            failures.append("answer:too_many_lines")
        if rules.get("max_chars") and len(answer) > rules["max_chars"]:
            failures.append("answer:too_many_characters")
    evidence = oracle["evidence"]
    for name, bounds in evidence["calls"].items():
        actual = observation["calls"].get(name, 0)
        if not bounds["min"] <= actual <= bounds["max"]:
            failures.append(f"calls:{name}:{actual}")
    if evidence.get("no_unexpected_calls"):
        failures.extend("unexpected_call:" + name for name, count in observation["calls"].items() if count and name not in evidence["calls"])
    failures.extend("missing_receipt:" + receipt for receipt in evidence["required_receipts"] if receipt not in observation["receipts"])
    expected = copy.deepcopy(task["initial_state"])
    if not oracle["state"]["unchanged"]:
        expected["files"].update(oracle["state"].get("file_changes", {}))
        expected["effects"].update(oracle["state"].get("effect_changes", {}))
    for field in ("files", "effects"):
        if observation["state"][field] != expected[field]:
            failures.append("state:" + field + "_mismatch")
    return failures
