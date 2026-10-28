"""Offline relationship semantics and real mocked embedding protocol boundaries."""
import asyncio
import copy
import json

import httpx
import pytest

from aster.api import create_app
from aster.customization.credentials import CredentialStore
from aster.domain import Message, ToolCall, to_dict
from aster.memory.facts import StructuredMemory
from aster.memory.semantic import SemanticMemory, embedding_status, validate_embedding_config
from aster.memory.service import MemoryService
from aster.providers.deepseek import ProviderError
from aster.providers.profiles import ProviderProfiles
from aster.service import RunCoordinator
from aster.state import Store

TOKEN = "offline-memory-token-at-least-thirty-two-characters"


def view(store, tmp_path, agent="a", project="p"):
    return StructuredMemory(store, MemoryService(tmp_path / "memory", agent_id=agent, project_id=project))


def add(memory, entity, relation, value, *, scope="project", **kwargs):
    quote = f"{entity} {kwargs.get('entity_type', '')} {relation} {value} {kwargs.get('target_type', '')}"
    return memory.record(entity=entity, relation=relation, value=value, scope=scope,
                         sources=[{"kind": "user_supplied", "label": "explicit operator fixture", "quote": quote}], **kwargs)


def test_observed_forward_inverse_paths_and_explicit_type_neighborhoods(tmp_path):
    store = Store(tmp_path / "runtime.db")
    try:
        memory = view(store, tmp_path)
        first = add(memory, "Alice", "collaborates_with", "Bob")  # Legacy labels retain unknown type.
        second = add(memory, "Bob", "works_at", "Lab", entity_type="person", target_type="organization")
        third = add(memory, "Lab", "located_in", "City", entity_type="organization", target_type="city")
        forward = memory.query(mode="path_query", entity="Alice", path=["collaborates_with", "works_at", "located_in"])
        assert forward["paths"][0]["entity"] == "City"
        hops = forward["paths"][0]["observed_path"]
        assert [hop["fact_id"] for hop in hops] == [first["id"], second["id"], third["id"]]
        assert hops[0]["target_type"] is None
        assert all(hop["evidence"][0]["quote"] for hop in hops)
        inverse = memory.query(mode="path_query", entity="City", path=["located_in^-1", "works_at^-1", "collaborates_with^-1"])
        assert inverse["paths"][0]["entity"] == "Alice"
        assert all(hop["inverse"] for hop in inverse["paths"][0]["observed_path"])
        typed = memory.query(mode="typed_neighborhood", entity="Bob", relation="works_at", candidate_type="organization")
        assert typed["paths"][0]["entity"] == "Lab"
        assert memory.query(mode="typed_neighborhood", entity="Alice", relation="collaborates_with", candidate_type="person")["no_data"]
        # Same label with an explicitly incompatible type does not bridge.
        add(memory, "Lab", "belongs_to", "Bad", entity_type="person", target_type="person")
        assert memory.query(mode="path_query", entity="Bob", path=["works_at", "belongs_to"])["no_data"]
    finally:
        store.close()


def test_graph_scope_asof_supersede_retraction_and_source_type_rules(tmp_path):
    store = Store(tmp_path / "runtime.db")
    try:
        memory = view(store, tmp_path)
        other = view(store, tmp_path, project="foreign")
        edge = add(memory, "Alice", "works_at", "Old", entity_type="person", target_type="organization", observed_at="2026-01-01T00:00:00Z")
        add(other, "Old", "located_in", "PrivateCity", entity_type="organization", target_type="city")
        assert memory.query(mode="path_query", entity="Alice", path=["works_at", "located_in"])["no_data"]
        revised = memory.supersede(edge["id"], scope="project", expected_version=1, value="New", reason="new workplace",
            valid_from="2026-02-01T00:00:00Z", sources=[{"kind": "user_supplied", "label": "operator", "quote": "Alice works at New organization"}], target_type="organization")
        assert memory.query(entity="Alice", relation="works_at", at="2026-01-15T00:00:00Z")["facts"][0]["value"] == "Old"
        assert memory.query(entity="Alice", relation="works_at")["facts"][0]["value"] == "New"
        assert revised["entity_type"] == "person" and any("person" in item["quote"] for item in revised["evidence"])
        with pytest.raises(ValueError, match="new supplied source quotes"):
            memory.supersede(revised["id"], scope="project", expected_version=1, value="Unsupported", reason="missing new type proof",
                target_type="organization", sources=[{"kind": "user_supplied", "label": "operator", "quote": "Unsupported workplace"}])
        memory.forget(revised["id"], scope="project", expected_version=1, reason="withdrawn")
        assert memory.query(entity="Alice", relation="works_at", at="2026-03-01T00:00:00Z")["no_data"]
        with pytest.raises(ValueError, match="source quotes"):
            memory.record(entity="Alice", relation="works_at", value="Lab", scope="project", entity_type="inferred_person",
                          sources=[{"kind": "user_supplied", "label": "operator", "quote": "Alice works at Lab"}])
        with pytest.raises(ValueError, match="candidate_type"):
            memory.query(mode="typed_neighborhood", entity="Alice", relation="works_at")
        with pytest.raises(ValueError, match="multiple hops"):
            memory.query(mode="typed_neighborhood", entity="Alice", relation="works_at", path=["works_at"], candidate_type="organization")
    finally:
        store.close()


def test_direct_aggregation_conflict_nonnumeric_and_explicit_scan_truncation(tmp_path):
    store = Store(tmp_path / "runtime.db")
    try:
        memory = view(store, tmp_path)
        add(memory, "A", "mass", 2)
        add(memory, "B", "mass", 3)
        assert memory.query(mode="aggregate", relation="mass", aggregate="sum")["aggregation"]["value"] == 5
        assert memory.query(mode="aggregate", relation="mass", aggregate="count")["aggregation"]["value"] == 2
        add(memory, "B", "mass", 3)  # Duplicate supporting facts do not double-count a value.
        assert memory.query(mode="aggregate", relation="mass", aggregate="sum")["aggregation"]["value"] == 5
        conflicting = add(memory, "B", "mass", 4)
        unavailable = memory.query(mode="aggregate", relation="mass", aggregate="sum")
        assert unavailable["aggregation"] == {"operation": "sum", "available": False, "value": None, "reason": "conflicting_facts"}
        assert conflicting["id"] in unavailable["conflicts"][0]["fact_ids"]
        add(memory, "C", "weight", "unknown")
        assert memory.query(mode="aggregate", relation="weight", aggregate="sum")["aggregation"]["reason"] == "nonnumeric_facts"
        limited = memory.query(mode="aggregate", relation="mass", aggregate="count", scan_limit=1)
        assert limited["truncated"] and limited["scan_truncated"]
        assert limited["aggregation"]["reason"] == "candidate_scan_truncated"
        assert len(memory.query(relation="mass", limit=1)["facts"]) == 1
        with pytest.raises(ValueError, match="direct_relation"):
            memory.query(mode="aggregate", relation="mass^-1", aggregate="sum")
    finally:
        store.close()


def test_inherited_type_evidence_does_not_recursively_grow_across_revisions(tmp_path):
    store = Store(tmp_path / "runtime.db")
    try:
        memory = view(store, tmp_path)
        fact = add(memory, "Alice", "collaborates_with", "Bob", entity_type="person", target_type="person",
                   observed_at="2026-01-01T00:00:00Z")
        for number in range(2, 12):
            fact = memory.supersede(fact["id"], scope="project", expected_version=1, value="Bob", reason="another explicit source",
                valid_from=f"2026-01-{number:02d}T00:00:00Z",
                sources=[{"kind": "user_supplied", "label": f"source-{number}-{source}", "quote": "Alice collaborates with Bob"}
                         for source in range(20)])
            assert len(fact["evidence"]) <= 22
            assert fact["entity_type"] == fact["target_type"] == "person"
        assert sum(source["quote"].count("person") > 0 for source in fact["evidence"]) == 1
    finally:
        store.close()


def embedding_fixture(tmp_path, store, *, handler=None):
    requests = []

    def receive(request):
        body = json.loads(request.content)
        requests.append(body)
        assert request.url.path == "/v1/embeddings"
        assert request.headers["authorization"] == "Bearer offline-embedding-key"
        if handler is not None:
            response = handler(request, body)
            if response is not None:
                return response
        # Deliberately synthetic vectors test transport/ranking mechanics, not
        # semantic quality. No lexical/hashed vector is presented as production.
        vectors = [[1.0, 0.0] if "ocean" in text else [0.0, 1.0] for text in body["input"]]
        return httpx.Response(200, json={"data": [{"index": index, "embedding": vector} for index, vector in enumerate(vectors)]})

    credentials = CredentialStore(data_dir=tmp_path)
    providers = ProviderProfiles(store, credentials, client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(receive)))
    profile = providers.create(name="embedding fixture", base_url="https://fixture.example/v1", protocol="openai_chat",
                               api_key="offline-embedding-key")
    config = {"enabled": True, "provider_id": profile["id"], "model": "offline-embedding-model", "batch_size": 2}
    return providers, config, requests


async def test_actual_embedding_protocol_rank_scopes_persistent_cache_and_identity_invalidation(tmp_path):
    path = tmp_path / "runtime.db"
    store = Store(path)
    try:
        memory = view(store, tmp_path)
        ocean = add(memory, "ocean topic", "summary", "waves")
        other = add(memory, "forest topic", "summary", "trees")
        add(view(store, tmp_path, agent="other"), "private ocean", "summary", "private secret")
        add(view(store, tmp_path, project="foreign"), "foreign ocean", "summary", "foreign secret")
        providers, config, requests = embedding_fixture(tmp_path, store)
        semantic = SemanticMemory(memory, providers, config)
        result = await semantic.recall(query="ocean", scopes=["project"])
        assert result["facts"][0]["id"] == ocean["id"] and result["facts"][0]["score"] == 1
        assert {fact["id"] for fact in result["facts"]} == {ocean["id"], other["id"]}
        assert len(requests) == 2  # one candidate batch + query
        assert "private secret" not in json.dumps(requests) and "foreign secret" not in json.dumps(requests)
        store.close()
        store = Store(path)
        # Recreate profiles against the same persisted metadata and vectors.
        reopened_profiles = ProviderProfiles(store, CredentialStore(data_dir=tmp_path), client_factory=providers.client_factory)
        memory = view(store, tmp_path)
        semantic = SemanticMemory(memory, reopened_profiles, config)
        assert (await semantic.recall(query="ocean", scopes=["project"]))["facts"][0]["id"] == ocean["id"]
        assert len(requests) == 3  # cached facts survive restart, query remains explicit external work
        changed_model = SemanticMemory(memory, reopened_profiles, {**config, "model": "new-embedding-model"})
        await changed_model.recall(query="ocean", scopes=["project"])
        assert len(requests) == 5
        profile = reopened_profiles.get(config["provider_id"])
        reopened_profiles.update(profile["id"], expected_revision=profile["revision"], api_key="offline-embedding-key")
        await changed_model.recall(query="ocean", scopes=["project"])
        assert len(requests) == 7  # identity revision changed even when the fixture key string is reused
        assert store.connection.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0] == 6
    finally:
        store.close()


async def test_embedding_validity_retraction_version_change_and_input_scan_budgets(tmp_path):
    store = Store(tmp_path / "runtime.db")
    try:
        memory = view(store, tmp_path)
        first = add(memory, "ocean topic", "summary", "old", observed_at="2026-01-01T00:00:00Z")
        withdrawn = add(memory, "ocean withdrawn", "summary", "discard", observed_at="2026-01-01T00:00:00Z")
        providers, config, requests = embedding_fixture(tmp_path, store)
        semantic = SemanticMemory(memory, providers, config)
        await semantic.recall(query="ocean", scopes=["project"])
        memory.forget(withdrawn["id"], scope="project", expected_version=1, reason="withdrawn")
        second = memory.supersede(first["id"], scope="project", expected_version=1, value="new", reason="new source",
                                  valid_from="2026-02-01T00:00:00Z", sources=[{"kind": "user_supplied", "label": "operator", "quote": "new"}])
        current = await semantic.recall(query="ocean", scopes=["project"])
        assert [fact["id"] for fact in current["facts"]] == [second["id"]]
        historical = await semantic.recall(query="ocean", scopes=["project"], at="2026-01-15T00:00:00Z")
        assert [fact["id"] for fact in historical["facts"]] == [first["id"]]
        assert store.connection.execute("SELECT COUNT(*) FROM memory_embeddings WHERE fact_id=?", (withdrawn["id"],)).fetchone()[0] == 0
        assert len(requests) == 6  # revised effective and historical versions each reindexed
        large = add(memory, "large ocean", "summary", "x" * 1000)
        clipped = SemanticMemory(memory, providers, {**config, "input_chars": 100, "scan_limit": 1})
        result = await clipped.recall(query="ocean", scopes=["project"])
        assert result["truncated"] and result["scan_truncated"]
        assert result["input_truncated_fact_ids"] == [large["id"]]
        assert all(len(text) <= 100 for text in requests[-2]["input"])
        bounded_rank = SemanticMemory(memory, providers, {**config, "rank_limit": 1})
        assert len((await bounded_rank.recall(query="ocean"))["facts"]) == 1
    finally:
        store.close()


@pytest.mark.parametrize("payload", [
    {"data": []}, {"data": [{"index": 0, "embedding": [0, 0]}]},
    {"data": [{"index": 0, "embedding": [True, 1]}]},
    {"data": [{"index": 0, "embedding": [float('nan'), 1]}]},
])
async def test_embedding_invalid_vectors_are_terminal_generic_errors(payload, tmp_path):
    store = Store(tmp_path / "runtime.db")
    try:
        memory = view(store, tmp_path)
        add(memory, "ocean topic", "summary", "waves")
        providers, config, requests = embedding_fixture(tmp_path, store, handler=lambda request, body: httpx.Response(200, text=json.dumps(payload)))
        with pytest.raises(ProviderError, match="invalid vector schema"):
            await SemanticMemory(memory, providers, config).recall(query="ocean")
        assert len(requests) == 1 and store.connection.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0] == 0
    finally:
        store.close()


async def test_withdrawal_during_embedding_await_cannot_revive_or_return_the_fact(tmp_path):
    store = Store(tmp_path / "runtime.db")
    memory = view(store, tmp_path)
    fact = add(memory, "ocean topic", "summary", "waves")

    def withdraw(request, body):
        if body["input"][0].startswith("Entity:"):
            memory.forget(fact["id"], scope="project", expected_version=1, reason="withdrawn during query")

    try:
        providers, config, _ = embedding_fixture(tmp_path, store, handler=withdraw)
        result = await SemanticMemory(memory, providers, config).recall(query="ocean")
        assert result["no_data"] and result["facts"] == []
        assert store.connection.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0] == 0
    finally:
        store.close()


@pytest.mark.parametrize("status", [302, 401, 429, 503])
async def test_embedding_http_errors_and_redirects_do_not_retry_or_echo_secrets(status, tmp_path):
    store = Store(tmp_path / "runtime.db")
    try:
        memory = view(store, tmp_path)
        add(memory, "ocean", "summary", "waves")
        providers, config, requests = embedding_fixture(tmp_path, store, handler=lambda request, body:
            httpx.Response(status, headers={"location": "https://attacker.invalid"},
                           json={"error": {"message": "offline-embedding-key secret private prompt"}}))
        with pytest.raises(ProviderError) as error:
            await SemanticMemory(memory, providers, config).recall(query="ocean")
        assert len(requests) == 1 and "offline-embedding-key" not in str(error.value)
        assert error.value.details == {}
        assert store.connection.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0] == 0
    finally:
        store.close()


async def test_embedding_cancellation_closes_real_stream_without_cache_or_second_request(tmp_path):
    store = Store(tmp_path / "runtime.db")
    began, closed, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            began.set()
            try:
                await asyncio.Event().wait()
                yield b"never"
            finally:
                closed.set()

    try:
        memory = view(store, tmp_path)
        add(memory, "ocean", "summary", "waves")
        providers, config, requests = embedding_fixture(tmp_path, store, handler=lambda request, body:
            httpx.Response(200, stream=Stream()))
        work = asyncio.create_task(SemanticMemory(memory, providers, config).recall(query="ocean", cancel=cancelled))
        await asyncio.wait_for(began.wait(), 1)
        cancelled.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(work, 1)
        assert closed.is_set() and len(requests) == 1
        assert store.connection.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0] == 0
    finally:
        store.close()


async def test_embedding_profile_change_during_request_invalidates_late_response(tmp_path):
    store = Store(tmp_path / "runtime.db")
    try:
        memory = view(store, tmp_path)
        add(memory, "ocean", "summary", "waves")
        providers, config, requests = embedding_fixture(tmp_path, store)
        original_factory = providers.client_factory
        http = original_factory()
        original_handler = http._transport.handler
        await http.aclose()

        def handler(request):
            response = original_handler(request)
            profile = providers.get(config["provider_id"])
            providers.update(profile["id"], expected_revision=profile["revision"], base_url="https://changed.example/v1")
            return response

        providers.client_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
        with pytest.raises(ProviderError, match="identity changed") as error:
            await SemanticMemory(memory, providers, config).recall(query="ocean")
        assert error.value.category == "configuration_changed" and len(requests) == 1
        assert store.connection.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone()[0] == 0
    finally:
        store.close()


async def test_disabled_semantic_retrieval_never_falls_back_to_keyword_or_requests(tmp_path):
    store = Store(tmp_path / "runtime.db")
    try:
        memory = view(store, tmp_path)
        add(memory, "ocean", "summary", "waves")
        providers, _, requests = embedding_fixture(tmp_path, store)
        assert memory.recall(query="ocean")["facts"]
        with pytest.raises(ProviderError) as error:
            await SemanticMemory(memory, providers, {}).recall(query="ocean")
        assert error.value.category == "unavailable" and requests == []
    finally:
        store.close()


@pytest.mark.parametrize("change", ["supersede", "withdraw_cached"])
async def test_late_fact_revision_or_cached_withdrawal_cannot_leak_old_result(change, tmp_path):
    store = Store(tmp_path / "runtime.db")
    memory = view(store, tmp_path)
    writer = view(store, tmp_path)  # Independent same-scope writer during the network await.
    fact = add(memory, "ocean", "summary", "old private value", observed_at="2026-01-01T00:00:00Z")
    try:
        providers, config, requests = embedding_fixture(tmp_path, store)
        semantic = SemanticMemory(memory, providers, config)
        if change == "withdraw_cached":
            await semantic.recall(query="ocean")
        http = providers.client_factory()
        original = http._transport.handler
        await http.aclose()

        def revise(request):
            response = original(request)
            if change == "supersede":
                writer.supersede(fact["id"], scope="project", expected_version=1, value="new value", reason="new source",
                    valid_from="2026-02-01T00:00:00Z", sources=[{"kind": "user_supplied", "label": "operator", "quote": "new value"}])
            else:
                writer.forget(fact["id"], scope="project", expected_version=1, reason="withdrawn while query awaited")
            # Only mutate once; the supersede query vector remains an explicit
            # next phase, not another indexing/payment attempt for new facts.
            providers.client_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(original))
            return response

        providers.client_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(revise))
        before = len(requests)
        result = await semantic.recall(query="ocean")
        assert result["state_changed"] and result["facts"] == []
        assert "old private value" not in json.dumps(result)
        assert len(requests) - before == (2 if change == "supersede" else 1)
        assert store.connection.execute("SELECT COUNT(*) FROM memory_embeddings WHERE fact_id=?", (fact["id"],)).fetchone()[0] == 0
        if change == "supersede":
            assert result["truncated"] and result["available_candidate_count"] == 1
    finally:
        store.close()


def test_embedding_config_status_disabled_unconfigured_and_validation(tmp_path):
    store = Store(tmp_path / "runtime.db")
    try:
        providers, config, requests = embedding_fixture(tmp_path, store)
        assert embedding_status({}, providers)["status"] == "disabled"
        assert embedding_status(config, providers)["status"] == "ready"
        profile = providers.get(config["provider_id"])
        providers.delete_key(profile["id"], profile["revision"])
        assert embedding_status(config, providers)["status"] == "unconfigured"
        for bad in ({"batch_size": True}, {"enabled": "true"}, {"model": "a b"}, {"api_key": "secret"}, {"enabled": True}):
            with pytest.raises(ValueError):
                validate_embedding_config(bad, providers)
        assert requests == []
    finally:
        store.close()


async def test_memory_api_queries_real_service_and_semantic_requires_external_approval(tmp_path):
    coordinator = RunCoordinator(tmp_path / "data", tools=[])
    profile = coordinator.providers.create(name="embed fixture", base_url="https://fixture.example/v1", protocol="openai_chat", api_key="offline-embedding-key")
    requests = []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, json={"data": [{"index": i, "embedding": [1.0, 0.0]} for i in range(len(body["input"]))]})

    coordinator.providers.client_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(handle))
    (coordinator.data_dir / "config.yaml").write_text(
        f"embedding:\n  enabled: true\n  provider_id: {profile['id']}\n  model: explicit-embedding-model\n", encoding="utf-8")
    app = create_app(coordinator, token=TOKEN)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost",
            headers={"Authorization": "Bearer " + TOKEN}) as client:
        sid = coordinator.create_session(tmp_path)["id"]
        other = coordinator.create_session(tmp_path, agent_id="foreign")["id"]
        memory = view(coordinator.store, coordinator.data_dir, agent="default", project=None)
        fact = add(memory, "ocean topic", "summary", "waves", scope="agent")
        query = await client.post(f"/customization/sessions/{sid}/memory_query", json={"arguments": {"relation": "summary"}})
        assert query.status_code == 200 and query.json()["data"]["memory"]["facts"][0]["id"] == fact["id"]
        isolated = await client.post(f"/customization/sessions/{other}/memory_query", json={"arguments": {"relation": "summary"}})
        assert isolated.json()["data"]["memory"]["no_data"]
        endpoint = f"/customization/sessions/{sid}/memory_semantic_recall"
        assert (await client.post(endpoint, json={"arguments": {"query": "ocean"}})).status_code == 403
        assert requests == []
        result = (await client.post(endpoint, json={"arguments": {"query": "ocean"}, "approved": True})).json()
        assert not result["is_error"] and result["data"]["memory"]["facts"][0]["id"] == fact["id"]
        assert len(requests) == 2
        run = coordinator.store.list_runs(sid)[0]
        execution = coordinator.store.tool_executions(run["id"])[0]
        assert execution["risk"] == "external" and execution["state"] == "checkpointed"
        (coordinator.data_dir / "config.yaml").write_text("permissions:\n  denied_tools: [memory_semantic_recall]\n", encoding="utf-8")
        denied = (await client.post(endpoint, json={"arguments": {"query": "ocean"}, "approved": True})).json()
        assert denied["is_error"] and len(requests) == 2


async def test_manual_compaction_seeds_legacy_constraints_from_lossless_archive(tmp_path):
    coordinator = RunCoordinator(tmp_path / "data", tools=[])
    try:
        sid = coordinator.create_session(tmp_path)["id"]
        run, _ = coordinator.store.create_run(sid)
        raw = [Message("user", "Use CSV"), Message("user", "Use JSON instead"), Message("user", "Use CSV")]
        for number in range(10):
            raw.extend([Message("assistant", tool_calls=[ToolCall(str(number), "read", {})]),
                        Message("tool", "x" * 2000, tool_call_id=str(number))])
        for message in raw:
            coordinator.store.append_message(sid, run["id"], message)
        coordinator.store.set_run_status(run["id"], "completed")
        original = copy.deepcopy(to_dict(coordinator.store.history(sid)))
        coordinator.store.checkpoint(sid, run["id"], [Message("user", "stale summary " * 3000,
            metadata={"compaction_summary": True}), *raw[-4:]])
        result = await coordinator.manual_compact(sid)
        assert result["changed"] and result["preserved_archive"]
        assert coordinator.store.active_history(sid)[0].metadata["protected_requests"] == ["Use JSON instead", "Use CSV"]
        assert to_dict(coordinator.store.history(sid)) == original
    finally:
        await coordinator.close()
