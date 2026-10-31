"""Read BFCL call-prediction data without executing dataset or model code.

This is a deliberately strict local structured-call grader, not the official
BFCL evaluator. Strings are compared literally; dictionary candidate lists use
the BFCL accepted-value encoding, including within arrays of dictionaries.
Stateful multi-turn evaluation requires the upstream simulator and is rejected.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from pathlib import Path

from jsonschema import Draft202012Validator

from aster.domain import ToolSpec

SUPPORTED_CATEGORIES = frozenset({
    "simple", "simple_python", "multiple", "parallel", "parallel_multiple",
    "live_simple", "live_multiple", "live_parallel", "live_parallel_multiple",
    "irrelevance", "live_irrelevance", "relevance", "live_relevance",
})
GRADING_SCOPE = "local_bfcl_structured_calls"
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,199}\Z")
_SAFE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
_ID = re.compile(r"([a-z][a-z0-9_]*)_([0-9]+)\Z")
_MAX_BYTES = 64 * 1024 * 1024


class BFCLUnsupportedError(ValueError):
    """The requested BFCL track needs a different protocol or evaluator."""


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON object key")
        value[key] = item
    return value


def _constant(_value):
    raise ValueError("Non-finite JSON values are not supported")


def _rows(path):
    path = Path(path)
    if path.stat().st_size > _MAX_BYTES:
        raise ValueError("BFCL input exceeds the 64 MiB file limit")
    payload = path.read_bytes()
    try:
        lines = payload.decode("utf-8-sig").splitlines()
    except UnicodeError as error:
        raise ValueError("BFCL inputs must be UTF-8 JSONL") from error
    rows = {}
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line, object_pairs_hook=_object, parse_constant=_constant)
            json.dumps(row, allow_nan=False)
        except (ValueError, RecursionError) as error:
            raise ValueError(f"Invalid BFCL JSONL record at line {number}") from error
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not _ID.fullmatch(row["id"]):
            raise ValueError(f"Invalid BFCL record ID at line {number}")
        if row["id"] in rows:
            raise ValueError("Duplicate BFCL record ID")
        rows[row["id"]] = row
    if not rows:
        raise ValueError("BFCL inputs must contain at least one record")
    return rows, hashlib.sha256(payload).hexdigest()


def _category(case_id):
    match = _ID.fullmatch(case_id)
    if match is None:
        raise ValueError("Invalid BFCL record ID")
    category = match[1]
    if category not in SUPPORTED_CATEGORIES:
        raise BFCLUnsupportedError(f"Unsupported BFCL category: {category}")
    return category


def _reject_refs(value):
    if isinstance(value, dict):
        if {"$ref", "$dynamicRef", "$recursiveRef"} & value.keys():
            raise BFCLUnsupportedError("References in BFCL function schemas are unsupported")
        for child in value.values():
            _reject_refs(child)
    elif isinstance(value, list):
        for child in value:
            _reject_refs(child)


def _schema(value, *, root=False):
    """Translate Python type labels only at JSON Schema positions."""
    if not isinstance(value, dict):
        raise ValueError("BFCL parameters must contain JSON Schema objects")  # noqa: TRY004 - malformed file data.
    if root:
        _reject_refs(value)
    result = copy.deepcopy(value)
    kind = result.get("type")
    aliases = {"dict": "object", "float": "number", "tuple": "array", "list": "array"}
    if kind == "any":
        result.pop("type")
    elif isinstance(kind, str):
        result["type"] = aliases.get(kind, kind)
    elif kind is not None:
        raise BFCLUnsupportedError("Union or non-string BFCL type annotations are unsupported")
    properties = result.get("properties")
    if properties is not None:
        if not isinstance(properties, dict) or not all(isinstance(key, str) for key in properties):
            raise ValueError("BFCL properties must be a mapping")
        result["properties"] = {key: _schema(item) for key, item in properties.items()}
    if "items" in result:
        if isinstance(result["items"], list):
            result["prefixItems"] = [_schema(item) for item in result.pop("items")]
            result["items"] = False
        else:
            result["items"] = _schema(result["items"])
    for keyword in ("additionalProperties", "contains"):
        if isinstance(result.get(keyword), dict):
            result[keyword] = _schema(result[keyword])
    for keyword in ("anyOf", "oneOf", "allOf"):
        if keyword in result:
            if not isinstance(result[keyword], list):
                raise ValueError("BFCL schema alternatives must be lists")
            result[keyword] = [_schema(item) for item in result[keyword]]
    if root:
        if result.get("type") != "object":
            raise ValueError("BFCL function parameters must be an object")
        properties = result.get("properties", {})
        required = result.get("required", [])
        if (not isinstance(required, list) or not all(isinstance(key, str) and key in properties for key in required)
                or len(set(required)) != len(required)):
            raise ValueError("BFCL required parameters must name declared properties")
        result["additionalProperties"] = False
    try:
        Draft202012Validator.check_schema(result)
    except Exception as error:
        raise BFCLUnsupportedError("BFCL schema cannot be represented by the supported JSON Schema track") from error
    return result


def _functions(row):
    functions = row.get("function")
    if not isinstance(functions, list) or not 1 <= len(functions) <= 256:
        raise ValueError("BFCL function list must contain 1 to 256 functions")
    names, aliases = set(), {}
    for function in functions:
        if not isinstance(function, dict) or not isinstance(function.get("name"), str) or not _NAME.fullmatch(function["name"]):
            raise ValueError("Invalid BFCL function name")
        name = function["name"]
        if name in names:
            raise ValueError("Duplicate BFCL function name")
        names.add(name)
        if not isinstance(function.get("description"), str):
            raise ValueError("BFCL functions require a description")  # noqa: TRY004 - malformed file data.
        _schema(function.get("parameters"), root=True)
        alias = name if _SAFE_NAME.fullmatch(name) else "b_" + hashlib.sha256(name.encode()).hexdigest()[:24]
        if alias in aliases:
            raise ValueError("BFCL wire function alias collision")
        aliases[alias] = name
    if any(alias in names and alias != name for alias, name in aliases.items()):
        raise ValueError("BFCL wire function alias conflicts with an original name")
    return copy.deepcopy(functions), aliases


def _expected(value, functions):
    if not isinstance(value, list) or len(value) > 256:
        raise ValueError("BFCL ground_truth must be a list of at most 256 calls")
    indexed = {function["name"]: function for function in functions}
    for expected in value:
        if not isinstance(expected, dict) or len(expected) != 1:
            raise BFCLUnsupportedError("Only BFCL single-turn argument-candidate ground truth is supported")
        name, arguments = next(iter(expected.items()))
        if name not in indexed or not isinstance(arguments, dict):
            raise ValueError("BFCL ground truth references an undeclared function")
        schema = _schema(indexed[name]["parameters"], root=True)
        if not set(arguments).issubset(schema.get("properties", {})):
            raise ValueError("BFCL ground truth references an undeclared parameter")
        if not set(schema.get("required", [])).issubset(arguments):
            raise ValueError("BFCL ground truth omits a required parameter")
        if any(not isinstance(options, list) or not options for options in arguments.values()):
            raise ValueError("BFCL expected parameters require nonempty accepted-value lists")
    return copy.deepcopy(value)


def load_cases(data_path, answers_path, category=None):
    """Load paired local official-format JSONL files; never fetch or run code.

    The input and answer IDs must match exactly before an optional category
    filter is applied. Returning cases does not certify upstream provenance;
    callers should also pin the source revision and verify the file digests.
    """
    if category is not None and category not in SUPPORTED_CATEGORIES:
        raise BFCLUnsupportedError("Unsupported BFCL category filter")
    rows, data_digest = _rows(data_path)
    answers, answer_digest = _rows(answers_path)
    if set(rows) != set(answers):
        raise ValueError("BFCL task and answer IDs must match exactly")
    version = re.match(r"BFCL_(v[0-9]+)_", Path(data_path).name)
    provenance = {"source_format": "bfcl_jsonl", "source_version": version[1] if version else None,
                  "data_sha256": data_digest, "answers_sha256": answer_digest}
    cases = []
    for case_id, row in rows.items():
        case_category = _category(case_id)
        if category is not None and case_category != category:
            continue
        question = row.get("question")
        if not isinstance(question, list) or len(question) != 1 or not isinstance(question[0], list) or not question[0]:
            raise BFCLUnsupportedError("Only single-user-turn BFCL call prediction is supported")
        messages = []
        for message in question[0]:
            if (not isinstance(message, dict) or message.get("role") != "user"
                    or not isinstance(message.get("content"), str) or not message["content"].strip()):
                raise BFCLUnsupportedError("Only nonempty BFCL user text messages are supported")
            messages.append({"role": "user", "content": message["content"]})
        functions, names = _functions(row)
        if "ground_truth" not in answers[case_id]:
            raise ValueError("BFCL answer record has no ground_truth")
        expected = _expected(answers[case_id]["ground_truth"], functions)
        cases.append({"id": case_id, "category": case_category,
                      "prompt": "\n\n".join(message["content"] for message in messages), "messages": messages,
                      "functions": functions, "tool_names": names, "expected": expected,
                      "provenance": copy.deepcopy(provenance)})
    if not cases:
        raise ValueError("No BFCL cases match the category selection")
    return cases


def public_case(case):
    """Allowlist the task surface; answer data and arbitrary source keys stay out."""
    return {key: copy.deepcopy(case[key]) for key in ("id", "category", "prompt", "messages")}


def tool_specs(case):
    """Return protocol-safe schemas, retaining a reversible original-name map."""
    reverse = {name: alias for alias, name in case["tool_names"].items()}
    return [ToolSpec(reverse[function["name"]], function["description"],
                     _schema(function["parameters"], root=True), risk="read", concurrency_safe=True,
                     replay_safe=True) for function in case["functions"]]


def _equal(actual, expected):
    if isinstance(actual, bool) or isinstance(expected, bool):
        return type(actual) is type(expected) and actual == expected
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)):
        finite = all(not isinstance(item, float) or math.isfinite(item) for item in (actual, expected))
        return finite and actual == expected
    if type(actual) is not type(expected):
        return False
    if isinstance(actual, dict):
        return actual.keys() == expected.keys() and all(_equal(actual[key], expected[key]) for key in actual)
    if isinstance(actual, list):
        return len(actual) == len(expected) and all(_equal(a, b) for a, b in zip(actual, expected, strict=True))
    return actual == expected


def _candidate_equal(actual, expected, schema):
    kind = schema.get("type")
    if kind == "object" and isinstance(actual, dict) and isinstance(expected, dict):
        if set(actual) - set(expected):
            return False
        properties, required = schema.get("properties", {}), set(schema.get("required", []))
        for key, choices in expected.items():
            if not isinstance(choices, list):
                choices = [choices]
            if key not in actual:
                if key in required or "" not in choices:
                    return False
            elif not any(_candidate_equal(actual[key], choice, properties.get(key, {})) for choice in choices):
                return False
        return True
    if kind == "array" and isinstance(actual, list) and isinstance(expected, list):
        if len(actual) != len(expected):
            return False
        prefix, item_schema = schema.get("prefixItems", []), schema.get("items", {})
        for index, (left, right) in enumerate(zip(actual, expected, strict=True)):
            child = prefix[index] if index < len(prefix) else item_schema
            if not _candidate_equal(left, right, child if isinstance(child, dict) else {}):
                return False
        return True
    return _equal(actual, expected)


def _matches(call, expected, schema):
    name, accepted = next(iter(expected.items()))
    if call["name"] != name or set(call["arguments"]) - set(accepted):
        return False
    required = set(schema.get("required", []))
    for parameter, choices in accepted.items():
        if parameter not in call["arguments"]:
            if parameter in required or "" not in choices:
                return False
        elif not any(_candidate_equal(call["arguments"][parameter], choice, schema.get("properties", {}).get(parameter, {}))
                     for choice in choices):
            return False
    return True


def grade_case(case, calls, answer=""):
    """Match the complete call multiset, including duplicate and extra calls.

    Text answers are intentionally not scored in this supported track. Optional
    argument omission is allowed only when the upstream candidate list contains
    the empty-string sentinel and the schema does not require the parameter.
    """
    del answer
    _category(case["id"])
    failures = []
    specs = {case["tool_names"][spec.name]: spec for spec in tool_specs(case)}
    normalized = []
    if not isinstance(calls, list):
        failures.append("calls:not_a_list")
        calls = []
    for index, call in enumerate(calls):
        if (not isinstance(call, dict) or not isinstance(call.get("name"), str)
                or not isinstance(call.get("arguments"), dict) or call.get("turn", 0) != 0):
            failures.append(f"call:{index}:invalid_structure")
            continue
        name = case["tool_names"].get(call["name"], call["name"])
        if name not in specs:
            failures.append(f"call:{index}:unknown_function")
            continue
        try:
            json.dumps(call["arguments"], allow_nan=False)
            valid = Draft202012Validator(specs[name].parameters).is_valid(call["arguments"])
        except (TypeError, ValueError, RecursionError):
            valid = False
        if not valid:
            failures.append(f"call:{index}:invalid_arguments")
            continue
        normalized.append({"name": name, "arguments": copy.deepcopy(call["arguments"])})
    expected = case["expected"]
    if len(calls) != len(expected):
        failures.append("calls:count_mismatch")
    if not failures:
        # Bipartite assignment avoids greedy errors for overlapping accepted
        # values, while consuming each duplicate expected call exactly once.
        edges = []
        for call in normalized:
            schema = specs[call["name"]].parameters
            edges.append([index for index, item in enumerate(expected) if _matches(call, item, schema)])
        assigned = {}

        def assign(source, visited):
            for target in edges[source]:
                if target in visited:
                    continue
                visited.add(target)
                if target not in assigned or assign(assigned[target], visited):
                    assigned[target] = source
                    return True
            return False

        if not all(assign(source, set()) for source in range(len(normalized))):
            failures.append("calls:no_complete_argument_match")
    return {"result": "fail" if failures else "pass", "passed": not failures,
            "failures": failures, "grading_scope": GRADING_SCOPE, "official_score": False}
