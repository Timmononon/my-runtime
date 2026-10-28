"""Offline unavailable/recovered catalogs and generation-safe material draining."""

import asyncio
import json
from collections import Counter
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
import yaml

from aster.config import load_config
from aster.domain import ModelTurn, ToolCall, ToolContext
from aster.engine import FakeModel
from aster.mcp import MCPClient, MCPServerConfig
from aster.service import RunCoordinator

DEFINITION = {"name": "read", "inputSchema": {"type": "object"},
              "annotations": {"readOnlyHint": True}}


class ReadSession:
    async def list_tools(self, cursor=None):
        return SimpleNamespace(tools=[DEFINITION], nextCursor=None)

    async def call_tool(self, *args, **kwargs):
        return {"content": [{"type": "text", "text": "healthy MCP response"}]}


def config(name, **values):
    return MCPServerConfig(name, command="offline-fixture", connect_attempts=1,
                           timeout_seconds=0.2, discovery_timeout_seconds=0.02,
                           reconnect_initial_seconds=0.02, reconnect_max_seconds=0.04,
                           **values)


def context(root):
    return ToolContext("s", "r", root, root / "artifacts", asyncio.Event())


async def wait_status(client, name, status):
    async with asyncio.timeout(2):
        while next(row for row in client.health() if row["name"] == name)["status"] != status:
            await asyncio.sleep(0.005)


@pytest.mark.asyncio
async def test_optional_failure_preserves_local_model_and_healthy_catalog(tmp_path, monkeypatch):
    attempts = Counter()
    available = asyncio.Event()

    @asynccontextmanager
    async def connector(server, **kwargs):
        attempts[server.name] += 1
        if server.name == "broken" and not available.is_set():
            raise ConnectionError("transport contained private fixture data")
        yield ReadSession()

    monkeypatch.setattr("aster.mcp.client.sdk_connection", connector)
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"mcp_servers": [
        {"name": name, "command": "offline-fixture", "connect_attempts": 1,
         "timeout_seconds": 0.2, "reconnect_initial_seconds": 0.02,
         "reconnect_max_seconds": 0.04, "discovery_timeout_seconds": 0.02,
         "trust_annotations": True} for name in ("broken", "healthy")]}), encoding="utf-8")
    model = FakeModel([ModelTurn("ordinary local response")])
    coordinator = RunCoordinator(tmp_path, tools=[], model_factory=lambda _: model)
    try:
        session = coordinator.create_session(tmp_path)
        run = await coordinator.submit(session["id"], "say hello without unavailable services")
        outcome = await coordinator.wait(run["id"], timeout=2)
        assert outcome.status == "completed" and len(model.calls) == 1
        health = {row["name"]: row for row in await coordinator.mcp_health()}
        assert health["healthy"]["status"] == "healthy" and health["healthy"]["tool_count"] == 1
        assert health["broken"]["error_category"] == "mcp_transport"
        assert "private fixture" not in json.dumps(health)
        available.set()
        client = next(iter(coordinator._mcp_clients.values()))
        await wait_status(client, "broken", "healthy")
        # Recovery did not require a new model run or retry the completed run.
        assert len(model.calls) == 1 and attempts["broken"] >= 2
        tool = next(tool for tool in await client.list_tools() if tool.connection.config.name == "broken")
        second = FakeModel([ModelTurn(tool_calls=[ToolCall("read-after-recovery", tool.spec.name, {})]),
                            ModelTurn("recovered response")])
        coordinator.model_factory = lambda _: second
        recovered = await coordinator.submit(session["id"], "use the recovered service")
        assert (await coordinator.wait(recovered["id"], timeout=2)).status == "completed"
        assert any("healthy MCP response" in message.content
                   for message in coordinator.store.history(session["id"]) if message.role == "tool")
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_manual_config_removal_revokes_clones_and_stops_pending_recovery(tmp_path, monkeypatch):
    attempts = Counter()

    @asynccontextmanager
    async def connector(server, **kwargs):
        attempts[server.name] += 1
        if server.name == "offline":
            raise ConnectionError("unavailable")
        yield ReadSession()

    monkeypatch.setattr("aster.mcp.client.sdk_connection", connector)
    values = {"mcp_servers": [{"name": name, "command": "offline-fixture", "connect_attempts": 1,
                               "reconnect_initial_seconds": 0.02, "reconnect_max_seconds": 0.02}
                              for name in ("available", "offline")]}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(values), encoding="utf-8")
    coordinator = RunCoordinator(tmp_path, tools=[], model_factory=lambda _: FakeModel([]))
    try:
        client = await coordinator._mcp_client_for(values)
        tool = (await client.list_tools())[0]
        before = attempts.copy()
        (tmp_path / "config.yaml").write_text("mcp_servers: []\n", encoding="utf-8")
        denied = await tool.execute({}, context(tmp_path))
        assert denied.is_error and not denied.data["dispatched"]
        await asyncio.sleep(0.07)
        assert attempts == before and not coordinator._mcp_clients
        assert all(row["status"] == "closed" for row in client.health())
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_agent_scoped_configuration_replacement_keeps_other_agent_pool(tmp_path, monkeypatch):
    @asynccontextmanager
    async def connector(server, **kwargs):
        yield ReadSession()

    monkeypatch.setattr("aster.mcp.client.sdk_connection", connector)
    (tmp_path / "config.yaml").write_text("mcp_servers:\n- name: global\n  command: offline-fixture\n", encoding="utf-8")
    alice = tmp_path / "agents" / "alice"
    alice.mkdir(parents=True)
    (alice / "config.yaml").write_text("mcp_servers:\n- name: alice\n  command: offline-fixture\n", encoding="utf-8")
    coordinator = RunCoordinator(tmp_path, tools=[], model_factory=lambda _: FakeModel([]))
    try:
        clients = {}
        for agent in ("alice", "bob"):
            settings = load_config(tmp_path, agent_id=agent, cwd=tmp_path).data
            clients[agent] = await coordinator._mcp_client_for(settings, scope=(agent, None, str(tmp_path)))
        alice_tool = (await clients["alice"].list_tools())[0]
        bob_tool = (await clients["bob"].list_tools())[0]
        (alice / "config.yaml").write_text("mcp_servers: []\n", encoding="utf-8")
        denied = await alice_tool.execute({}, context(tmp_path))
        assert denied.is_error and not denied.data["dispatched"]
        good = await bob_tool.execute({}, context(tmp_path))
        assert not good.is_error and good.content == "healthy MCP response"
        assert not bob_tool.connection.closed
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_required_failure_blocks_run_and_remains_diagnosable(tmp_path, monkeypatch):
    @asynccontextmanager
    async def connector(server, **kwargs):
        raise ConnectionError("private failure payload")
        yield  # pragma: no cover

    monkeypatch.setattr("aster.mcp.client.sdk_connection", connector)
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"mcp_servers": [{
        "name": "mandatory", "command": "unused", "required": True,
        "connect_attempts": 1, "timeout_seconds": 0.1, "reconnect_attempts": 0}]}), encoding="utf-8")
    model = FakeModel([ModelTurn("must not run")])
    coordinator = RunCoordinator(tmp_path, tools=[], model_factory=lambda _: model)
    try:
        run = await coordinator.submit(coordinator.create_session(tmp_path)["id"], "requires mandatory MCP")
        outcome = await coordinator.wait(run["id"], timeout=2)
        assert outcome.status == "failed" and not model.calls
        health = await coordinator.mcp_health()
        assert health[0]["required"] and health[0]["status"] == "exhausted"
        assert "private failure" not in json.dumps(health)
    finally:
        await coordinator.close()


@pytest.mark.asyncio
async def test_background_reconnect_is_bounded_and_manual_reconnect_rearms(tmp_path):
    attempts = 0
    available = False

    @asynccontextmanager
    async def connector(server):
        nonlocal attempts
        attempts += 1
        if not available:
            raise ConnectionError("offline")
        yield ReadSession()

    async with MCPClient([config("recover", reconnect_attempts=2)], connector=connector) as client:
        assert await client.list_tools(admission=True) == []
        await wait_status(client, "recover", "exhausted")
        assert attempts == 3  # Initial catalog plus exactly two recovery rounds.
        await asyncio.sleep(0.06)
        assert attempts == 3 and client.health()[0]["next_retry_at"] is None
        available = True
        health = await client.reconnect("recover")
        assert health[0]["generation"] == 2 and health[0]["status"] == "healthy"
        assert health[0]["reconnect_attempts"] == 0


@pytest.mark.asyncio
async def test_shutdown_during_slow_connect_stops_recovery_and_drains_owner(tmp_path):
    attempts = 0
    entered = asyncio.Event()
    exited = asyncio.Event()

    @asynccontextmanager
    async def connector(server):
        nonlocal attempts
        attempts += 1
        entered.set()
        try:
            await asyncio.sleep(0.08)
            raise ConnectionError("offline")
            yield  # pragma: no cover
        finally:
            exited.set()

    client = MCPClient([config("slow")], connector=connector)
    assert await client.list_tools(admission=True) == []
    assert entered.is_set() and not exited.is_set()
    await asyncio.wait_for(client.close(), 1)
    assert exited.is_set() and not client.connections["slow"].connected
    await asyncio.sleep(0.08)
    assert attempts == 1
    assert client.health()[0]["status"] == "closed"


@pytest.mark.asyncio
async def test_replacement_revokes_new_dispatch_but_drains_material_call(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    class WriteSession(ReadSession):
        async def list_tools(self, cursor=None):
            return SimpleNamespace(tools=[{"name": "write", "inputSchema": {"type": "object"}}],
                                   nextCursor=None)

        async def call_tool(self, *args, **kwargs):
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return {"content": [{"type": "text", "text": "write settled"}]}

    @asynccontextmanager
    async def connector(server):
        yield WriteSession()

    async with MCPClient([config("writer")], connector=connector) as client:
        tool = (await client.list_tools())[0]
        material = asyncio.create_task(tool.execute({}, context(tmp_path)))
        await entered.wait()
        replacement = asyncio.create_task(client.replace_server(config("writer")))
        await asyncio.sleep(0)
        assert tool.connection.closed and not replacement.done()
        denied = await tool.execute({}, context(tmp_path))
        assert denied.is_error and denied.outcome == "known" and not denied.data["dispatched"]
        release.set()
        result = await material
        await replacement
        assert result.content == "write settled" and not result.is_error and calls == 1
        assert client.health()[0]["generation"] == 2


@pytest.mark.asyncio
async def test_unknown_material_call_recovers_catalog_without_repeating_write(tmp_path):
    writes, connections = 0, 0

    class WriteSession(ReadSession):
        async def list_tools(self, cursor=None):
            return SimpleNamespace(tools=[{"name": "write", "inputSchema": {"type": "object"}}],
                                   nextCursor=None)

        async def call_tool(self, *args, **kwargs):
            nonlocal writes
            writes += 1
            raise ConnectionError("write may have occurred")

    @asynccontextmanager
    async def connector(server):
        nonlocal connections
        connections += 1
        yield WriteSession()

    async with MCPClient([config("unknown")], connector=connector) as client:
        tool = (await client.list_tools())[0]
        result = await tool.execute({}, context(tmp_path))
        assert result.outcome == "unknown" and result.is_error
        await wait_status(client, "unknown", "healthy")
        assert connections == 2 and writes == 1
        assert client.health()[0]["tool_count"] == 1


@pytest.mark.asyncio
async def test_file_credential_rotation_invalidates_old_transport_without_echo(tmp_path, monkeypatch):
    @asynccontextmanager
    async def connector(server, **kwargs):
        yield ReadSession()

    monkeypatch.setattr("aster.mcp.client.sdk_connection", connector)
    coordinator = RunCoordinator(tmp_path, tools=[], model_factory=lambda _: FakeModel([]))
    settings = {"mcp_servers": [{"name": "bound", "command": "offline-fixture",
                                 "env": {"ACCESS_TOKEN": "keyring:aster/mcp-account"}}]}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(settings), encoding="utf-8")
    try:
        coordinator.credentials.set("mcp-account", "fixture-before-rotation")
        first = await coordinator._mcp_client_for(settings)
        tool = (await first.list_tools())[0]
        coordinator.credentials.set("mcp-account", "fixture-after-rotation")
        second = await coordinator._mcp_client_for(settings)
        assert first is not second and tool.connection.closed
        result = await tool.execute({}, context(tmp_path))
        assert result.is_error and not result.data["dispatched"]
        assert "fixture-" not in json.dumps(second.health())
        await coordinator.invalidate_mcp()
        assert not coordinator._mcp_clients and second.health()[0]["status"] == "closed"
    finally:
        await coordinator.close()
