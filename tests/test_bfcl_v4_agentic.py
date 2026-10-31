import copy
import json
import socket

import pytest
from bfcl_test_prerequisites import require_bm25

from aster.evaluation.bfcl_v4_agentic import (
    BFCLAgenticUnsupportedError,
    MemoryEnvironment,
    WebFixtureEnvironment,
    run_memory_offline,
    validate_encoder_output,
    vector_backend_status,
)
from aster.evaluation.bfcl_v4_data import normalize_case


def function(name, properties=None, required=None):
    return {"name": name, "description": "Offline protocol tool.", "parameters": {
        "type": "dict", "properties": properties or {}, "required": required or []}}


def web_case(variant="base"):
    documents = {"WebSearchAPI": [
        function("search_engine_query", {"keywords": {"type": "string"}}, ["keywords"]),
        function("fetch_url_content", {"url": {"type": "string"}, "mode": {"type": "string"}}, ["url"])]}
    return normalize_case({"id": "web_search_0", "question": [[{"role": "user", "content": "Find the city."}]],
                           "involved_classes": ["WebSearchAPI"]}, {"ground_truth": ["Oslo"]},
                          category="web_search", variant=variant, documents=documents)


def memory_chain(backend="kv"):
    if backend == "kv":
        functions = [function("core_memory_add", {"key": {"type": "string"}, "value": {"type": "string"}}, ["key", "value"]),
                     function("core_memory_retrieve", {"key": {"type": "string"}}, ["key"])]
    elif backend == "vector":
        functions = [function("core_memory_add", {"text": {"type": "string"}}, ["text"]),
                     function("core_memory_retrieve", {"query": {"type": "string"}}, ["query"])]
    else:
        functions = [function("memory_append", {"text": {"type": "string"}}, ["text"]), function("memory_clear"),
                     function("memory_replace", {"old_text": {"type": "string"}, "new_text": {"type": "string"}}, ["old_text", "new_text"]),
                     function("memory_retrieve"), function("memory_update", {"text": {"type": "string"}}, ["text"])]
    documents = {"MemoryAPI_" + backend: functions}
    entries = []
    for index in range(2):
        entry = normalize_case({"id": f"memory_prereq_{index}-notetaker-{index}", "scenario": "notetaker",
            "question": [[{"role": "user", "content": f"Remember fact {index}."}]]}, None,
            category="memory_prereq", variant=backend, documents=documents, prerequisites=entries)
        entries.append(entry)
    case = normalize_case({"id": "memory_0-notetaker-0", "scenario": "notetaker",
        "question": [[{"role": "user", "content": "What fact did I mention?"}]]}, {"ground_truth": ["Oslo"]},
        category="memory", variant=backend, documents=documents, prerequisites=entries)
    return entries, case


def forbid_network(monkeypatch):
    monkeypatch.setattr(socket, "create_connection", lambda *args, **kwargs: pytest.fail("Offline means no socket"))


def test_web_snippet_switch_and_page_modes_use_only_explicit_observations(tmp_path, monkeypatch):
    forbid_network(monkeypatch)
    fixtures = {"search": {"city": [{"title": "City", "href": "https://fixture.invalid/city", "body": "Oslo"}]},
                "pages": {"https://fixture.invalid/city": {"truncate": "Oslo"}}}
    first = WebFixtureEnvironment(web_case("base"), tmp_path / "with", fixtures=fixtures)
    second = WebFixtureEnvironment(web_case("no_snippet"), tmp_path / "without", fixtures=fixtures)
    assert "body" in first.execute("search_engine_query", {"keywords": "city"})
    assert "body" not in second.execute("search_engine_query", {"keywords": "city"})
    assert second.execute("fetch_url_content", {"url": "https://fixture.invalid/city", "mode": "truncate"}) == "Oslo"
    assert len(second.results) == 2 and len(second.requests) == 2
    with pytest.raises(BFCLAgenticUnsupportedError):
        second.execute("fetch_url_content", {"url": "https://fixture.invalid/city", "mode": "raw"})
    with pytest.raises(BFCLAgenticUnsupportedError):
        first.execute("search_engine_query", {"keywords": "unknown"})


def test_web_explicit_error_observation_is_returned_without_retry(tmp_path, monkeypatch):
    forbid_network(monkeypatch)
    env = WebFixtureEnvironment(web_case(), tmp_path, fixtures={"search": {"fail": {"error": "fixture 503"}}})
    result = env.execute("search_engine_query", {"keywords": "fail"})
    assert json.loads(result) == {"error": "fixture 503"}
    assert len(env.requests) == 1
    with pytest.raises(ValueError):
        env.execute("__import__", {})


@pytest.mark.parametrize("backend", ["kv", "rec_sum"])
def test_official_memory_cross_session_snapshot_and_evaluation_writes_are_isolated(tmp_path, monkeypatch, backend):
    if backend == "kv":
        require_bm25()
    forbid_network(monkeypatch)
    entries, case = memory_chain(backend)
    root = tmp_path / "snapshots"
    with pytest.raises(ValueError, match="chain"):
        MemoryEnvironment(case, root)
    for index, entry in enumerate(entries):
        env = MemoryEnvironment(entry, root)
        if backend == "kv":
            result = env.execute("core_memory_add", {"key": f"fact_{index}", "value": "Oslo"})
        else:
            result = env.execute("memory_append", {"text": f"fact_{index}=Oslo\n"})
        assert "status" in result
        env.finish_entry()
        with pytest.raises(ValueError, match="twice"):
            env.finish_entry()
    assessment = MemoryEnvironment(case, root)
    assert "Oslo" in assessment.context_for_turn()
    before = assessment._latest.read_bytes()
    if backend == "kv":
        assert json.loads(assessment.execute("core_memory_retrieve", {"key": "fact_0"})) == {"value": "Oslo"}
        assessment.execute("core_memory_add", {"key": "evaluation_only", "value": "Do not persist"})
    else:
        assert "Oslo" in assessment.execute("memory_retrieve", {})
        assessment.execute("memory_append", {"text": "Do not persist"})
    assessment.finish_entry()
    assert assessment._latest.read_bytes() == before
    fresh = MemoryEnvironment(case, root)
    assert "Do not persist" not in fresh.context_for_turn()


def test_unfinished_prerequisite_does_not_grant_next_entry_admission(tmp_path):
    require_bm25()
    entries, _case = memory_chain()
    env = MemoryEnvironment(entries[0], tmp_path / "snapshots")
    env.execute("core_memory_add", {"key": "fact", "value": "Oslo"})
    env.close()
    with pytest.raises(ValueError, match="chain"):
        MemoryEnvironment(entries[1], tmp_path / "snapshots")


def test_memory_snapshot_root_and_identity_cannot_escape(tmp_path):
    entries, case = memory_chain()
    bad = copy.deepcopy(case)
    bad["id"] = "../outside"
    with pytest.raises(ValueError, match="identity"):
        MemoryEnvironment(bad, tmp_path)
    with pytest.raises(ValueError, match="isolated"):
        MemoryEnvironment(entries[0], tmp_path / ".aster" / "nested")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "runtime.db").write_bytes(b"fixture")
    with pytest.raises(ValueError, match="Runtime"):
        MemoryEnvironment(entries[0], runtime / "snapshots")


def test_vector_dependency_contract_never_calls_encoder_or_imports_a_model():
    class Encoder:
        model_id = "sentence-transformers/all-MiniLM-L6-v2"
        dimension = 384
        local_files_only = True

        def encode(self, *_args):
            pytest.fail("Dependency inspection must not execute a model")
    assert vector_backend_status()["status"] == "unsupported_official_runtime"
    assert vector_backend_status(Encoder())["official_runtime"] is False
    assert validate_encoder_output([[0.0] * 384], expected_rows=1)["protocol_only"] is True
    for vectors in ([[0.0] * 383], [[float("inf")] * 384], [[True] * 384]):
        with pytest.raises(ValueError):
            validate_encoder_output(vectors, expected_rows=1)


@pytest.mark.asyncio
async def test_coordinator_search_feedback_drives_second_step_without_a_provider(tmp_path, monkeypatch):
    from aster.evaluation.bfcl_v4_runner import run_case_offline

    forbid_network(monkeypatch)
    case = web_case("no_snippet")
    fixtures = {"search": {"city": [{"title": "City", "href": "https://fixture.invalid/city", "body": "HIDDEN"}]},
                "pages": {"https://fixture.invalid/city": {"raw": "Oslo"}}}

    def factory(current, *, root):
        return WebFixtureEnvironment(current, root, fixtures=fixtures)
    script = [[{"calls": [{"name": "search_engine_query", "arguments": {"keywords": "city"}}]},
               {"calls": [{"name": "fetch_url_content", "arguments": {"url": "https://fixture.invalid/city"}}]},
               {"text": '{"answer":"Oslo","context":"read the fixture page"}'}]]
    report = await run_case_offline(case, script, tmp_path / "run", environment_factory=factory)
    assert report["protocol_result"] == "pass" and report["scoring_verification"]["valid"]
    assert report["model_api_calls"] == 0 and report["model_quality"] == "unsupported"
    assert "HIDDEN" not in json.dumps(report["messages"])
    assert len(report["tool_ledger"]) == 2
    assert any(message["role"] == "tool" and "fixture.invalid/city" in message["content"]
               for message in report["step_observations"][1]["messages"])


@pytest.mark.asyncio
async def test_memory_sequence_uses_one_budget_across_fresh_sessions(tmp_path, monkeypatch):
    require_bm25()
    from aster.evaluation.bfcl_v4_runner import OfflineLimits

    forbid_network(monkeypatch)
    entries, case = memory_chain()
    scripts = {entry["id"]: [[{"calls": [{"name": "core_memory_add", "arguments": {"key": f"fact_{index}", "value": "Oslo"}}]},
                             {"text": "Stored."}]] for index, entry in enumerate(entries)}
    scripts[case["id"]] = [[{"calls": [{"name": "core_memory_retrieve", "arguments": {"key": "fact_0"}}]},
                            {"text": "Oslo"}]]
    report = await run_memory_offline(case, scripts, tmp_path / "complete", limits=OfflineLimits(max_requests=6))
    assert report["protocol_result"] == "pass" and report["completed_entry_count"] == 3
    assert report["scoring_verification"]["valid"]
    assert report["case_budget_counts"]["requests"] == 6
    assert len({entry["session_id"] for entry in report["entries"]}) == 3
    limited = await run_memory_offline(case, scripts, tmp_path / "limited", limits=OfflineLimits(max_requests=2))
    assert limited["protocol_result"] == "fail" and limited["completed_entry_count"] == 1
    assert limited["case_budget_counts"]["requests"] == 2
    assert limited["scoring_verification"]["valid"] is None


@pytest.mark.asyncio
async def test_memory_exhausted_tool_quota_still_allows_core_context_answer(tmp_path):
    require_bm25()
    from aster.evaluation.bfcl_v4_runner import OfflineLimits

    entries, case = memory_chain()
    scripts = {entry["id"]: [[{"calls": [{"name": "core_memory_add", "arguments": {"key": f"fact_{index}", "value": "Oslo"}}]},
                             {"text": "Stored."}]] for index, entry in enumerate(entries)}
    scripts[case["id"]] = [[{"text": "Oslo"}]]
    report = await run_memory_offline(case, scripts, tmp_path / "tool-limit",
                                     limits=OfflineLimits(max_requests=5, max_tool_calls=2))
    assert report["protocol_result"] == "pass" and report["scoring_verification"]["valid"]
    assert report["case_budget_counts"] == {"requests": 5, "tool_calls": 2}


@pytest.mark.asyncio
async def test_agentic_verification_suite_records_explicit_fixture_boundaries(tmp_path, monkeypatch):
    require_bm25()
    from aster.evaluation.bfcl_v4_agentic import verify_agentic_protocols

    forbid_network(monkeypatch)
    cases = [web_case("base"), web_case("no_snippet"), memory_chain("kv")[1], memory_chain("rec_sum")[1]]
    report = await verify_agentic_protocols(cases, tmp_path / "agentic-suite")
    assert report["verification_passed"] is True and report["protocol_totals"]["pass"] == 4
    assert report["model_quality"] == "unsupported" and report["model_api_calls"] == 0
    assert report["vector"]["status"] == "unsupported_official_runtime"
    assert all(row["synthetic"] and row["negative_answer_check"]["valid"] is False for row in report["results"])
    assert all(row.get("missing_chain_rejected", True) for row in report["results"])


def test_vector_memory_uses_explicit_fixture_encoder_and_real_faiss(tmp_path, monkeypatch):
    pytest.importorskip("faiss")
    import numpy as np

    from aster.evaluation._vendor.bfcl_v4.vector_context import current_vector_encoder

    forbid_network(monkeypatch)

    class FixtureEncoder:
        model_id = "sentence-transformers/all-MiniLM-L6-v2"
        local_files_only = True
        dimension = 384
        protocol_only = True

        def __init__(self):
            self.calls = 0

        def encode(self, texts, *, normalize_embeddings):
            assert normalize_embeddings is True
            self.calls += 1
            result = np.zeros((len(texts), 384), dtype=np.float32)
            for index, text in enumerate(texts):
                result[index, {"Oslo": 0, "Friday": 1}[text]] = 1.0
            return result
    encoder = FixtureEncoder()
    entries, case = memory_chain("vector")
    root = tmp_path / "vector-snapshots"
    with pytest.raises(BFCLAgenticUnsupportedError):
        MemoryEnvironment(entries[0], root)
    for index, entry in enumerate(entries):
        env = MemoryEnvironment(entry, root, encoder=encoder)
        assert env.protocol_only and env.official_runtime is False
        assert "id" in env.execute("core_memory_add", {"text": "Oslo" if index == 0 else "Friday"})
        env.finish_entry()
        with pytest.raises(ValueError, match="no admitted"):
            current_vector_encoder()
    assessment = MemoryEnvironment(case, root, encoder=encoder)
    before = assessment._latest.read_bytes()
    retrieved = json.loads(assessment.execute("core_memory_retrieve", {"query": "Oslo"}))
    assert "Oslo" in json.dumps(retrieved)
    assert encoder.calls >= 4
    assessment.finish_entry()
    assert assessment._latest.read_bytes() == before
    assert "SentenceTransformer" not in json.dumps(assessment.context_for_turn())
    with pytest.raises(ValueError, match="no admitted"):
        current_vector_encoder()


@pytest.mark.asyncio
async def test_rec_sum_complete_tool_set_does_not_shadow_aster_builtin_memory_update(tmp_path, monkeypatch):
    forbid_network(monkeypatch)
    entries, case = memory_chain("rec_sum")
    assert {function["name"] for function in case["functions"]} == {
        "memory_append", "memory_clear", "memory_replace", "memory_retrieve", "memory_update"}
    scripts = {entries[0]["id"]: [[{"calls": [{"name": "memory_update", "arguments": {"text": "Home: Oslo.\n"}}]}, {"text": "Stored."}]],
               entries[1]["id"]: [[{"calls": [{"name": "memory_append", "arguments": {"text": "Meeting: Friday.\n"}}]}, {"text": "Stored."}]],
               case["id"]: [[{"calls": [{"name": "memory_retrieve", "arguments": {}}]}, {"text": "Oslo"}]]}
    report = await run_memory_offline(case, scripts, tmp_path / "rec-sum-full")
    assert report["protocol_result"] == "pass" and report["scoring_verification"]["valid"]
    first = report["entries"][0]
    assert any(row["tool_name"] == "bfcl_memory_update" for row in first["tool_ledger"])
    assert "bfcl_memory_update" in first["step_observations"][0]["offered_tools"]
    assert "memory_update" not in first["step_observations"][0]["offered_tools"]
    assert first["model_api_calls"] == 0 and first["model_quality"] == "unsupported"
