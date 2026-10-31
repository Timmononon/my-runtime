"""Read the frozen BFCL V4 coverage archive without executing source or models."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from collections import Counter
from pathlib import Path

REVISION = "6ea57973c7a6097fd7c5915698c54c17c5b1b6c8"
SAMPLE_MANIFEST_SHA256 = "f0c5f6e2d07826c0db8a7029b32742a2ba4e12e6a792a2aace7ad94089917614"
PROVENANCE_SHA256 = "654544beaafcb071f46da5101b5e0c539295506b39a5e227c0ba00813cdded48"
DATA_PREFIX = "raw/berkeley-function-call-leaderboard/bfcl_eval/data/"
FUNCTION_DOCUMENTS = {
    "GorillaFileSystem": "gorilla_file_system.json", "MathAPI": "math_api.json",
    "MessageAPI": "message_api.json", "TwitterAPI": "posting_api.json", "TicketAPI": "ticket_api.json",
    "TradingBot": "trading_bot.json", "TravelAPI": "travel_booking.json", "VehicleControlAPI": "vehicle_control.json",
    "WebSearchAPI": "web_search.json", "MemoryAPI_kv": "memory_kv.json",
    "MemoryAPI_vector": "memory_vector.json", "MemoryAPI_rec_sum": "memory_rec_sum.json",
}
TRACKS = (
    "simple_python", "simple_java", "simple_javascript", "multiple", "parallel", "parallel_multiple",
    "live_simple", "live_multiple", "live_parallel", "live_parallel_multiple",
    "multi_turn_base", "multi_turn_miss_func", "multi_turn_miss_param", "multi_turn_long_context",
    "irrelevance", "live_irrelevance", "web_search", "memory", "live_relevance", "format_sensitivity",
)
VARIANTS = {"web_search": ("base", "no_snippet"), "memory": ("kv", "vector", "rec_sum")}
ADDITIONAL_FUNCTION_PROMPT = "I have updated some more functions you can choose from. What about now?"
AGENTIC_RESPONSE_PROMPT = (
    "For your final answer to the user, you must respond in this format: "
    "{'answer': A short and precise answer to the question, 'context': A brief explanation of how you arrived at "
    "this answer or why it is correct}. If you do not know the answer, respond with {'answer': 'I do not know', "
    "'context': 'I do not know'}. If you think the question cannot be properly answered, response with "
    "{'answer': 'I cannot answer this question', 'context': A short reason explaining why this question cannot "
    "be answered}.\n"
)
_ID = re.compile(r"[A-Za-z][A-Za-z0-9_:-]*(?:-[A-Za-z0-9]+)*\Z")
_MAX_BYTES = 32 * 1024 * 1024


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON key")
        value[key] = item
    return value


def _number(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Non-finite JSON number")
    return number


def _constant(_value):
    raise ValueError("Non-finite JSON constant")


def strict_json(payload):
    return json.loads(payload, object_pairs_hook=_object, parse_float=_number, parse_constant=_constant)


def read_jsonl(path):
    payload = Path(path).read_bytes()
    if len(payload) > _MAX_BYTES:
        raise ValueError("BFCL input exceeds the file size limit")
    try:
        rows = [strict_json(line) for line in payload.decode("utf-8").splitlines() if line.strip()]
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("BFCL file must be strict UTF-8 JSONL") from error
    if not rows or any(not isinstance(row, dict) for row in rows):
        raise ValueError("BFCL JSONL must contain object records")
    return rows


def _digest(payload):
    return hashlib.sha256(payload).hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def _local(root, relative):
    relative = Path(relative)
    if relative.is_absolute() or relative.drive or ".." in relative.parts:
        raise ValueError("BFCL archive paths must be relative")
    target = root / relative
    if not target.resolve().is_relative_to(root.resolve()) or any(part.is_symlink() for part in (target, *target.parents)):
        raise ValueError("BFCL archive paths must stay inside the corpus")
    return target


def _indexed(rows):
    result = {}
    for row in rows:
        identifier = row.get("id")
        if not isinstance(identifier, str) or not _ID.fullmatch(identifier) or identifier in result:
            raise ValueError("Invalid or duplicate BFCL record ID")
        result[identifier] = row
    return result


def _verified_archive(corpus_dir):
    root = Path(corpus_dir).expanduser().resolve()
    manifest_payload = _local(root, "sample-manifest.json").read_bytes()
    provenance_payload = _local(root, "provenance.json").read_bytes()
    # Pin metadata before trusting the paths and per-file hashes it supplies;
    # checking only listed files would let a changed manifest redefine them.
    if _digest(manifest_payload) != SAMPLE_MANIFEST_SHA256 or _digest(provenance_payload) != PROVENANCE_SHA256:
        raise ValueError("The BFCL V4 archive metadata differs from the pinned manifest")
    manifest, provenance = strict_json(manifest_payload), strict_json(provenance_payload)
    if manifest.get("revision") != REVISION or provenance.get("revision") != REVISION:
        raise ValueError("The BFCL V4 corpus revision is not supported")
    sources = {}
    for item in provenance["files"]:
        name = item["local_file"]
        payload = _local(root, name).read_bytes()
        if len(payload) != item["bytes"] or _digest(payload) != item["sha256"]:
            raise ValueError("Pinned BFCL source bytes changed")
        if item.get("revision") != REVISION:
            raise ValueError("Mixed BFCL source revisions")
        sources[name] = item
    return root, manifest, sources


def _question_turns(row):
    question = row.get("question")
    if not isinstance(question, list) or not question:
        raise ValueError("A BFCL task needs a nonempty list of turns")
    for turn in question:
        if not isinstance(turn, list):
            raise ValueError("BFCL turns must be message lists")  # noqa: TRY004 - malformed file data.
        for message in turn:
            if (not isinstance(message, dict) or message.get("role") not in {"user", "system"}
                    or not isinstance(message.get("content"), str) or not message["content"].strip()):
                raise ValueError("Only complete user/system text messages are accepted from the pinned corpus")
    return [[{"role": message["role"], "content": message["content"]} for message in turn] for turn in question]


def normalize_case(row, answer, *, category, variant=None, documents=None, prerequisites=None, provenance=None):
    """Copy an official record; reference state and answers remain evaluator-only."""
    if category not in TRACKS and category != "memory_prereq":
        raise ValueError("Unknown BFCL V4 track")
    base_id = row.get("id")
    if not isinstance(base_id, str) or not _ID.fullmatch(base_id):
        raise ValueError("Invalid BFCL V4 task ID")
    if category not in {"format_sensitivity", "memory_prereq"} and not base_id.startswith(category + "_"):
        raise ValueError("BFCL task ID does not match its track")
    case = {"id": base_id, "base_id": base_id, "category": category, "variant": variant,
            "question": _question_turns(row), "functions": copy.deepcopy(row.get("function", [])),
            "initial_config": copy.deepcopy(row.get("initial_config", {})),
            "involved_classes": copy.deepcopy(row.get("involved_classes", [])),
            "missed_function": copy.deepcopy(row.get("missed_function", {})),
            "excluded_function": copy.deepcopy(row.get("excluded_function", [])),
            "expected": copy.deepcopy(answer.get("ground_truth") if answer is not None else None),
            "provenance": copy.deepcopy(provenance or {}), "diagnostic": category in {"live_relevance", "format_sensitivity"},
            "is_prerequisite": category == "memory_prereq"}
    if category == "web_search":
        if variant not in VARIANTS[category]:
            raise ValueError("Unknown BFCL web-search variant")
        case["id"] = base_id.replace("web_search", "web_search_" + variant, 1)
        case["initial_config"] = {"WebSearchAPI": {"show_snippet": variant == "base"}}
    if category in {"memory", "memory_prereq"}:
        if variant not in VARIANTS["memory"] or row.get("scenario") not in {"customer", "finance", "healthcare", "notetaker", "student"}:
            raise ValueError("Unknown BFCL memory backend or scenario")
        case["scenario"] = row["scenario"]
        case["id"] = base_id.replace("memory", "memory_" + variant, 1)
        case["involved_classes"] = ["MemoryAPI_" + variant]
        case["depends_on"] = [entry["id"] for entry in prerequisites or []]
        case["memory_prerequisites"] = copy.deepcopy(prerequisites or []) if category == "memory" else []
    if not case["functions"] and case["involved_classes"]:
        if documents is None:
            raise ValueError("BFCL stateful tasks need trusted function documents")
        for class_name in case["involved_classes"]:
            if class_name not in documents:
                raise ValueError("An involved BFCL class has no trusted function document")
            case["functions"].extend(copy.deepcopy(documents[class_name]))
    if not isinstance(case["functions"], list) or not case["functions"]:
        raise ValueError("BFCL tasks need offered function documents")
    if any(not isinstance(function, dict) or not isinstance(function.get("name"), str)
           or not isinstance(function.get("description"), str)
           or not isinstance(function.get("parameters"), dict) for function in case["functions"]):
        raise ValueError("Invalid BFCL function document")
    if not isinstance(case["initial_config"], dict) or not isinstance(case["involved_classes"], list):
        raise ValueError("Invalid BFCL environment configuration")  # noqa: TRY004 - malformed file data.
    names = {function["name"] for function in case["functions"]}
    for turn_index, missing in case["missed_function"].items():
        if not turn_index.isdigit() or int(turn_index) >= len(case["question"]) or case["question"][int(turn_index)]:
            raise ValueError("A missing-function reveal must refer to its empty user turn")
        if not isinstance(missing, list) or not set(missing) <= names:
            raise ValueError("Unknown missing function")
    return case


def public_turn(case, turn_index):
    """Return only the user messages and schemas visible at this user turn."""
    if type(turn_index) is not int or not 0 <= turn_index < len(case["question"]):
        raise ValueError("Invalid BFCL user-turn index")
    hidden = {name for index, names in case.get("missed_function", {}).items() if int(index) > turn_index for name in names}
    original = case["question"][turn_index]
    messages = [copy.deepcopy(message) for message in original if message["role"] == "user"]
    system = [message["content"] for message in original if message["role"] == "system"]
    if str(turn_index) in case.get("missed_function", {}):
        messages = [{"role": "user", "content": ADDITIONAL_FUNCTION_PROMPT}]
    if case["category"] in {"web_search", "memory"}:
        system.insert(0, AGENTIC_RESPONSE_PROMPT)
    return {"id": case["id"], "category": case["category"], "variant": case.get("variant"),
            "messages": messages, "functions": [{key: copy.deepcopy(item[key]) for key in ("name", "description", "parameters")}
                for item in case["functions"] if item["name"] not in hidden],
            "system": "\n\n".join(system)}


def load_corpus(corpus_dir, *, tracks=None, expand_variants=True):
    """Load every selected task and complete memory dependency chain offline."""
    root, manifest, sources = _verified_archive(corpus_dir)
    selected_tracks = set(TRACKS if tracks is None else tracks)
    if not selected_tracks or not selected_tracks <= set(TRACKS):
        raise ValueError("Unknown or empty BFCL track selection")
    documents = {name: read_jsonl(_local(root, DATA_PREFIX + "multi_turn_func_doc/" + filename))
                 for name, filename in FUNCTION_DOCUMENTS.items()}
    result = []
    for sample in manifest["samples"]:
        category = sample["track"]
        if category not in selected_tracks:
            continue
        files, file_hashes = {}, {}
        for item in sample["files"]:
            payload = _local(root, item["file"]).read_bytes()
            if _digest(payload) != item["sha256"]:
                raise ValueError("Frozen BFCL sample bytes changed")
            kind = "answers" if "/answers/" in item["file"] else "questions"
            files[kind] = _indexed(read_jsonl(_local(root, item["file"])))
            file_hashes[kind] = item["sha256"]
        questions, answers = files["questions"], files.get("answers")
        if answers is not None and set(questions) != set(answers):
            raise ValueError("BFCL question and answer IDs must match exactly")
        if set(questions) != set(sample["question_ids"]):
            raise ValueError("BFCL sample IDs differ from the frozen manifest")
        variants = VARIANTS.get(category, (None,)) if expand_variants else (VARIANTS.get(category, (None,))[0],)
        for variant in variants:
            for row in questions.values():
                if _digest(_canonical(row)) != sample["row_sha256"][row["id"]]:
                    raise ValueError("A BFCL task differs from its frozen original record")
                prerequisites = []
                if category == "memory":
                    name = DATA_PREFIX + "memory_prereq_conversation/memory_" + row["scenario"] + ".json"
                    if name not in sources:
                        raise ValueError("Memory prerequisite source is missing from provenance")
                    for entry in read_jsonl(_local(root, name)):
                        prereq = normalize_case(entry, None, category="memory_prereq", variant=variant,
                                                documents=documents, prerequisites=prerequisites,
                                                provenance={"revision": REVISION, "source_file": name})
                        prerequisites.append(prereq)
                result.append(normalize_case(row, answers[row["id"]] if answers is not None else None,
                    category=category, variant=variant, documents=documents, prerequisites=prerequisites,
                    provenance={"revision": REVISION, "sample_manifest_sha256": SAMPLE_MANIFEST_SHA256,
                                "question_sha256": sample["row_sha256"][row["id"]], "corpus_dir": str(root),
                                "sample_file_sha256": copy.deepcopy(file_hashes),
                                "function_document_sha256": {name: sources[DATA_PREFIX + "multi_turn_func_doc/" + FUNCTION_DOCUMENTS[name]]["sha256"]
                                    for name in (["MemoryAPI_" + variant] if category == "memory" else row.get("involved_classes", []))
                                    if name in FUNCTION_DOCUMENTS}}))
    if not result:
        raise ValueError("No BFCL tasks match the selected tracks")
    return result


def inspect_corpus(corpus_dir, *, tracks=None):
    cases = load_corpus(corpus_dir, tracks=tracks)
    support = {}
    for category in {case["category"] for case in cases}:
        if category.startswith("multi_turn_"):
            support[category] = "pinned_local_simulators_and_coordinator_offline_script"
        elif category == "web_search":
            support[category] = "explicit_offline_observation_fixtures_and_official_answer_checker"
        elif category == "memory":
            support[category] = "official_kv_rec_sum_with_complete_snapshot_dependencies; vector_requires_explicit_local_encoder_and_faiss"
        elif category in {"simple_java", "simple_javascript"}:
            support[category] = "official_decoded_language_literal_replay; native_json_literal_projection_pending"
        elif category == "format_sensitivity":
            support[category] = "prompt_non_fc_protocol_not_implemented"
        elif "relevance" in category:
            support[category] = "official_relevance_rule_on_decoded_calls"
        else:
            support[category] = "official_ast_checker_on_saved_decoded_calls"
    return {"schema_version": 1, "revision": REVISION, "mode": "offline_inspection", "official_score": False,
            "model_quality": "unsupported", "model_api_calls": 0, "paid_calls_authorized": False,
            "base_task_count": len({case["base_id"] for case in cases}), "configuration_cases": len(cases),
            "tracks": dict(Counter(case["category"] for case in cases)),
            "variants": dict(Counter(case["category"] + (":" + case["variant"] if case["variant"] else "") for case in cases)),
            "track_support": support, "full_score_status": "incomplete_not_run", "case_ids": [case["id"] for case in cases]}
