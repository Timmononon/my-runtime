"""Verify a pinned BFCL pool and quarantine two documented schema/gold conflicts.

This module is entirely offline. It never rewrites upstream bytes, changes gold
answers, opens a Runtime, or weakens the structured-call grader.
"""
from __future__ import annotations

import copy
from pathlib import Path

from aster.evaluation.bfcl import BFCLUnsupportedError, _category, _expected, _functions, _rows

PINNED_REVISION = "6ea57973c7a6097fd7c5915698c54c17c5b1b6c8"
CATEGORIES = ("simple_python", "multiple", "parallel", "parallel_multiple")
SOURCE_HASHES = {
    "BFCL_v4_simple_python.json": "82dd63ba502eb2520c6b5d1d9a5c4b590e03ff261565175561f6228a367d1991",
    "BFCL_v4_simple_python.answers.json": "90cd5bc653690ee8e459b5b3f3fc9458606f7f3fcbf795bb51b7dc581f8c86dc",
    "BFCL_v4_multiple.json": "aef168155ebd74b7ac2401198b201343bc7d16d7a3d7e0d4e6d8ee82c6969b2a",
    "BFCL_v4_multiple.answers.json": "244e00ce9395df948bcafc7bee64e8f9c87ef70887587d83cae45b13699f3047",
    "BFCL_v4_parallel.json": "19f51a82eff42e5d62541aa500115a056eb78f437c2ba1f10415fd7c8e5dda84",
    "BFCL_v4_parallel.answers.json": "8a6aa19c1adddc6a5a2f7e40f9dbf30cc7e95815e7b830c90589ab318229e0f0",
    "BFCL_v4_parallel_multiple.json": "8863ea8433239f55c5f016154cf0830853c89f693c6ea270396a2fa121960579",
    "BFCL_v4_parallel_multiple.answers.json": "5ebf24f458c1f16300c05505d83d6f0a1b68b79be273a033febd0d4f840507e3",
}
_EXPECTED_COUNTS = {"simple_python": 400, "multiple": 200, "parallel": 200, "parallel_multiple": 200}
_QUARANTINE = {
    "parallel_multiple_12": ("calculate_voltage_difference", "permeability"),
    "parallel_multiple_26": ("bank.calculate_balance", "type"),
}


def _messages(row):
    question = row.get("question")
    if not isinstance(question, list) or len(question) != 1 or not isinstance(question[0], list) or not question[0]:
        raise BFCLUnsupportedError("Only single-user-turn BFCL call prediction is supported")
    messages = []
    for message in question[0]:
        if (not isinstance(message, dict) or message.get("role") != "user"
                or not isinstance(message.get("content"), str) or not message["content"].strip()):
            raise BFCLUnsupportedError("Only nonempty BFCL user text messages are supported")
        messages.append({"role": "user", "content": message["content"]})
    return messages


def _quarantine_receipt(case_id, truth, functions, provenance):
    """Recognize the exact known defect and reject any additional gold defects.

    The reduced copy is used solely to check the remaining shape, then discarded.
    It is never exported as a corrected answer or passed to a model/scorer.
    """
    indexed = {function["name"]: function for function in functions}
    if not isinstance(truth, list) or len(truth) != 2:
        raise ValueError("Known BFCL quarantine case no longer matches its documented defect")
    conflicts = []
    checked = copy.deepcopy(truth)
    for call in checked:
        if not isinstance(call, dict) or len(call) != 1:
            raise ValueError("Known BFCL quarantine case has an additional malformed call")
        name, arguments = next(iter(call.items()))
        if name not in indexed or not isinstance(arguments, dict):
            raise ValueError("Known BFCL quarantine case has an additional undeclared function")
        declared = indexed[name]["parameters"].get("properties", {})
        for parameter in sorted(set(arguments) - set(declared)):
            conflicts.append((name, parameter))
            del arguments[parameter]
    if conflicts != [_QUARANTINE[case_id]]:
        raise ValueError("Known BFCL quarantine case no longer matches its documented defect")
    _expected(checked, functions)
    name, parameter = conflicts[0]
    return {
        "id": case_id, "category": _category(case_id), "action": "quarantine",
        "reason": "ground_truth_references_undeclared_parameter",
        "function": name, "undeclared_parameters": [parameter],
        "declared_parameters": list(indexed[name]["parameters"].get("properties", {})),
        "data_sha256": provenance["data_sha256"], "answers_sha256": provenance["answers_sha256"],
        "source_revision": PINNED_REVISION,
        "policy": "Excluded from candidate pool; original task and gold bytes preserved; no answer correction.",
    }


def load_pinned_pool(source_dir):
    """Read 1,000 pinned pairs and return 998 valid cases plus two receipts.

    Any changed source hash, mismatched ID/count/category, unknown invalid case,
    or changed known defect rejects the entire preparation. Quarantine is an
    explicit data-quality exclusion, never a model failure or a silent skip.
    """
    root = Path(source_dir)
    pool = {"questions": {}, "answers": {}, "cases": {}, "sources": [],
            "quarantined": [], "revision": PINNED_REVISION}
    for category in CATEGORIES:
        data_name = f"BFCL_v4_{category}.json"
        answer_name = f"BFCL_v4_{category}.answers.json"
        rows, data_digest = _rows(root / data_name)
        answers, answer_digest = _rows(root / answer_name)
        for name, digest, records in ((data_name, data_digest, rows), (answer_name, answer_digest, answers)):
            if digest != SOURCE_HASHES[name]:
                raise ValueError(f"Pinned BFCL source hash mismatch: {name}")
            if len(records) != _EXPECTED_COUNTS[category]:
                raise ValueError(f"Pinned BFCL record count mismatch: {name}")
            if any(_category(case_id) != category for case_id in records):
                raise ValueError(f"Pinned BFCL category mismatch: {name}")
            pool["sources"].append({"file": name, "sha256": digest, "records": len(records)})
        if set(rows) != set(answers):
            raise ValueError("Pinned BFCL task and answer IDs must match exactly")
        provenance = {"source_format": "bfcl_jsonl", "source_version": "v4",
                      "data_sha256": data_digest, "answers_sha256": answer_digest,
                      "source_revision": PINNED_REVISION}
        for case_id, row in rows.items():
            messages = _messages(row)
            functions, names = _functions(row)
            if "ground_truth" not in answers[case_id]:
                raise ValueError("BFCL answer record has no ground_truth")
            truth = answers[case_id]["ground_truth"]
            if case_id in _QUARANTINE:
                pool["quarantined"].append(_quarantine_receipt(case_id, truth, functions, provenance))
                continue
            expected = _expected(truth, functions)
            pool["questions"][case_id] = row
            pool["answers"][case_id] = answers[case_id]
            pool["cases"][case_id] = {
                "id": case_id, "category": category,
                "prompt": "\n\n".join(message["content"] for message in messages), "messages": messages,
                "functions": functions, "tool_names": names, "expected": expected,
                "provenance": copy.deepcopy(provenance),
            }
    if {item["id"] for item in pool["quarantined"]} != set(_QUARANTINE):
        raise ValueError("Pinned BFCL pool is missing a documented quarantine case")
    return pool
