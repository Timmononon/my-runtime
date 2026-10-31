"""Pinned BFCL V4 score rules without model clients or executable expressions.

The upstream pure checker bodies are vendored with source hashes. This bridge
restores Aster wire aliases and checks input shape before invoking those rules.
It is not the official leaderboard runner and never creates a provider/Runtime.
The official irrelevance subchecker is explicitly wired here; the pinned full
upstream runner imports it but omits the call. See scoring_rule_metadata().
"""
from __future__ import annotations

import copy
import hashlib
import importlib
import json
import math
from collections import defaultdict
from pathlib import Path

from aster.evaluation._vendor.bfcl_v4.constants.enums import Language
from aster.evaluation._vendor.bfcl_v4.eval_checker.agentic_eval.agentic_checker import agentic_checker
from aster.evaluation._vendor.bfcl_v4.eval_checker.ast_eval.ast_checker import ast_checker
from aster.evaluation._vendor.bfcl_v4.eval_checker.multi_turn_eval.multi_turn_checker import (
    multi_turn_irrelevance_checker,
    response_checker,
    state_checker,
)
from aster.evaluation._vendor.bfcl_v4.vector_context import admitted_vector_encoder, current_vector_encoder

__all__ = ["admitted_vector_encoder", "aggregate_scores", "check_irrelevant_turn", "check_multiturn_completion",
           "check_multiturn_turn", "grade_agentic_answer", "grade_relevance", "grade_single_turn", "memory_class",
           "scoring_rule_metadata", "vendored_simulator_classes", "vendored_simulator_method_names", "verify_vendor_sources"]

PINNED_REVISION = "6ea57973c7a6097fd7c5915698c54c17c5b1b6c8"
SCORING_RULE = "bfcl_v4_pinned_official_checker"
SCORING_ADAPTATIONS = (
    {"id": "explicit_multiturn_irrelevance_wiring",
     "official_subchecker": "multi_turn_irrelevance_checker",
     "upstream_full_runner_calls_subchecker": False,
     "aster_harness_calls_subchecker": True,
     "note": "The pinned full runner imports but does not invoke this official rule. Aster explicitly wires it for empty-gold turns; no legacy bypass is supplied."},
    {"id": "agentic_pinned_source_behavior",
     "note": "The pinned matcher uses punctuation/case normalization and word-bounded containment in the final non-tool message; it does not extract an answer field or require whole-message equality."},
    {"id": "canonical_function_names",
     "note": "Aster restores wire aliases to original names before the unchanged AST checker; the provider registry is replaced by one non-rewriting name entry."},
    {"id": "explicit_vector_encoder_context",
     "note": "VectorStore and MemoryAPI_vector bodies remain unchanged. Eager model construction is replaced by an explicitly admitted offline ContextVar encoder proxy, with optional lazy FAISS and finite 384-dimensional output validation."},
)
_LEAVES = (
    "simple_python", "simple_java", "simple_javascript", "multiple", "parallel", "parallel_multiple",
    "live_simple", "live_multiple", "live_parallel", "live_parallel_multiple",
    "irrelevance", "live_irrelevance", "multi_turn_base", "multi_turn_miss_func",
    "multi_turn_miss_param", "multi_turn_long_context", "web_search_base", "web_search_no_snippet",
    "memory_kv", "memory_vector", "memory_rec_sum",
)
_LIVE_COUNTS = dict(zip(_LEAVES[6:10], (258, 1053, 16, 24), strict=True))
_SIMULATORS = {
    "GorillaFileSystem": "gorilla_file_system", "MathAPI": "math_api", "MessageAPI": "message_api",
    "TwitterAPI": "posting_api", "TicketAPI": "ticket_api", "TradingBot": "trading_bot",
    "TravelAPI": "travel_booking", "VehicleControlAPI": "vehicle_control",
}
_SIMULATOR_DOCS = {
    "GorillaFileSystem": "gorilla_file_system", "MathAPI": "math_api", "MessageAPI": "message_api",
    "TwitterAPI": "posting_api", "TicketAPI": "ticket_api", "TradingBot": "trading_bot",
    "TravelAPI": "travel_booking", "VehicleControlAPI": "vehicle_control",
}
_SIMULATOR_PACKAGE = "aster.evaluation._vendor.bfcl_v4.eval_checker.multi_turn_eval.func_source_code"


def _result(result):
    return {**result, "result": "pass" if result.get("valid") else "fail",
            "scoring_rule": SCORING_RULE, "official_score": False, "source_revision": PINNED_REVISION}


def _failure(kind):
    return _result({"valid": False, "error_type": kind})


def _json_value(value):
    try:
        json.dumps(value, allow_nan=False)
    except (ValueError, TypeError, RecursionError) as error:
        raise ValueError("BFCL score inputs must contain finite JSON values") from error


def scoring_rule_metadata():
    return {"scoring_rule": SCORING_RULE, "source_revision": PINNED_REVISION, "official_score": False,
            "adaptations": copy.deepcopy(SCORING_ADAPTATIONS),
            "scope": "Pinned official pure checkers with explicit Aster harness wiring, not an unchanged full upstream runner."}


def verify_vendor_sources():
    """Check original/adapted package bytes; do not import any simulator."""
    root = Path(__file__).parent / "_vendor/bfcl_v4"
    manifest = json.loads((root / "SOURCE_MANIFEST.json").read_text(encoding="utf-8"))
    if manifest["revision"] != PINNED_REVISION:
        raise ValueError("Unexpected BFCL vendor revision")
    for entry in manifest["files"]:
        for key, digest_key in (("file", "vendored_sha256"), ("source", "source_sha256")):
            relative = Path(entry[key])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Invalid BFCL vendor manifest path")
            if hashlib.sha256((root / relative).read_bytes()).hexdigest() != entry[digest_key]:
                raise ValueError("BFCL vendor source integrity check failed")
    for entry in manifest.get("adapters", []):
        relative = Path(entry["file"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Invalid BFCL vendor adapter path")
        if hashlib.sha256((root / relative).read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError("BFCL vendor adapter integrity check failed")
    for entry in manifest.get("resources", []):
        relative = Path(entry["file"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Invalid BFCL vendor resource path")
        if hashlib.sha256((root / relative).read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError("BFCL vendor resource integrity check failed")
    return {"valid": True, "files": len(manifest["files"]), "source_revision": PINNED_REVISION}


def vendored_simulator_classes(names=None):
    """Load only trusted allowlisted local classes; never web/vector models.

    The caller must separately constrain methods, state, arguments and budgets.
    MathAPI requires the optional mpmath dependency; imports construct no state.
    """
    names = list(_SIMULATORS) if names is None else list(names)
    if len(set(names)) != len(names) or any(name not in _SIMULATORS for name in names):
        raise ValueError("Unsupported BFCL local simulator class")
    return {name: getattr(importlib.import_module(f"{_SIMULATOR_PACKAGE}.{_SIMULATORS[name]}"), name)
            for name in names}


def vendored_simulator_method_names(names=None):
    """Read the pinned bundled method allowlist, never a model/corpus-supplied one."""
    names = list(_SIMULATOR_DOCS) if names is None else list(names)
    if len(set(names)) != len(names) or any(name not in _SIMULATOR_DOCS for name in names):
        raise ValueError("Unsupported BFCL local simulator class")
    root = Path(__file__).parent / "_vendor/bfcl_v4/data/multi_turn_func_doc"
    result = {}
    for name in names:
        rows = [json.loads(line) for line in (root / f"{_SIMULATOR_DOCS[name]}.json").read_text(
            encoding="utf-8").splitlines() if line.strip()]
        result[name] = {row["name"] for row in rows}
    return result


def memory_class(backend):
    """Load official KV/recursive-summary classes, without creating snapshots.

    Snapshot configuration is supplied by the isolated caller, never by a model.
    Vector memory requires an explicitly admitted encoder context and optional
    FAISS. Its adapted module never imports SentenceTransformer or auto-loads
    weights. The original VectorStore and memory method bodies remain intact.
    """
    if backend not in {"kv", "rec_sum", "vector"}:
        raise ValueError("Unknown official memory backend")
    if backend == "vector":
        current_vector_encoder()
        importlib.import_module("faiss")
    module = importlib.import_module(f"{_SIMULATOR_PACKAGE}.memory_{backend}")
    return getattr(module, f"MemoryAPI_{backend}")


def check_multiturn_turn(model_instances, reference_instances, cumulative_results, expected_results, turn_index=0):
    """Official state plus cumulative response checks for a nonempty gold turn.

    The runner owns empty-turn detection, irrelevant turns and forced termination.
    Both instance maps must hold isolated objects of the same simulator types.
    """
    if set(model_instances) != set(reference_instances):
        return _failure("multi_turn:instance_class_mismatch")
    if any(type(model_instances[name]) is not type(reference_instances[name]) for name in reference_instances):
        return _failure("multi_turn:instance_class_mismatch")
    try:
        state = state_checker(model_instances, reference_instances)
    except (AttributeError, AssertionError, TypeError):
        return _failure("multi_turn:invalid_state_shape")
    if not state["valid"]:
        return _result(state)
    return _result(response_checker(cumulative_results, expected_results, turn_index))


def check_irrelevant_turn(decoded_steps, expected_calls):
    """Invoke the unchanged official empty-ground-truth turn rule."""
    if not isinstance(decoded_steps, list) or not isinstance(expected_calls, list):
        return _failure("multi_turn:invalid_decoded_turn")
    return _result(multi_turn_irrelevance_checker([decoded_steps], [expected_calls]))


def check_multiturn_completion(decoded_turns, expected_turns, *, forced_terminated=False):
    """Retain the official entry-level forced/incomplete-turn failure boundary."""
    if not isinstance(decoded_turns, list) or not isinstance(expected_turns, list):
        return _failure("multi_turn:inference_error")
    if forced_terminated or len(decoded_turns) != len(expected_turns):
        return _failure("multi_turn:force_terminated")
    return _result({"valid": True})


def grade_agentic_answer(answer, ground_truth, track):
    """Score the final non-tool message using the pinned official matcher.

    That matcher searches normalized answers within the message with word
    boundaries; it does not extract an answer field or require message equality.
    """
    if not any(name in str(track) for name in ("memory", "web_search")):
        raise ValueError("An agentic BFCL track is required")
    if isinstance(ground_truth, dict):
        ground_truth = ground_truth.get("ground_truth")
    if (not isinstance(ground_truth, list) or not ground_truth
            or any(not isinstance(value, str) or not value for value in ground_truth)):
        raise ValueError("Agentic ground truth must be a nonempty string list")
    if not isinstance(answer, (str, list)) or not answer:
        return _failure("agentic:no_last_message")
    if isinstance(answer, list) and (not answer or any(not isinstance(item, str) for item in answer)):
        return _failure("agentic:invalid_last_message")
    return _result(agentic_checker(copy.deepcopy(answer), copy.deepcopy(ground_truth)))


def _decoded_calls(calls, name_map=None):
    if not isinstance(calls, list) or len(calls) > 256:
        raise ValueError("BFCL decoded calls must be a bounded list")
    decoded = []
    for call in calls:
        if not isinstance(call, dict):
            raise TypeError("BFCL decoded calls must contain objects")
        if set(call) >= {"name", "arguments"}:
            name, arguments = call["name"], call["arguments"]
        elif len(call) == 1:
            name, arguments = next(iter(call.items()))
        else:
            raise ValueError("Invalid BFCL decoded call shape")
        if not isinstance(name, str) or not name or not isinstance(arguments, dict):
            raise ValueError("Invalid BFCL decoded function name or arguments")
        if name_map is not None:
            name = name_map.get(name, name)
        _json_value(arguments)
        decoded.append({name: copy.deepcopy(arguments)})
    return decoded


def grade_relevance(calls, category):
    """Official relevance logic after AST decoding, without executing a call.

    Invalid/empty decoded output means no valid function call. Irrelevance wants
    no call; relevance wants at least one syntactically decoded nonempty call.
    """
    if "relevance" not in category:
        raise ValueError("A relevance or irrelevance track is required")
    try:
        decoded = _decoded_calls(calls)
        contain_call = bool(decoded)
    except (ValueError, TypeError):
        contain_call = False
    success = not contain_call if "irrelevance" in category else contain_call
    if success:
        return _result({"valid": True})
    return _failure("irrelevance_error:decoder_success" if "irrelevance" in category
                    else "relevance_error:decoder_failed")


def grade_single_turn(functions, expected, calls, language="python", category="simple_python", name_map=None):
    """Official AST rule on decoded calls, restoring aliases before comparison.

    Java/JavaScript parameters must be their already-decoded string literals,
    as required by the original converters (e.g. Java ``3.14f`` or JS ``123n``).
    This API does not execute/parse arbitrary language source or silently coerce
    native JSON arguments into a different language's syntax.
    """
    if "relevance" in category:
        return grade_relevance(calls, category)
    if isinstance(expected, dict) and "ground_truth" in expected:
        expected = expected["ground_truth"]
    try:
        language = language if isinstance(language, Language) else Language(str(language).lower())
        decoded = _decoded_calls(calls, name_map)
    except (ValueError, TypeError):
        return _failure("ast:invalid_decoded_prediction")
    if not isinstance(functions, list) or not functions or not isinstance(expected, list):
        raise ValueError("Official AST scoring requires function descriptions and ground truth")
    _json_value(functions)
    _json_value(expected)
    try:
        result = ast_checker(copy.deepcopy(functions), decoded, copy.deepcopy(expected), language, category, "aster")
    except (KeyError, IndexError, TypeError, ValueError, AttributeError, NotImplementedError) as error:
        return {"valid": None, "result": "unsupported", "error_type": "ast:unsupported_input_contract",
                "failure_type": type(error).__name__, "scoring_rule": SCORING_RULE,
                "source_revision": PINNED_REVISION, "official_score": False}
    return _result(result)


def _leaf_name(row):
    category = row.get("category", row.get("track"))
    variant = row.get("variant")
    if category == "web_search":
        return f"web_search_{variant}" if variant in {"base", "no_snippet"} else category
    if category == "memory":
        return f"memory_{variant}" if variant in {"kv", "vector", "rec_sum"} else category
    return category


def _leaf_metrics(track_results):
    if isinstance(track_results, dict):
        rows = [{"category": key, **value} for key, value in track_results.items()]
    elif isinstance(track_results, list):
        rows = track_results
    else:
        raise TypeError("BFCL aggregate input must be a metric mapping or result list")
    groups = defaultdict(list)
    for row in rows:
        if not isinstance(row, dict):
            raise TypeError("BFCL aggregate rows must be objects")
        leaf = _leaf_name(row)
        if leaf in _LEAVES:
            groups[leaf].append(row)
    metrics = {}
    for leaf in _LEAVES:
        leaf_rows = groups.get(leaf, [])
        if not leaf_rows or any(("model_quality" in row and row["model_quality"] != "evaluated") or row.get("status") in {
                "unsupported", "not_run", "incomplete"} or row.get("result") in {"unsupported", "not_run", "case_error"}
                for row in leaf_rows):
            metrics[leaf] = {"accuracy": None, "total_count": len(leaf_rows), "status": "incomplete"}
            continue
        if all("accuracy" in row for row in leaf_rows):
            if len(leaf_rows) != 1:
                raise ValueError("Duplicate preaggregated BFCL leaf metrics")
            row = leaf_rows[0]
            accuracy, count = row["accuracy"], row.get("total_count")
        else:
            values = []
            for row in leaf_rows:
                if "valid" in row:
                    value = row["valid"]
                elif row.get("result") in {"pass", "fail"}:
                    value = row["result"] == "pass"
                else:
                    raise ValueError("Unscored BFCL results must remain unknown")
                if type(value) is not bool:
                    raise ValueError("Only completed boolean score results may be aggregated")
                values.append(value)
            accuracy, count = sum(values) / len(values), len(values)
        if (isinstance(accuracy, bool) or not isinstance(accuracy, (int, float)) or not math.isfinite(accuracy)
                or not 0 <= accuracy <= 1 or type(count) is not int or count < 1):
            raise ValueError("Invalid BFCL leaf accuracy or count")
        metrics[leaf] = {"accuracy": float(accuracy), "total_count": count, "status": "complete"}
    return metrics


def aggregate_scores(track_results, subset=True, *, live_weighting="sample_counts"):
    """Official score hierarchy with explicit unknowns for absent tracks.

    ``sample_counts`` matches the official helper's reported-count weighting.
    ``official_full_counts`` is an explicit full-distribution subset estimate;
    neither mode turns a small subset into the published full leaderboard.
    """
    if live_weighting not in {"sample_counts", "official_full_counts"}:
        raise ValueError("Unknown Live weighting convention")
    leaves = _leaf_metrics(track_results)

    def combine(children, weights=None):
        nodes = [leaves[name] if isinstance(name, str) else name for name in children]
        if any(node["accuracy"] is None for node in nodes):
            accuracy = None
        else:
            weights = [1] * len(nodes) if weights is None else weights
            accuracy = sum(node["accuracy"] * weight for node, weight in zip(nodes, weights, strict=True)) / sum(weights)
        return {"accuracy": accuracy, "total_count": sum(node["total_count"] for node in nodes),
                "status": "complete" if accuracy is not None else "incomplete"}

    simple = combine(_LEAVES[:3])
    non_live = combine([simple, *(_LEAVES[3:6])])
    live_names = _LEAVES[6:10]
    live_weights = ([_LIVE_COUNTS[name] for name in live_names] if live_weighting == "official_full_counts"
                    else [leaves[name]["total_count"] for name in live_names])
    live = combine(live_names, live_weights)
    hallucination = combine(_LEAVES[10:12])
    multi_turn = combine(_LEAVES[12:16])
    web = combine(_LEAVES[16:18])
    memory = combine(_LEAVES[18:21])
    agentic = combine([web, memory])
    overall = combine([non_live, live, hallucination, multi_turn, agentic], [0.1, 0.1, 0.1, 0.3, 0.4])
    return {"schema_version": 1, "source_revision": PINNED_REVISION, "scoring_rule": SCORING_RULE,
            "official_score": False, "subset": bool(subset), "live_weighting": live_weighting,
            "status": ("complete_subset" if subset else "complete") if overall["accuracy"] is not None else "incomplete",
            "overall_accuracy": overall["accuracy"], "overall_percentage": (
                overall["accuracy"] * 100 if overall["accuracy"] is not None else None),
            "categories": {"non_live": non_live, "live": live, "hallucination": hallucination,
                           "multi_turn": multi_turn, "agentic": agentic},
            "subcategories": {"simple": simple, "web_search": web, "memory": memory},
            "leaf_metrics": leaves, "missing_leaf_categories": [name for name in _LEAVES if leaves[name]["accuracy"] is None],
            "diagnostics_affect_overall": False,
            "note": "Fixed-subset score rules only; offline scripted protocol checks are not model capability scores."}
