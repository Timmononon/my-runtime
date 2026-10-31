"""Offline Agentic environments with explicit fixture and snapshot boundaries.

Search fixtures validate the execution protocol, not real retrieval quality.
KV and recursive-summary methods come from the pinned official implementation.
No search provider, embedding worker, model, or ambient credential is loaded.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from contextlib import nullcontext
from pathlib import Path

from aster.domain import ToolSpec
from aster.evaluation.bfcl import _schema
from aster.evaluation.bfcl_v4_data import strict_json
from aster.platform.filesystem import FileLock, atomic_write


class BFCLAgenticUnsupportedError(RuntimeError):
    """A genuine dependency or unsupplied fixture cannot be substituted."""


_SAFE_ID = re.compile(r"memory_(kv|rec_sum|vector)_(?:prereq_)?[0-9]+-(customer|finance|healthcare|notetaker|student)-[0-9]+\Z")
_SCENARIOS = {
    "student": "You are an academic-support assistant for college student. Remember key personal and academic details discussed across sessions, and draw on them to answer questions or give guidance.",
    "customer": "You are a general customer support assistant for an e-commerce platform. Your task is to understand and remember information that can be used to provide information about user inquiries, preferences, and offer consistent, helpful assistance over multiple interactions.",
    "finance": "You are a high-level executive assistant supporting a senior finance professional. Retain and synthesize both personal and professional information including facts, goals, prior decisions, and family life across sessions to provide strategic, context-rich guidance and continuity.",
    "healthcare": "You are a healthcare assistant supporting a patient across appointments. Retain essential medical history, treatment plans, and personal preferences to offer coherent, context-aware guidance and reminders.",
    "notetaker": "You are a personal organization assistant. Capture key information from conversations, like tasks, deadlines, and preferences, and use it to give reliable reminders and answers in future sessions.",
}
_CORE_TEMPLATE = """{scenario_setting}

You have access to an advanced memory system, consisting of two memory types 'Core Memory' and 'Archival Memory'. Both type of memory is persistent across multiple conversations with the user, and can be accessed in a later interactions. You should actively manage your memory data to keep track of important information, ensure that it is up-to-date and easy to retrieve to provide personalized responses to the user later.

The Core memory is limited in size, but always visible to you in context. The Archival Memory has a much larger capacity, but will be held outside of your immediate context due to its size.

Here is the content of your Core Memory from previous interactions:
{memory_content}
"""
_UNIFIED_TEMPLATE = """{scenario_setting}

You have access to an advanced memory system, which is persistent across multiple conversations with the user, and can be accessed in a later interactions. You should actively manage your memory data to keep track of important information, ensure that it is up-to-date and easy to retrieve to provide personalized responses to the user later.

Here is the content of your memory system from previous interactions:
{memory_content}
"""


def serialize_result(value):
    """Match the fixed official tool result representation."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return json.dumps(value)
    return str(value)


def _specs(functions):
    result, names = [], {}
    for function in functions:
        name = function["name"]
        # Aster keeps its own built-in memory tools registered. Namespace all
        # Agentic wire names rather than replacing or shadowing those tools.
        candidate = "bfcl_" + name
        wire = candidate if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", candidate) else "b_" + hashlib.sha256(name.encode()).hexdigest()[:24]
        if wire in names:
            raise ValueError("Duplicate Agentic function name")
        names[wire] = name
        result.append(ToolSpec(wire, function["description"], _schema(function["parameters"], root=True),
                               risk="write", concurrency_safe=False, replay_safe=False))
    return result, names


def _root(path):
    root = Path(path).expanduser().resolve()
    if root == Path.home().resolve() or any(part.casefold() == ".aster" for part in root.parts):
        raise ValueError("Agentic snapshots require an isolated explicit root")
    if any((parent / name).exists() for parent in (root, *root.parents)
           for name in ("runtime.db", "credentials.yaml")) or (root / "config.yaml").exists():
        raise ValueError("Agentic snapshots must not reuse Runtime or credential state")
    return root


class WebFixtureEnvironment:
    """Execute only explicitly supplied search and processed-page observations."""

    protocol_only = True
    official_runtime = False

    def __init__(self, case, root, *, fixtures):
        if case["category"] != "web_search" or case.get("variant") not in {"base", "no_snippet"}:
            raise ValueError("Invalid BFCL search case")
        if not isinstance(fixtures, dict) or set(fixtures) - {"search", "pages"}:
            raise ValueError("Web verification requires explicit local search/page fixtures")
        json.dumps(fixtures, allow_nan=False)
        self.root, self.case, self.fixtures = _root(root), copy.deepcopy(case), copy.deepcopy(fixtures)
        self.specs, self.original_names = _specs(case["functions"])
        if set(self.original_names.values()) != {"search_engine_query", "fetch_url_content"}:
            raise ValueError("Web fixture tools must match the pinned two-tool protocol")
        self.instances, self.results, self.requests = {}, [], []

    def execute(self, name, arguments):
        name = self.original_names.get(name, name)
        if name not in self.original_names.values() or not isinstance(arguments, dict):
            raise ValueError("Unknown or malformed offline search call")
        arguments = copy.deepcopy(arguments)
        json.dumps(arguments, allow_nan=False)
        self.requests.append({"name": name, "arguments": copy.deepcopy(arguments)})
        if name == "search_engine_query":
            unknown = set(arguments) - {"keywords", "max_results", "region"}
            if unknown or not isinstance(arguments.get("keywords"), str):
                raise ValueError("Malformed offline search query")
            query = arguments["keywords"]
            limit, region = arguments.get("max_results", 10), arguments.get("region", "wt-wt")
            if type(limit) is not int or limit < 1:
                raise ValueError("Search result limit must be positive")
            fixtures = self.fixtures.get("search", {})
            key = json.dumps([query, region], ensure_ascii=False)
            response = copy.deepcopy(fixtures.get(key, fixtures.get(query)))
            if response is None:
                raise BFCLAgenticUnsupportedError("No offline observation exists for this search query")
            if isinstance(response, list):
                keys = ("title", "href", "body") if self.case["variant"] == "base" else ("title", "href")
                response = [{key: row[key] for key in keys if key in row} for row in response[:limit]]
            elif not isinstance(response, dict) or "error" not in response:
                raise ValueError("Search observations must be rows or an explicit error")
        else:
            if set(arguments) - {"url", "mode"} or not isinstance(arguments.get("url"), str):
                raise ValueError("Malformed offline page request")
            url, mode = arguments["url"], arguments.get("mode", "raw")
            if not url.startswith(("http://", "https://")) or mode not in {"raw", "markdown", "truncate"}:
                raise ValueError("Invalid offline page URL or processing mode")
            page = self.fixtures.get("pages", {}).get(url)
            if not isinstance(page, dict) or mode not in page:
                raise BFCLAgenticUnsupportedError("No offline processed-page observation exists for this URL/mode")
            response = copy.deepcopy(page[mode])
        result = serialize_result(response)
        self.results.append(result)
        return result

    def call_expressions(self, _expressions):
        raise BFCLAgenticUnsupportedError("Search has answer scoring and explicit observations, not a reference action script")

    def context_for_turn(self):
        return ""

    def close(self):
        pass


class MemoryEnvironment:
    """Use official memory methods under a validated, per-campaign snapshot root."""

    protocol_only = False
    official_runtime = True

    def __init__(self, case, root, *, encoder=None):
        if case["category"] not in {"memory", "memory_prereq"}:
            raise ValueError("Invalid BFCL memory case")
        match = _SAFE_ID.fullmatch(case["id"])
        backend, scenario = case.get("variant"), case.get("scenario")
        if match is None or (backend, scenario) != match.groups():
            raise ValueError("Memory snapshot identity is not a safe official ID")
        if backend == "vector" and encoder is None:
            raise BFCLAgenticUnsupportedError("Official vector runtime requires an explicitly supplied local encoder and FAISS; no automatic model loading")
        if scenario not in _SCENARIOS:
            raise ValueError("Unknown memory scenario")
        from aster.evaluation.bfcl_v4_official import memory_class

        self.root, self.case, self.encoder = _root(root), copy.deepcopy(case), encoder
        self.protocol_only = backend == "vector" and getattr(encoder, "protocol_only", False)
        self.official_runtime = backend != "vector" or getattr(encoder, "verified_official_weights", False) is True
        self.specs, self.original_names = _specs(case["functions"])
        self.results = []
        self.root.mkdir(parents=True, exist_ok=True)
        self._receipt_path = self.root / "dependency-receipts.json"
        self._lock_path = self.root / "dependency-receipts.lock"
        dependencies = case.get("depends_on", [])
        if (not isinstance(dependencies, list) or len(set(dependencies)) != len(dependencies)
                or any(not isinstance(entry, str) or not _SAFE_ID.fullmatch(entry) for entry in dependencies)):
            raise ValueError("Invalid memory dependency IDs")
        self._dependency_key = backend + ":" + scenario
        with FileLock(self._lock_path):
            completed = self._receipts().get(self._dependency_key, [])
            if completed != dependencies:
                raise ValueError("Memory prerequisite chain is incomplete or differs from the declared order")
            if case["category"] == "memory" and not dependencies:
                raise ValueError("Memory assessment requires the complete prerequisite chain")
            self._latest = self.root / "agentic" / "memory" / backend / "memory_snapshot" / (scenario + "_final.json")
            if dependencies and not self._latest.is_file():
                raise ValueError("A completed memory chain has no final snapshot")
            if not dependencies and self._latest.exists():
                raise ValueError("First prerequisite must not reuse an existing memory snapshot")
            if dependencies:
                strict_json(self._latest.read_bytes())
            with self._memory_context():
                try:
                    memory = memory_class(backend)()
                except ImportError as error:
                    raise BFCLAgenticUnsupportedError("The explicit optional memory backend dependency is unavailable") from error
                memory._load_scenario({"model_result_dir": self.root, "test_id": case["id"], "scenario": scenario})
        self.instances = {"MemoryAPI_" + backend: memory}
        self.memory, self._finished = memory, False
        allowed = set(self.original_names.values())
        if any(name.startswith("_") or not callable(getattr(memory, name, None)) for name in allowed):
            raise ValueError("Memory tool documentation refers to an unavailable public method")

    def _memory_context(self):
        if self.case["variant"] != "vector":
            return nullcontext()
        from aster.evaluation.bfcl_v4_official import admitted_vector_encoder

        return admitted_vector_encoder(self.encoder)

    def _receipts(self):
        if not self._receipt_path.exists():
            return {}
        value = strict_json(self._receipt_path.read_bytes())
        if not isinstance(value, dict):
            raise ValueError("Invalid memory dependency receipt")  # noqa: TRY004 - malformed file data.
        return value

    def execute(self, name, arguments):
        name = self.original_names.get(name, name)
        if name not in self.original_names.values() or name.startswith("_") or not isinstance(arguments, dict):
            raise ValueError("Unknown or malformed memory tool call")
        json.dumps(arguments, allow_nan=False)
        try:
            with self._memory_context():
                response = getattr(self.memory, name)(**copy.deepcopy(arguments))
            result = serialize_result(response)
        except Exception as error:  # noqa: BLE001 - match official simulated tool errors without replay.
            result = "Error during execution: " + str(error)
        self.results.append(result)
        return result

    def context_for_turn(self):
        template = _UNIFIED_TEMPLATE if self.case["variant"] == "rec_sum" else _CORE_TEMPLATE
        with self._memory_context():
            return template.format(scenario_setting=_SCENARIOS[self.case["scenario"]],
                                   memory_content=self.memory._dump_core_memory_to_context())

    def finish_entry(self):
        if self._finished:
            raise ValueError("A memory entry must not be committed twice")
        if self.case["category"] == "memory_prereq":
            with FileLock(self._lock_path):
                receipts = self._receipts()
                if receipts.get(self._dependency_key, []) != self.case.get("depends_on", []):
                    raise ValueError("Memory dependency state changed before entry completion")
                with self._memory_context():
                    self.memory._flush_memory_to_local_file()
                receipts[self._dependency_key] = [*self.case.get("depends_on", []), self.case["id"]]
                atomic_write(self._receipt_path, json.dumps(receipts, ensure_ascii=False, indent=2).encode())
        # Official base_handler flushes only prerequisites. Assessment writes
        # remain local to that independent session and cannot alter scenario_final.
        self._finished = True

    def call_expressions(self, _expressions):
        raise BFCLAgenticUnsupportedError("Memory assessment uses final-answer scoring, not a reference action script")

    def close(self):
        pass


def vector_backend_status(encoder=None):
    """Describe dependencies without importing eager model-loading modules."""
    if encoder is None:
        reason = "missing_explicit_local_official_encoder"
    elif (getattr(encoder, "model_id", None) != "sentence-transformers/all-MiniLM-L6-v2"
          or getattr(encoder, "local_files_only", None) is not True
          or getattr(encoder, "dimension", None) != 384):
        reason = "encoder_identity_or_offline_contract_mismatch"
    else:
        import importlib.util

        if importlib.util.find_spec("faiss") is None:
            reason = "missing_optional_faiss_dependency"
        else:
            return {"status": "explicit_offline_dependencies_available_unverified_encoder_weights",
                    "reason": "actual_local_encoder_identity_and_weights_require_caller_admission",
                    "model_api_calls": 0, "model_downloads": 0, "official_runtime": False}
    return {"status": "unsupported_official_runtime", "reason": reason,
            "model_api_calls": 0, "model_downloads": 0, "official_runtime": False}


def validate_encoder_output(vectors, *, expected_rows, dimension=384):
    """Validate an explicit encoder fixture's shape; never call an encoder."""
    if (not isinstance(vectors, list) or len(vectors) != expected_rows
            or any(not isinstance(row, list) or len(row) != dimension for row in vectors)
            or any(type(value) not in {int, float} or not math.isfinite(value) for row in vectors for value in row)):
        raise ValueError("Invalid local encoder output shape or values")
    return {"shape": [expected_rows, dimension], "protocol_only": True, "official_runtime": False}


def create_agentic_environment(case, root, *, fixtures=None, encoder=None):
    if case["category"] == "web_search":
        return WebFixtureEnvironment(case, root, fixtures=fixtures)
    if case["category"] in {"memory", "memory_prereq"}:
        return MemoryEnvironment(case, root, encoder=encoder)
    raise ValueError("Not a BFCL Agentic case")


async def run_memory_offline(case, scripts_by_entry, output_dir, *, limits=None, cancel=None, encoder=None):
    """Run the entire prerequisite chain and assessment with one shared budget.

    Each prerequisite segment and assessment gets a fresh Aster session. Only
    the official explicit memory snapshot crosses that conversation boundary.
    """
    from aster.evaluation.bfcl_v4_runner import OfflineCaseBudget, OfflineLimits, run_case_offline

    if case.get("category") != "memory":
        raise ValueError("A memory assessment with complete dependencies is required")
    if case.get("variant") == "vector" and encoder is None:
        raise BFCLAgenticUnsupportedError("Official vector runtime is not admitted; no encoder may auto-load")
    limits = limits or OfflineLimits()
    if not isinstance(limits, OfflineLimits):
        raise TypeError("OfflineLimits is required")
    prerequisites = case.get("memory_prerequisites", [])
    entries = [*prerequisites, case]
    ids = [entry["id"] for entry in entries]
    if (not prerequisites or len(set(ids)) != len(ids) or not isinstance(scripts_by_entry, dict)
            or set(scripts_by_entry) != set(ids)):
        raise ValueError("An explicit offline script is required for every complete memory entry")
    for index, entry in enumerate(entries):
        if (entry.get("variant") != case["variant"] or entry.get("scenario") != case["scenario"]
                or entry.get("depends_on", []) != ids[:index]
                or (entry.get("category") != "memory_prereq" and index != len(entries) - 1)):
            raise ValueError("Memory entries must keep the original complete prerequisite order")
    output = _root(output_dir)
    if output.exists():
        raise ValueError("Memory protocol evidence requires a new directory")
    output.mkdir(parents=True)
    snapshots = output / "snapshots"
    budget = OfflineCaseBudget(limits, path=output / "case-budget.json", cancel=cancel)
    reports = []
    stopped = None
    for entry in entries:
        if not budget.check() or budget.counts["requests"] >= limits.max_requests:
            stopped = "case_budget_or_cancellation_before_next_memory_entry"
            break
        def factory(current_case, *, root):
            del root
            return MemoryEnvironment(current_case, snapshots, encoder=encoder)

        report = await run_case_offline(entry, scripts_by_entry[entry["id"]],
            output / "entries" / hashlib.sha256(entry["id"].encode()).hexdigest()[:24],
            limits=limits, shared_budget=budget, environment_factory=factory, cancel=cancel)
        reports.append(report)
        if report["protocol_result"] != "pass":
            stopped = "memory_prerequisite_or_assessment_protocol_failed"
            break
    completed = len(reports) == len(entries) and stopped is None
    result = {"schema_version": 1, "id": case["id"], "mode": "offline_scripted_memory_sequence",
              "official_score": False, "model_quality": "unsupported", "model_api_calls": 0,
              "protocol_result": "pass" if completed else "fail", "stop_reason": stopped,
              "expected_entry_count": len(entries), "completed_entry_count": len(reports),
              "case_budget_counts": dict(budget.counts), "limits": {"max_requests": limits.max_requests,
              "max_tool_calls": limits.max_tool_calls, "deadline_seconds": limits.deadline_seconds},
              "entries": reports,
              "scoring_verification": reports[-1]["scoring_verification"] if completed else {"valid": None, "result": "not_run"}}
    if encoder is not None:
        result["encoder_protocol_only"] = getattr(encoder, "protocol_only", False)
        result["local_fixture_encode_calls"] = getattr(encoder, "calls", None)
    atomic_write(output / "result.json", json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False).encode())
    return result


async def verify_agentic_protocols(cases, output_dir):
    """Explicit synthetic protocol examples using the pinned task tool schemas.

    These are local observation fixtures, not solutions of the downloaded web
    or memory questions. Reference answers exist only in these fixture cases.
    """
    from aster.evaluation.bfcl_v4_data import normalize_case
    from aster.evaluation.bfcl_v4_official import grade_agentic_answer
    from aster.evaluation.bfcl_v4_runner import run_case_offline

    root = _root(output_dir)
    if root.exists():
        raise ValueError("Agentic protocol verification needs new evidence")
    root.mkdir(parents=True)
    results = []
    for variant in ("base", "no_snippet"):
        example = next((case for case in cases if case["category"] == "web_search" and case["variant"] == variant), None)
        if example is None:
            continue
        case = normalize_case({"id": "web_search_999999", "question": [[{"role": "user", "content":
            "Find the city in the local search observation, then open its source page to confirm."}]],
            "involved_classes": ["WebSearchAPI"]}, {"ground_truth": ["Oslo"]}, category="web_search", variant=variant,
            documents={"WebSearchAPI": example["functions"]})
        observations = {"search": {"fixture city": [{"title": "Fixture city", "href": "https://fixture.invalid/city",
                                                     "body": "Local example; open the page to confirm."}]},
                        "pages": {"https://fixture.invalid/city": {"truncate": "The fixture city is Oslo."}}}

        def factory(current, *, root, fixtures=observations):
            return WebFixtureEnvironment(current, root, fixtures=fixtures)

        script = [[{"calls": [{"name": "search_engine_query", "arguments": {"keywords": "fixture city"}}]},
                   {"calls": [{"name": "fetch_url_content", "arguments": {"url": "https://fixture.invalid/city", "mode": "truncate"}}]},
                   {"text": '{"answer":"Oslo","context":"confirmed with the local page observation"}'}]]
        report = await run_case_offline(case, script, root / ("web_" + variant), environment_factory=factory)
        negative = grade_agentic_answer("Wrong city sentinel", case["expected"], "web_search")
        results.append({"fixture": "web_search_" + variant, "synthetic": True, "protocol_result": report["protocol_result"],
                        "scoring_verification": report["scoring_verification"], "negative_answer_check": negative,
                        "model_quality": "unsupported", "model_api_calls": 0,
                        "evidence_file": str(root / ("web_" + variant) / "result.json")})
    for backend in ("kv", "rec_sum"):
        example = next((case for case in cases if case["category"] == "memory" and case["variant"] == backend), None)
        if example is None:
            continue
        documents = {"MemoryAPI_" + backend: example["functions"]}
        prerequisites = []
        for index, fact in enumerate(("My home city is Oslo.", "My meeting is on Friday.")):
            prerequisites.append(normalize_case({"id": f"memory_prereq_{index}-notetaker-{index}",
                "scenario": "notetaker", "question": [[{"role": "user", "content": fact}]]}, None,
                category="memory_prereq", variant=backend, documents=documents, prerequisites=prerequisites))
        case = normalize_case({"id": "memory_999999-notetaker-0", "scenario": "notetaker",
            "question": [[{"role": "user", "content": "What is my home city?"}]]}, {"ground_truth": ["Oslo"]},
            category="memory", variant=backend, documents=documents, prerequisites=prerequisites)
        scripts = {}
        for index, entry in enumerate(prerequisites):
            if backend == "kv":
                name, arguments = "core_memory_add", {"key": "home_city" if index == 0 else "meeting_day",
                                                     "value": "Oslo" if index == 0 else "Friday"}
            else:
                name, arguments = "memory_append", {"text": "Home city: Oslo.\n" if index == 0 else "Meeting day: Friday.\n"}
            scripts[entry["id"]] = [[{"calls": [{"name": name, "arguments": arguments}]}, {"text": "Stored the fixture fact."}]]
        call = {"name": "core_memory_retrieve", "arguments": {"key": "home_city"}} if backend == "kv" else {"name": "memory_retrieve", "arguments": {}}
        scripts[case["id"]] = [[{"calls": [call]}, {"text": '{"answer":"Oslo","context":"read from the isolated prior-session snapshot"}'}]]
        negative_chain_rejected = False
        try:
            MemoryEnvironment(case, root / ("missing_chain_" + backend))
        except ValueError:
            negative_chain_rejected = True
        report = await run_memory_offline(case, scripts, root / ("memory_" + backend))
        results.append({"fixture": "memory_" + backend, "synthetic": True, "protocol_result": report["protocol_result"],
                        "scoring_verification": report["scoring_verification"],
                        "negative_answer_check": grade_agentic_answer("Wrong city sentinel", case["expected"], "memory"),
                        "missing_chain_rejected": negative_chain_rejected,
                        "model_quality": "unsupported", "model_api_calls": 0,
                        "evidence_file": str(root / ("memory_" + backend) / "result.json")})
    passed = all(row["protocol_result"] == "pass" and row["scoring_verification"]["valid"] is True
                 and row["negative_answer_check"]["valid"] is False
                 and row.get("missing_chain_rejected", True) for row in results) if results else None
    return {"kind": "explicit_synthetic_agentic_protocol_fixtures", "model_quality": "unsupported", "model_api_calls": 0,
            "verification_passed": passed,
            "results": results, "protocol_totals": {key: sum(row["protocol_result"] == key for row in results)
            for key in ("pass", "fail", "unsupported")}, "vector": vector_backend_status()}
