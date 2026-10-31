"""Offline-only BFCL V4 inspection, checker verification and saved replay."""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path

from aster.evaluation.bfcl_v4_data import TRACKS, inspect_corpus, load_corpus, read_jsonl


def add_parser(actions):
    parser = actions.add_parser("v4", help="Offline pinned official-rule verification; no provider calls")
    commands = parser.add_subparsers(dest="bfcl_v4_action", required=True)
    for action in ("inspect", "verify", "replay"):
        sub = commands.add_parser(action)
        sub.add_argument("--corpus-dir", type=Path, required=True)
        sub.add_argument("--track", choices=TRACKS, action="append", dest="tracks")
        sub.add_argument("--output", type=Path, required=action != "inspect")
        if action == "replay":
            sub.add_argument("--predictions", type=Path, required=True,
                             help="Local JSONL: id plus calls/answer or user-turn call-expression steps")
        if action == "verify":
            sub.add_argument("--skip-coordinator-smoke", action="store_true",
                             help="Run pure checker/reference replay only, without local scripted Runtime checks")
    return parser


def _output_path(output, corpus, *, predictions=None):
    if output is None:
        return None
    target = Path(output).expanduser().resolve()
    corpus = Path(corpus).expanduser().resolve()
    if target.suffix.lower() != ".json" or target.is_relative_to(corpus) or target == corpus:
        raise ValueError("Use a JSON report outside the immutable corpus")
    if target.name.casefold() in {"credentials.yaml", "config.yaml", "runtime.db"}:
        raise ValueError("Reports cannot replace Runtime configuration")
    if any(part.casefold() == ".aster" for part in target.parts):
        raise ValueError("Reports must not enter the user Runtime")
    if predictions is not None and target == Path(predictions).resolve():
        raise ValueError("Reports cannot replace saved predictions")
    if target.exists() or target.with_suffix(".md").exists():
        raise ValueError("Existing evaluation evidence must not be overwritten")
    for parent in target.parents:
        if any((parent / name).is_file() for name in ("runtime.db", "credentials.yaml")):
            raise ValueError("Reports must stay outside Runtime and credential directories")
    return target


def _save(path, report):
    from aster.platform.filesystem import atomic_write

    if path is not None:
        atomic_write(path, json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False).encode())


def _summary(results):
    return dict(Counter("pass" if row.get("valid") is True else "fail" if row.get("valid") is False
                        else "unsupported" for row in results))


def _base_report(mode):
    from aster.evaluation.bfcl_v4_official import scoring_rule_metadata

    return {"schema_version": 1, "mode": mode, "official_score": False, "model_quality": "unsupported",
            "model_api_calls": 0, "paid_calls_authorized": False,
            "scoring_rules": scoring_rule_metadata(), "capability_accuracy": None,
            "note": "Offline checker/protocol verification only. Scripted and oracle replay are not model capability results."}


def _expected_schema_rejection(case, report):
    """Recognize exactly one pinned data conflict without hiding arbitrary fails."""
    from aster.evaluation.bfcl_v4_data import REVISION, SAMPLE_MANIFEST_SHA256

    provenance = case.get("provenance", {})
    if (case.get("id") != "multi_turn_base_173" or provenance.get("revision") != REVISION
            or provenance.get("sample_manifest_sha256") != SAMPLE_MANIFEST_SHA256
            or provenance.get("question_sha256") != "5b99750ae263d28dd997e74c8d37e4d245f37e8adc01d0515c40aef5fd7e3f60"):
        return None
    function = next((item for item in case["functions"] if item["name"] == "close_ticket"), None)
    if (function is None or function.get("parameters", {}).get("properties", {}).get("ticket_id", {}).get("type") != "integer"
            or len(case.get("expected", [])) <= 3 or "close_ticket(ticket_id='ticket_001')" not in case["expected"][3]):
        return None
    messages = report.get("messages", [])
    errors = [message for message in messages if message.get("role") == "tool" and message.get("metadata", {}).get("is_error")]
    if len(errors) != 1:
        return None
    error = errors[0]
    matching = [call for message in messages if message.get("role") == "assistant"
                for call in message.get("tool_calls", []) if call.get("id") == error.get("tool_call_id")]
    if (len(matching) != 1 or matching[0].get("name") != "close_ticket"
            or matching[0].get("arguments") != {"ticket_id": "ticket_001"}
            or not error.get("content", "").startswith("Invalid arguments:")
            or "'ticket_001' is not of type 'integer'" not in error.get("content", "")
            or error.get("metadata", {}).get("error_category") != "validation"
            or error.get("metadata", {}).get("outcome") != "not_executed"
            or report.get("protocol_result") != "fail" or report.get("forced_termination")
            or report.get("budget", {}).get("stop_reason") is not None
            or len(report.get("runs", [])) != len(case["question"])
            or any(run.get("runtime_status") != "completed" for run in report["runs"])):
        return None
    turn_checks = report.get("scoring_verification", {}).get("turns", [])
    if len(turn_checks) < 4 or any(check.get("valid") is not True for check in turn_checks[:3]):
        return None
    return {"id": case["id"], "status": "blocked_dataset_conflict", "turn_index": 3,
            "function": "close_ticket", "parameter": "ticket_id", "schema_type": "integer",
            "ground_truth_value": "ticket_001", "expected_rejection_verified": True,
            "validation_evidence": {"tool_call_id": error["tool_call_id"], "metadata": error.get("metadata", {}),
                                    "content": error.get("content", "")},
            "source_evidence": copy.deepcopy(provenance),
            "policy": "Preserve the original protocol failure and all official data; do not relax schema or count this as a model failure."}


def _call_candidates(case):
    """Make an explicitly labelled reference-candidate fixture for checker tests."""
    if not isinstance(case.get("expected"), list):
        return None
    functions = {function["name"]: function for function in case["functions"]}
    result = []
    for expected in case["expected"]:
        if not isinstance(expected, dict) or len(expected) != 1:
            return None
        name, parameters = next(iter(expected.items()))
        if name not in functions or not isinstance(parameters, dict):
            return None
        required = functions[name]["parameters"].get("required", [])
        arguments = {}
        for key, candidates in parameters.items():
            if not isinstance(candidates, list) or not candidates:
                return None
            if "" in candidates and key not in required:
                continue
            arguments[key] = copy.deepcopy(candidates[0])
        result.append({"name": name, "arguments": arguments})
    return result


def _score_saved(case, row, *, replay_root):
    from aster.evaluation.bfcl_v4_official import grade_agentic_answer, grade_relevance, grade_single_turn

    category = case["category"]
    if category.startswith("multi_turn_"):
        from aster.evaluation.bfcl_v4_runner import replay_multiturn

        forced = row.get("forced_terminated", False)
        if type(forced) is not bool:
            raise ValueError("Saved forced-termination markers must be booleans")
        forced = forced or row.get("runtime_status") in {"budget_exhausted", "cancelled", "failed", "interrupted"}
        return replay_multiturn(case, row.get("turns"), root=replay_root, forced_terminated=forced)
    if category in {"memory", "web_search"}:
        return grade_agentic_answer(row.get("answer", ""), case["expected"], category)
    if "relevance" in category:
        return grade_relevance(row.get("calls", []), category)
    if category == "format_sensitivity":
        return {"valid": None, "result": "unsupported", "error_type": "format_diagnostic:prompt_non_fc_protocol_required"}
    language = "javascript" if category == "simple_javascript" else "java" if category == "simple_java" else "python"
    name_map = {}
    original_names = {function["name"] for function in case["functions"]}
    for name in original_names:
        alias = name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name) else "b_" + hashlib.sha256(name.encode()).hexdigest()[:24]
        if alias in name_map or alias != name and alias in original_names:
            raise ValueError("BFCL wire aliases must not collide with source names")
        name_map[alias] = name
    return grade_single_turn(case["functions"], case["expected"], row.get("calls", []), language=language,
                             category=category, name_map=name_map)


def replay_inputs(corpus_dir, predictions, *, tracks=None):
    """Recompute saved predictions; claimed result/state/quality fields are ignored."""
    from aster.evaluation.bfcl_v4_official import aggregate_scores

    cases = {case["id"]: case for case in load_corpus(corpus_dir, tracks=tracks)}
    rows = read_jsonl(predictions)
    ids = [row.get("id") for row in rows]
    if any(not isinstance(identifier, str) for identifier in ids) or len(set(ids)) != len(ids) or not set(ids) <= cases.keys():
        raise ValueError("Saved prediction IDs must be distinct selected BFCL configuration IDs")
    results = []
    with tempfile.TemporaryDirectory(prefix="aster-bfcl-v4-score-") as temporary:
        for row in rows:
            case = cases[row["id"]]
            score = _score_saved(case, row, replay_root=temporary)
            results.append({"id": case["id"], "category": case["category"], "variant": case["variant"],
                            "model_quality": "unsupported", **score})
    report = _base_report("offline_saved_prediction_replay")
    report.update({"predictions_sha256": hashlib.sha256(Path(predictions).read_bytes()).hexdigest(),
                   "scoring_verification": {"totals": _summary(results), "results": results},
                   "unprovided_case_ids": sorted(set(cases) - set(ids)),
                   "capability_aggregation": aggregate_scores(results, live_weighting="official_full_counts")})
    return report


async def verify_corpus(corpus_dir, *, tracks=None, coordinator_smoke=True, evidence_root=None):
    """Exercise pinned positive/negative checker boundaries and local Runtime."""
    from aster.evaluation.bfcl_v4_official import (
        aggregate_scores,
        grade_agentic_answer,
        grade_relevance,
        grade_single_turn,
        verify_vendor_sources,
    )
    from aster.evaluation.bfcl_v4_runner import reference_script, replay_multiturn, run_case_offline

    cases = load_corpus(corpus_dir, tracks=tracks)
    vendor = verify_vendor_sources()
    rows, smoke, stateful, conflicts = [], [], [], []
    with tempfile.TemporaryDirectory(prefix="aster-bfcl-v4-verify-") as temporary:
        for case in cases:
            category = case["category"]
            row = {"id": case["id"], "category": category, "variant": case["variant"], "model_quality": "unsupported"}
            if category.startswith("multi_turn_"):
                predictions = [[list(turn)] if turn else [] for turn in case["expected"]]
                positive = replay_multiturn(case, predictions, root=temporary)
                negative = replay_multiturn(case, [[] for _ in case["question"]], root=temporary)
                row.update(valid=positive["valid"] is True and negative["valid"] is False,
                           positive=positive, negative=negative, validation_kind="reference_replay_and_empty_prediction")
                stateful.append(case)
            elif category in {"memory", "web_search"}:
                positive = grade_agentic_answer(case["expected"][0], case["expected"], category)
                negative = grade_agentic_answer("OFFLINE_NEGATIVE_SENTINEL_738194", case["expected"], category)
                row.update(valid=positive["valid"] is True and negative["valid"] is False,
                           positive=positive, negative=negative, validation_kind="answer_checker_boundary")
                if category == "memory" and case["variant"] == "vector":
                    row["runtime_status"] = "unsupported_official_runtime_missing_local_encoder_and_faiss"
            elif "relevance" in category:
                calls = [{"name": case["functions"][0]["name"], "arguments": {}}]
                positive_calls, negative_calls = ([], calls) if "irrelevance" in category else (calls, [])
                positive = grade_relevance(positive_calls, category)
                negative = grade_relevance(negative_calls, category)
                row.update(valid=positive["valid"] is True and negative["valid"] is False,
                           positive=positive, negative=negative, validation_kind="relevance_rule_boundary")
            elif category in {"simple_java", "simple_javascript", "format_sensitivity"}:
                row.update(valid=None, result="unsupported", validation_kind="runtime_input_contract_pending",
                           reason="Official Java/JS decoded literals or prompt-only format protocol require explicit predictions")
            else:
                calls = _call_candidates(case)
                if calls is None:
                    row.update(valid=None, result="unsupported", validation_kind="reference_candidate_shape")
                else:
                    positive = grade_single_turn(case["functions"], case["expected"], calls, category=category)
                    negative = grade_single_turn(case["functions"], case["expected"], [], category=category)
                    row.update(valid=(positive["valid"] is True and negative["valid"] is False) if positive["valid"] is not None else None,
                               positive=positive, negative=negative, validation_kind="reference_candidate_boundary")
            rows.append(row)
        agentic = None
        if coordinator_smoke:
            evidence = Path(evidence_root) if evidence_root else Path(temporary) / "runtime-evidence"
            for case in stateful:
                output = evidence / "coordinator" / hashlib.sha256(case["id"].encode()).hexdigest()[:24]
                try:
                    runtime_report = await run_case_offline(case, reference_script(case), output)
                    smoke.append(runtime_report)
                    conflict = _expected_schema_rejection(case, runtime_report)
                    if conflict is not None:
                        conflicts.append(conflict)
                except (ValueError, RuntimeError) as error:
                    smoke.append({"id": case["id"], "protocol_result": "unsupported", "failure_type": type(error).__name__,
                                  "model_quality": "unsupported", "model_api_calls": 0})
            from aster.evaluation.bfcl_v4_agentic import verify_agentic_protocols

            agentic = await verify_agentic_protocols(cases, evidence / "agentic")
    unexpected_protocol_failures = [item["id"] for item in smoke if item["protocol_result"] != "pass"
                                    and item["id"] not in {conflict["id"] for conflict in conflicts}]
    agentic_passed = agentic is None or agentic.get("verification_passed") is not False
    verification_passed = _summary(rows).get("fail", 0) == 0 and not unexpected_protocol_failures and agentic_passed
    report = _base_report("offline_pinned_checker_and_protocol_verification")
    report.update({"corpus": inspect_corpus(corpus_dir, tracks=tracks), "vendor_integrity": vendor,
                   "scoring_verification": {"totals": _summary(rows), "results": rows},
                   "protocol_checks": smoke,
                   "protocol_totals": dict(Counter(row["protocol_result"] for row in smoke)),
                   "agentic_protocol_checks": agentic,
                   "verification_passed": verification_passed,
                   "verification_status": ("complete_with_recorded_data_conflicts" if conflicts else "complete") if verification_passed else "failed",
                   "data_conflict_count": len(conflicts), "recorded_data_schema_conflicts": conflicts,
                   "unexpected_protocol_failure_ids": unexpected_protocol_failures,
                   "capability_aggregation": aggregate_scores(rows, live_weighting="official_full_counts"),
                   "support_notes": {
                       "java_javascript": "Official AST checker accepts language-literal strings; native JSON is not silently converted.",
                       "format_sensitivity": "Prompt/non-FC diagnostic is not implemented by native-FC replay.",
                       "web_search": "Only explicit local observations are allowed; fixture success is not retrieval capability.",
                       "memory_vector": "Answer checker is available; no eager encoder import, automatic model download or fake vector search.",
                   }})
    return report


async def handle_command(args):
    try:
        output = _output_path(args.output, args.corpus_dir, predictions=getattr(args, "predictions", None))
        if args.bfcl_v4_action == "inspect":
            report = inspect_corpus(args.corpus_dir, tracks=args.tracks)
        elif args.bfcl_v4_action == "replay":
            report = replay_inputs(args.corpus_dir, args.predictions, tracks=args.tracks)
        else:
            evidence = output.with_suffix(".evidence") if output else None
            if evidence is not None and evidence.exists():
                raise ValueError("Verification evidence already exists")
            report = await verify_corpus(args.corpus_dir, tracks=args.tracks,
                                        coordinator_smoke=not args.skip_coordinator_smoke, evidence_root=evidence)
        _save(output, report)
        compact = {key: report[key] for key in ("mode", "model_quality", "model_api_calls", "official_score") if key in report}
        for key in ("base_task_count", "configuration_cases", "protocol_totals"):
            if key in report:
                compact[key] = report[key]
        if "scoring_verification" in report:
            compact["checker_verification_totals"] = report["scoring_verification"]["totals"]
        if output:
            compact["output"] = str(output)
        print(json.dumps(compact, ensure_ascii=False, indent=2))
        if args.bfcl_v4_action == "verify":
            return 0 if report["verification_passed"] else 2
        failures = report.get("scoring_verification", {}).get("totals", {}).get("fail", 0)
        agentic_failed = (report.get("agentic_protocol_checks") or {}).get("verification_passed") is False
        return 2 if failures or report.get("protocol_totals", {}).get("fail", 0) or agentic_failed else 0
    except (ValueError, TypeError, OSError, RuntimeError, ImportError) as error:
        print(f"aster bfcl v4: offline operation rejected ({type(error).__name__}); inspect pinned input and explicit local options", file=sys.stderr)
        return 2


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m aster.evaluation.bfcl_v4_cli")
    add_parser(parser.add_subparsers(dest="bfcl_action", required=True))
    return asyncio.run(handle_command(parser.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
