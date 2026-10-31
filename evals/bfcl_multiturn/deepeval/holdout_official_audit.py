"""Independent deterministic simulator/checker replay of saved held-out tasks.

Only the fixed official local simulator is executed in fresh temporary roots.
No provider, learned judge, external business tool, or captured Python is run.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
from collections import Counter
from copy import deepcopy
from pathlib import Path

from aster.domain import to_dict
from aster.evaluation.bfcl_v4_data import public_turn
from aster.evaluation.bfcl_v4_official import (
    check_irrelevant_turn,
    check_multiturn_turn,
    verify_vendor_sources,
)
from aster.evaluation.bfcl_v4_tools import create_environment
from evals.bfcl_multiturn.promptfoo.compact_holdout_dataset import load_holdout_dataset


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _require(condition, code):
    if not condition:
        raise ValueError("Independent held-out audit rejected: " + code)


def _safe_check(check):
    return {key: check[key] for key in ("valid", "error_type", "result", "scoring_rule", "source_revision")
            if key in check}


def verify_case_sources(cases):
    """Re-normalize the fixed source rows; directory provenance is the only relocation."""
    expected, metadata = load_holdout_dataset()
    _require([case["id"] for case in cases] == [case["id"] for case in expected], "case_membership")
    for actual, original in zip(cases, expected, strict=True):
        first, second = deepcopy(actual), deepcopy(original)
        first.get("provenance", {}).pop("corpus_dir", None)
        second.get("provenance", {}).pop("corpus_dir", None)
        _require(_canonical(first) == _canonical(second), "raw_source_normalization")
    return metadata


def audit_campaign(campaign, output):
    """Recheck every entered completed round without changing the original score."""
    root = Path(campaign["root"])
    originals = {path.relative_to(root).as_posix(): _sha(path) for path in root.rglob("*") if path.is_file()}
    cases = json.loads((root / "private-cases.json").read_bytes())
    metadata = verify_case_sources(cases)
    by_id = {case["id"]: case for case in cases}
    vendor = verify_vendor_sources()
    rows, diagnostic_rows = campaign["rows"], []
    completed_checked, replayed_tools, entered_turns, gate_schema_checks = 0, 0, 0, 0
    for row in rows:
        case = by_id[row["id"]]
        message_turns = []
        for message in row.get("messages", []):
            if message.get("role") == "user":
                message_turns.append([])
            _require(bool(message_turns), "message_before_first_user")
            message_turns[-1].append(message)
        runs = row.get("runs", [])
        _require(len(message_turns) == len(runs), "run_message_boundary")
        checks, executed_results, turn_rows = [], [], []
        if runs:
            with tempfile.TemporaryDirectory(prefix="h60a-") as temporary:
                model = create_environment(case, root=Path(temporary) / "m")
                reference = create_environment(case, root=Path(temporary) / "r")
                try:
                    for index, run in enumerate(runs):
                        entered_turns += 1
                        messages = message_turns[index]
                        decoded = [[call["name"] for call in message.get("tool_calls", [])]
                                   for message in messages if message.get("role") == "assistant" and message.get("tool_calls")]
                        actual = [entry for entry in row.get("tool_ledger", []) if entry["run_id"] == run["run_id"]]
                        executed = [entry for entry in actual
                                    if entry.get("result", {}).get("data", {}).get("bfcl_simulator") is True]
                        offered = {item["name"] for item in public_turn(case, index)["functions"]}
                        tools = [{"name": spec.name, "description": spec.description, "parameters": to_dict(spec.parameters)}
                                 for spec in sorted(model.specs, key=lambda item: item.name)
                                 if model.original_names.get(spec.name, spec.name) in offered]
                        for observation in row.get("step_observations", []):
                            if observation.get("turn_index") == index and observation.get("request_purpose") == "capability_check":
                                envelope = json.loads(observation["messages"][0]["content"])
                                _require(envelope.get("policy") == "capability_check_v1", "gate_policy")
                                _require(envelope.get("tools") == tools, "offered_gate_schemas")
                                _require(envelope.get("tools_sha256") == hashlib.sha256(_canonical(tools).encode()).hexdigest(),
                                         "offered_gate_schema_hash")
                                gate_schema_checks += 1
                        for entry in executed:
                            _require(model.original_names.get(entry["tool_name"], entry["tool_name"]) in offered,
                                     "unoffered_business_tool")
                            value = model.execute(entry["tool_name"], entry["arguments"])
                            _require(value == entry["result"]["content"], "recorded_simulator_output")
                            executed_results.append(value)
                            replayed_tools += 1
                        expected = case["expected"][index]
                        reference_results = reference.call_expressions(expected)
                        irrelevant = check_irrelevant_turn(decoded, expected)
                        state = check_multiturn_turn(model.instances, reference.instances, model.results, reference_results, index)
                        judged = irrelevant if not irrelevant["valid"] or not expected else state
                        if expected and not decoded:
                            judged = {"valid": False, "error_type": "multi_turn:empty_turn_model_response"}
                        if run["runtime_status"] == "completed":
                            stored = row["scoring_verification"]["turns"][index]
                            _require(judged["valid"] == stored["valid"]
                                     and judged.get("error_type") == stored.get("error_type"), "official_completed_turn_result")
                            checks.append(judged["valid"])
                            completed_checked += 1
                        rejected = [message for message in messages if message.get("role") == "tool"
                                    and message.get("metadata", {}).get("is_error")]
                        turn_rows.append({"turn_index": index, "runtime_status": run["runtime_status"],
                            "reference_call_count": len(expected), "proposed_call_count": sum(map(len, decoded)),
                            "replayed_tool_count": len(executed), "rejected_proposal_count": len(rejected),
                            "irrelevance": _safe_check(irrelevant), "state_response": _safe_check(state),
                            "official_formal_check_verified": run["runtime_status"] == "completed"})
                finally:
                    model.close()
                    reference.close()
            _require(executed_results == row["simulator_results"], "all_simulator_outputs")
        forced = len(runs) != len(case["question"]) or any(run["runtime_status"] != "completed" for run in runs)
        rejected = any(message.get("role") == "tool" and message.get("metadata", {}).get("is_error")
                       for message in row.get("messages", []))
        passed = bool(checks) and all(checks) and not forced and not rejected and not row.get("budget", {}).get("stop_reason")
        if row["result"] in {"pass", "fail"}:
            _require(passed == (row["result"] == "pass"), "whole_task_result")
        diagnostic_rows.append({"task_id": row["id"], "saved_result": row["result"],
            "entered_turns": len(runs), "completed_turns_checked": sum(item["official_formal_check_verified"] for item in turn_rows),
            "turns": turn_rows, "simulator_outputs_match": True})
    now = {path.relative_to(root).as_posix(): _sha(path) for path in root.rglob("*") if path.is_file()}
    _require(now == originals, "original_campaign_changed")
    result = {"schema_version": 1, "configuration_label": campaign["label"],
        "mode": "independent_local_official_simulator_and_checker_replay",
        "saved_holdout_tasks": len(rows), "entered_turns": entered_turns,
        "completed_official_turns_verified": completed_checked, "simulator_outputs_replayed": replayed_tools,
        "gate_public_schema_checks": gate_schema_checks, "original_campaign_files_unchanged": len(originals),
        "source_revision": metadata["source_revision"], "official_vendor_identity": vendor,
        "recorded_outcomes_preserved": dict(Counter(row["result"] for row in rows)),
        "cases": diagnostic_rows, "original_file_sha256": originals,
        "new_model_api_calls": 0, "new_judge_api_calls": 0, "judge_enabled": False,
        "scores_replaced": False, "new_model_capability_measurement": False}
    output.mkdir()
    with (output / "independent-audit.json").open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    return {key: result[key] for key in (
        "configuration_label", "saved_holdout_tasks", "entered_turns", "completed_official_turns_verified",
        "simulator_outputs_replayed", "gate_public_schema_checks", "original_campaign_files_unchanged",
        "new_model_api_calls", "new_judge_api_calls", "scores_replaced")}
