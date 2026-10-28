"""Local embedding routing uses real memory/cache/policy with an offline worker double."""
import asyncio
import builtins
import copy
import hashlib
import inspect
import json
import shutil

import httpx
import pytest
import yaml
from test_local_embedding_backend import FAKE_WORKER, model_directory

from aster.api import create_app
from aster.domain import ModelTurn, ToolCall
from aster.engine import FakeModel
from aster.memory.facts import StructuredMemory
from aster.memory.semantic import SemanticMemory, embedding_status, validate_embedding_config
from aster.memory.service import MemoryService
from aster.providers.deepseek import ProviderError
from aster.service import RunCoordinator
from aster.state import StateError, Store

TOKEN = "local-embedding-offline-token-at-least-thirty-two-characters"


class NoRemoteProviders:
    def __getattr__(self, name):
        raise AssertionError("Local embeddings must not resolve provider keys or contact remote providers")


class OfflineWorker:
    def __init__(self):
        self.requests = []
        self.version = 1
        self.hook = None
        self.closed = False

    def pin(self, config):
        fields = {key: config[key] for key in ("model_path", "device", "local_max_tokens")}
        identity = hashlib.sha256(json.dumps({**fields, "version": self.version}, sort_keys=True).encode()).hexdigest()
        return {**fields, "backend": "local", "identity_sha": identity, "version": self.version}

    def validate_pin(self, pin):
        if pin["version"] != self.version:
            raise ProviderError("Local model changed", category="configuration_changed")

    def status(self, config):
        return {"state": "closed" if self.closed else "ready" if self.requests else "unloaded",
                "device": "cpu" if self.requests else None, "error_code": None, "dependencies_available": True}

    async def embed(self, texts, pin, *, cancel=None, timeout_seconds=30, batch_size=8):
        self.requests.append(list(texts))
        if self.hook:
            result = self.hook(texts)
            if inspect.isawaitable(result):
                await result
        return {"vectors": [[1.0, 0.0] if "ocean" in text else [0.0, 1.0] for text in texts],
                "truncated_indices": [i for i, text in enumerate(texts) if "CLIP" in text], "device": "cpu"}

    async def close(self):
        self.closed = True


@pytest.fixture
def offline_local(monkeypatch, tmp_path):
    from aster.memory import local_embedding
    monkeypatch.setattr(local_embedding, "validate_local_config", lambda config: {
        "configured": bool(config.get("model_path")), "available": bool(config.get("model_path")),
        "status": "ready", "reason": "Offline fixture is configured; no process started",
        "model_path": config.get("model_path"), "dependencies_available": True})
    config = {"enabled": True, "backend": "local", "model_path": str(tmp_path / "model"),
              "device": "cpu", "local_max_tokens": 1024, "batch_size": 2}
    return config, OfflineWorker()


def memory_view(store, root, *, agent="a", project="p"):
    return StructuredMemory(store, MemoryService(root / "memory", agent_id=agent, project_id=project))


def fact(memory, entity, value, *, scope="project"):
    return memory.record(entity=entity, relation="preference", value=value, scope=scope,
        sources=[{"kind": "user_supplied", "label": "offline fixture", "quote": entity + " " + value}])


def test_local_config_does_not_require_or_resolve_remote_credentials(offline_local):
    config, worker = offline_local
    assert validate_embedding_config({})["backend"] == "remote"
    validated = validate_embedding_config(config, NoRemoteProviders())
    assert validated["provider_id"] is None and validated["model"] is None
    status = embedding_status(validated, NoRemoteProviders(), worker)
    assert status["status"] == "ready" and status["local"]["state"] == "unloaded"
    assert worker.requests == []
    for change in ({"model_path": "relative/model"}, {"model_path": None}, {"backend": []},
                   {"device": "mps"}, {"local_max_tokens": True}, {"local_max_tokens": 8193}):
        with pytest.raises(ValueError):
            validate_embedding_config({**config, **change}, NoRemoteProviders())


async def test_local_rank_scope_restart_cache_identity_and_truncation(offline_local, tmp_path):
    config, worker = offline_local
    path = tmp_path / "runtime.db"
    store = Store(path)
    try:
        memory = memory_view(store, tmp_path)
        ocean = fact(memory, "ocean topic", "CLIP waves")
        fact(memory, "forest topic", "trees")
        hidden = fact(memory_view(store, tmp_path, agent="foreign", project="other"), "ocean foreign", "secret")
        semantic = SemanticMemory(memory, NoRemoteProviders(), config, local_manager=worker)
        first = await semantic.recall(query="ocean", limit=1)
        assert first["facts"][0]["id"] == ocean["id"]
        assert first["retrieval"] == "local_embedding_cosine"
        assert first["embedding"]["device"] == "cpu"
        assert first["input_truncated_fact_ids"] == [ocean["id"]]
        assert first["truncated"] and not first["query_truncated"]
        assert hidden["id"] not in str(first) and "secret" not in str(worker.requests)
        assert len(worker.requests) == 2
    finally:
        store.close()
    reopened = Store(path)
    try:
        semantic = SemanticMemory(memory_view(reopened, tmp_path), NoRemoteProviders(), config, local_manager=worker)
        again = await semantic.recall(query="ocean")
        assert len(worker.requests) == 3  # Cached facts; only the new query is embedded.
        assert again["input_truncated_fact_ids"] == [ocean["id"]]
        worker.version += 1
        await semantic.recall(query="ocean")
        assert len(worker.requests) == 5
        assert reopened.connection.execute("SELECT count(*) FROM memory_embeddings").fetchone()[0] == 4
        clipped_query = await semantic.recall(query="ocean CLIP")
        assert clipped_query["query_truncated"] and clipped_query["truncated"]
    finally:
        reopened.close()


@pytest.mark.parametrize("mutation", ["config", "model", "cancel"])
async def test_late_local_result_cannot_apply_after_identity_change_or_cancel(offline_local, tmp_path, mutation):
    config, worker = offline_local
    current = copy.deepcopy(config)
    cancelled = asyncio.Event()
    store = Store(tmp_path / "runtime.db")
    try:
        memory = memory_view(store, tmp_path)
        fact(memory, "ocean", "waves")
        def change(_):
            if mutation == "config":
                current["backend"] = "remote"
                current["provider_id"], current["model"] = "remote-id", "remote-model"
            elif mutation == "model":
                worker.version += 1
            else:
                cancelled.set()
        worker.hook = change
        semantic = SemanticMemory(memory, NoRemoteProviders(), config, local_manager=worker, config_loader=lambda: current)
        expected = asyncio.CancelledError if mutation == "cancel" else ProviderError
        with pytest.raises(expected) as error:
            await semantic.recall(query="ocean", cancel=cancelled)
        if mutation != "cancel":
            assert error.value.category == "configuration_changed"
        assert len(worker.requests) == 1
        assert store.connection.execute("SELECT count(*) FROM memory_embeddings").fetchone()[0] == 0
    finally:
        store.close()


async def test_local_withdrawal_while_worker_runs_does_not_revive_fact(offline_local, tmp_path):
    config, worker = offline_local
    store = Store(tmp_path / "runtime.db")
    try:
        memory = memory_view(store, tmp_path)
        row = fact(memory, "ocean", "waves")
        def withdraw(_):
            worker.hook = None
            memory.forget(fact_id=row["id"], expected_version=row["version"], reason="withdraw test", scope="project")
        worker.hook = withdraw
        result = await SemanticMemory(memory, NoRemoteProviders(), config, local_manager=worker).recall(query="ocean")
        assert result["no_data"] and result["state_changed"]
        assert store.connection.execute("SELECT count(*) FROM memory_embeddings").fetchone()[0] == 0
    finally:
        store.close()


async def test_local_api_needs_no_external_approval_read_refresh_is_lazy_and_shutdown_closes(offline_local, tmp_path):
    config, worker = offline_local
    coordinator = RunCoordinator(tmp_path / "data", tools=[])
    coordinator.local_embeddings = worker
    (coordinator.data_dir / "config.yaml").write_text(yaml.safe_dump({"embedding": config}), encoding="utf-8")
    app = create_app(coordinator, token=TOKEN)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
            base_url="http://localhost", headers={"Authorization": "Bearer " + TOKEN}) as client:
        state = (await client.get("/management")).json()
        assert state["extensions"]["embedding_status"]["local"]["state"] == "unloaded"
        saved = await client.patch("/management", json={"expected_revision": state["revision"], "embedding": config})
        assert saved.status_code == 200 and worker.requests == []
        assert "local_embedding_v1" in (await client.get("/capabilities")).json()["capabilities"]
        sid = coordinator.create_session(tmp_path)["id"]
        memory = memory_view(coordinator.store, coordinator.data_dir, agent="default", project=None)
        row = fact(memory, "ocean", "waves", scope="agent")
        result = await client.post(f"/customization/sessions/{sid}/memory_semantic_recall",
                                   json={"arguments": {"query": "ocean"}, "approved": False})
        assert result.status_code == 200 and not result.json()["is_error"]
        assert result.json()["data"]["memory"]["facts"][0]["id"] == row["id"]
        run = coordinator.store.list_runs(sid)[0]
        assert coordinator.store.tool_executions(run["id"])[0]["risk"] == "read"
        assert (await client.get("/management")).json()["extensions"]["embedding_status"]["local"]["state"] == "ready"
        current = yaml.safe_load((coordinator.data_dir / "config.yaml").read_text())
        current["permissions"] = {"denied_tools": ["memory_semantic_recall"]}
        (coordinator.data_dir / "config.yaml").write_text(yaml.safe_dump(current), encoding="utf-8")
        denied = (await client.post(f"/customization/sessions/{sid}/memory_semantic_recall",
                                   json={"arguments": {"query": "ocean"}})).json()
        assert denied["is_error"] and len(worker.requests) == 2
    assert worker.closed


async def test_local_to_remote_race_cannot_inherit_unapproved_local_api_gate(offline_local, tmp_path, monkeypatch):
    config, worker = offline_local
    coordinator = RunCoordinator(tmp_path / "data", tools=[])
    coordinator.local_embeddings = worker
    path = coordinator.data_dir / "config.yaml"
    path.write_text(yaml.safe_dump({"embedding": config}), encoding="utf-8")
    profile = coordinator.providers.create(name="offline remote", base_url="https://fixture.example/v1",
        protocol="openai_chat", api_key="offline-never-send")
    requests = []
    coordinator.providers.client_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: requests.append(request) or httpx.Response(500)))
    execute = coordinator.execute_session_tool
    async def changed(*args, **kwargs):
        path.write_text(yaml.safe_dump({"embedding": {"enabled": True, "provider_id": profile["id"],
                                                        "model": "remote-embedding"}}), encoding="utf-8")
        return await execute(*args, **kwargs)
    monkeypatch.setattr(coordinator, "execute_session_tool", changed)
    app = create_app(coordinator, token=TOKEN)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
            base_url="http://localhost", headers={"Authorization": "Bearer " + TOKEN}) as client:
        sid = coordinator.create_session(tmp_path)["id"]
        fact(memory_view(coordinator.store, coordinator.data_dir, agent="default", project=None), "ocean", "waves", scope="agent")
        response = await client.post(f"/customization/sessions/{sid}/memory_semantic_recall",
                                     json={"arguments": {"query": "ocean"}, "approved": False})
        assert response.status_code == 200 and response.json()["is_error"]
        assert requests == [] and worker.requests == []
        assert (await client.post(f"/customization/sessions/{sid}/memory_semantic_recall",
                                  json={"arguments": {"query": "ocean"}})).status_code == 403


async def test_project_embedding_override_cannot_select_another_local_model(offline_local, tmp_path):
    config, worker = offline_local
    coordinator = RunCoordinator(tmp_path / "data", tools=[])
    coordinator.local_embeddings = worker
    try:
        (coordinator.data_dir / "config.yaml").write_text(yaml.safe_dump({"embedding": config}), encoding="utf-8")
        agent = coordinator.data_dir / "agents" / "default"
        agent.mkdir(parents=True, exist_ok=True)
        (agent / "config.yaml").write_text(yaml.safe_dump({"embedding": {"model_path": str(tmp_path / "untrusted")}}), encoding="utf-8")
        project = coordinator.data_dir / "projects" / "project-one"
        project.mkdir(parents=True)
        (project / "config.yaml").write_text(yaml.safe_dump({"embedding": {"model_path": str(tmp_path / "also-untrusted")}}), encoding="utf-8")
        sid = coordinator.create_session(tmp_path, project_id="project-one")["id"]
        fact(memory_view(coordinator.store, coordinator.data_dir, agent="default", project="project-one"), "ocean", "waves", scope="project")
        result = await coordinator.execute_session_tool(sid, "memory_semantic_recall", {"query": "ocean"})
        assert not result.is_error
        assert result.data["memory"]["embedding"]["model_path"] == config["model_path"]
    finally:
        await coordinator.close()


async def test_model_dispatcher_uses_local_read_policy_without_remote_calls(offline_local, tmp_path):
    config, worker = offline_local
    model = FakeModel([ModelTurn(tool_calls=[ToolCall("semantic-local", "memory_semantic_recall", {"query": "ocean"})]),
                       ModelTurn("Recalled the local fact")])
    coordinator = RunCoordinator(tmp_path / "data", tools=[], model_factory=lambda _: model)
    coordinator.local_embeddings = worker
    requests = []
    coordinator.providers.client_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: requests.append(request) or httpx.Response(500)))
    try:
        (coordinator.data_dir / "config.yaml").write_text(yaml.safe_dump({"embedding": config}), encoding="utf-8")
        sid = coordinator.create_session(tmp_path)["id"]
        remembered = fact(memory_view(coordinator.store, coordinator.data_dir, agent="default", project=None),
                          "ocean", "waves", scope="agent")
        run = await coordinator.submit(sid, "Recall my ocean preference", unattended=True)
        outcome = await coordinator.wait(run["id"], timeout=5)
        assert outcome.status == "completed" and len(worker.requests) == 2 and requests == []
        execution = coordinator.store.tool_executions(run["id"])[0]
        assert execution["risk"] == "read" and execution["state"] == "checkpointed"
        output = next(message for message in model.calls[1]["messages"] if message.role == "tool")
        assert remembered["id"] in output.content and "local_embedding_cosine" in output.content
    finally:
        await coordinator.close()


async def test_no_candidates_and_status_never_import_or_start_model(tmp_path, monkeypatch):
    from aster.memory import local_embedding
    root = tmp_path / "model"
    root.mkdir()
    (root / "1_Pooling").mkdir()
    (root / "modules.json").write_text(json.dumps([
        {"idx": 0, "name": "0", "path": "", "type": "sentence_transformers.models.Transformer"},
        {"idx": 1, "name": "1", "path": "1_Pooling", "type": "sentence_transformers.models.Pooling"}]), encoding="utf-8")
    (root / "config.json").write_text('{"model_type":"bert"}', encoding="utf-8")
    (root / "tokenizer.json").write_text("{}", encoding="utf-8")
    (root / "1_Pooling" / "config.json").write_text("{}", encoding="utf-8")
    (root / "model.safetensors").write_bytes(b"offline-header-only-no-inference")
    config = {"enabled": True, "backend": "local", "model_path": str(root)}
    monkeypatch.setattr(local_embedding, "_dependencies", lambda: {"torch": "offline-test"})
    def unexpected_process(*args, **kwargs):
        raise AssertionError("Read-only status and empty recall must not start a process")
    monkeypatch.setattr(local_embedding.subprocess, "Popen", unexpected_process)
    original_import = builtins.__import__
    def guarded_import(name, *args, **kwargs):
        if name.partition(".")[0] in {"torch", "transformers", "sentence_transformers"}:
            raise AssertionError("Read-only status and empty recall must not import the inference stack")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    store = Store(tmp_path / "runtime.db")
    manager = local_embedding.LocalEmbeddingManager()
    try:
        status = embedding_status(config, NoRemoteProviders(), manager)
        assert status["status"] == "ready" and status["local"]["state"] == "unloaded"
        result = await SemanticMemory(memory_view(store, tmp_path), NoRemoteProviders(), config,
                                      local_manager=manager).recall(query="ocean")
        assert result["no_data"] and result["indexed_count"] == 0
        assert manager.status(config)["state"] == "unloaded"
    finally:
        await manager.close()
        store.close()


@pytest.mark.parametrize("change", ["disabled", "remote", "path"])
async def test_management_embedding_change_releases_owned_worker(tmp_path, monkeypatch, change):
    from aster.memory import local_embedding
    monkeypatch.setattr(local_embedding, "_dependencies", lambda: {"torch": "offline-test"})
    config = {**model_directory(tmp_path), "enabled": True}
    script = tmp_path / "worker.py"
    script.write_text(FAKE_WORKER, encoding="utf-8")
    coordinator = RunCoordinator(tmp_path / "data", tools=[],
        local_embedding_factory=lambda: local_embedding.LocalEmbeddingManager(worker_script=script))
    (coordinator.data_dir / "config.yaml").write_text(yaml.safe_dump({"embedding": config}), encoding="utf-8")
    app = create_app(coordinator, token=TOKEN)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
            base_url="http://localhost", headers={"Authorization": "Bearer " + TOKEN}) as client:
        sid = coordinator.create_session(tmp_path)["id"]
        fact(memory_view(coordinator.store, coordinator.data_dir, agent="default", project=None), "ocean", "waves", scope="agent")
        assert not (await coordinator.execute_session_tool(sid, "memory_semantic_recall", {"query": "ocean"})).is_error
        previous = coordinator.local_embeddings
        process = previous._process
        assert process is not None and process.poll() is None
        state = (await client.get("/management")).json()
        updated = dict(state["extensions"]["embedding"])
        if change == "disabled":
            updated["enabled"] = False
        elif change == "remote":
            updated.update(backend="remote", enabled=False)
        else:
            copied = tmp_path / "second-model"
            shutil.copytree(config["model_path"], copied)
            updated["model_path"] = str(copied)
        response = await client.patch("/management", json={"expected_revision": state["revision"], "embedding": updated})
        assert response.status_code == 200
        assert process.poll() is not None and previous._closed
        assert coordinator.local_embeddings is not previous and coordinator.local_embeddings._process is None
        assert coordinator.store.connection.execute("SELECT count(*) FROM memory_facts").fetchone()[0] == 1


async def test_shutdown_waits_for_manual_local_worker_and_resource_cleanup(tmp_path, monkeypatch):
    from aster.memory import local_embedding
    monkeypatch.setattr(local_embedding, "_dependencies", lambda: {"torch": "offline-test"})
    config = {**model_directory(tmp_path), "enabled": True}
    entered = tmp_path / "worker-busy"
    script = tmp_path / "worker.py"
    script.write_text(FAKE_WORKER.replace("if texts[0]=='sleep': time.sleep(20)",
        f"if texts[0]=='sleep':\n            open({str(entered)!r}, 'w').write('busy')\n            time.sleep(20)"), encoding="utf-8")
    coordinator = RunCoordinator(tmp_path / "data", tools=[],
        local_embedding_factory=lambda: local_embedding.LocalEmbeddingManager(worker_script=script))
    (coordinator.data_dir / "config.yaml").write_text(yaml.safe_dump({"embedding": config}), encoding="utf-8")
    cleanup_entered, cleanup_release = asyncio.Event(), asyncio.Event()
    class Resource:
        async def close(self):
            cleanup_entered.set()
            await cleanup_release.wait()
            assert coordinator.store.connection.execute("SELECT 1").fetchone()[0] == 1
    build = coordinator._build_engine
    async def with_resource(*args, **kwargs):
        engine, services, resources = await build(*args, **kwargs)
        resources.append(Resource())
        return engine, services, resources
    monkeypatch.setattr(coordinator, "_build_engine", with_resource)
    sid = coordinator.create_session(tmp_path)["id"]
    fact(memory_view(coordinator.store, coordinator.data_dir, agent="default", project=None), "ocean", "waves", scope="agent")
    work = asyncio.create_task(coordinator.execute_session_tool(sid, "memory_semantic_recall", {"query": "sleep"}))
    closing = None
    try:
        async with asyncio.timeout(5):
            while not entered.exists():
                await asyncio.sleep(.02)
        process = coordinator.local_embeddings._process
        run_id = coordinator.store.list_runs(sid)[0]["id"]
        closing = asyncio.create_task(coordinator.close())
        await asyncio.wait_for(cleanup_entered.wait(), 5)
        assert not closing.done()
        assert coordinator.store.connection.execute("SELECT 1").fetchone()[0] == 1
        with pytest.raises(StateError, match="shutting down"):
            await coordinator.execute_session_tool(sid, "memory_semantic_recall", {"query": "ocean"})
        cleanup_release.set()
        result = await asyncio.wait_for(work, 5)
        await asyncio.wait_for(closing, 5)
        assert result.is_error and process.poll() is not None
        with pytest.raises(StateError, match="shutting down"):
            await coordinator.execute_session_tool(sid, "memory_semantic_recall", {"query": "ocean"})
        reopened = Store(coordinator.data_dir / "runtime.db")
        try:
            assert reopened.get_run(run_id)["status"] == "cancelled"
        finally:
            reopened.close()
    finally:
        cleanup_release.set()
        if not work.done():
            work.cancel()
        await asyncio.gather(work, return_exceptions=True)
        if closing is not None:
            await asyncio.gather(closing, return_exceptions=True)
        else:
            await coordinator.close()


async def test_switching_to_remote_stops_active_local_query_without_forwarding(tmp_path, monkeypatch):
    from aster.memory import local_embedding
    monkeypatch.setattr(local_embedding, "_dependencies", lambda: {"torch": "offline-test"})
    config = {**model_directory(tmp_path), "enabled": True}
    entered = tmp_path / "query-started"
    script = tmp_path / "worker.py"
    script.write_text(FAKE_WORKER.replace("if texts[0]=='sleep': time.sleep(20)",
        f"if texts[0]=='sleep':\n            open({str(entered)!r}, 'w').write('busy')\n            time.sleep(20)"), encoding="utf-8")
    coordinator = RunCoordinator(tmp_path / "data", tools=[],
        local_embedding_factory=lambda: local_embedding.LocalEmbeddingManager(worker_script=script))
    profile = coordinator.providers.create(name="remote fixture", base_url="https://fixture.example/v1",
                                          protocol="openai_chat", api_key="offline-should-not-send")
    requests = []
    coordinator.providers.client_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: requests.append(request) or httpx.Response(500)))
    (coordinator.data_dir / "config.yaml").write_text(yaml.safe_dump({"embedding": config}), encoding="utf-8")
    app = create_app(coordinator, token=TOKEN)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
            base_url="http://localhost", headers={"Authorization": "Bearer " + TOKEN}) as client:
        sid = coordinator.create_session(tmp_path)["id"]
        fact(memory_view(coordinator.store, coordinator.data_dir, agent="default", project=None), "ocean", "waves", scope="agent")
        query = asyncio.create_task(client.post(f"/customization/sessions/{sid}/memory_semantic_recall",
                                               json={"arguments": {"query": "sleep"}, "approved": False}))
        try:
            async with asyncio.timeout(5):
                while not entered.exists():
                    await asyncio.sleep(.02)
            previous = coordinator.local_embeddings
            process = previous._process
            state = (await client.get("/management")).json()
            remote = {"enabled": True, "backend": "remote", "provider_id": profile["id"], "model": "remote-embedding"}
            saved = await client.patch("/management", json={"expected_revision": state["revision"], "embedding": remote})
            assert saved.status_code == 200 and process.poll() is not None
            result = (await asyncio.wait_for(query, 5)).json()
            assert result["is_error"] and "configuration changed" in result["content"].lower()
            assert requests == [] and coordinator.local_embeddings is not previous
            assert not coordinator.store.has_unresolved(sid)
        finally:
            if not query.done():
                query.cancel()
            await asyncio.gather(query, return_exceptions=True)
