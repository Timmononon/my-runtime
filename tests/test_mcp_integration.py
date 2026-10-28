import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from aster.domain import ToolContext
from aster.mcp import MCPClient, MCPServerConfig


def context(tmp_path):
    return ToolContext("s", "r", tmp_path, tmp_path / "artifacts", asyncio.Event())


@pytest.mark.asyncio
async def test_mcp_material_disconnect_is_never_replayed(tmp_path):
    state = {"connections": 0, "calls": 0}

    class Session:
        async def list_tools(self, cursor=None):
            return SimpleNamespace(
                tools=[{"name": "submit", "inputSchema": {"type": "object"}}], nextCursor=None
            )

        async def call_tool(self, *args, **kwargs):
            state["calls"] += 1
            raise ConnectionError("after dispatch")

    @asynccontextmanager
    async def connector(config):
        state["connections"] += 1
        yield Session()

    async with MCPClient(
        [MCPServerConfig("test", command="unused", connect_attempts=1)], connector=connector
    ) as client:
        tool = (await client.list_tools())[0]
        result = await tool.execute({}, context(tmp_path))
        assert result.is_error and result.outcome == "unknown"
        assert state["calls"] == 1
        await client.list_tools()
        assert state["connections"] == 2
        assert state["calls"] == 1


@pytest.mark.asyncio
async def test_replacement_invalidates_old_tool(tmp_path):
    class Session:
        async def list_tools(self, cursor=None):
            return SimpleNamespace(tools=[{"name": "read", "inputSchema": {}}], nextCursor=None)

    @asynccontextmanager
    async def connector(config):
        yield Session()

    config = MCPServerConfig("test", command="unused")
    async with MCPClient([config], connector=connector) as client:
        tool = (await client.list_tools())[0]
        await client.replace_server(config)
        result = await tool.execute({}, context(tmp_path))
        assert result.is_error and result.outcome == "known"
        assert not result.data["dispatched"]


@pytest.mark.asyncio
async def test_real_stdio_mcp_bridge_policy_and_roundtrip(tmp_path):
    script = tmp_path / "server.py"
    script.write_text(
        """import asyncio
from aster.domain import ToolSpec,ToolResult
from aster.mcp.server import create_mcp_server,serve_stdio
class Registry:
    def specs(self):
        return [ToolSpec("echo","Echo a value",{"type":"object","properties":{"value":{"type":"string"}},"required":["value"]})]
    def get(self,name):
        return name == "echo"
async def governed(name,args):
    if args["value"] == "blocked":
        return ToolResult("denied by policy",is_error=True,error_category="permission")
    return ToolResult("echo:"+args["value"])
asyncio.run(serve_stdio(create_mcp_server(Registry(),governed)))
""",
        encoding="utf-8",
    )
    source = str(Path(__file__).resolve().parents[1] / "src")
    config = MCPServerConfig(
        "local",
        command=sys.executable,
        args=(str(script),),
        cwd=tmp_path,
        env={"PYTHONPATH": source, "PYTHONIOENCODING": "utf-8"},
        timeout_seconds=10,
        connect_attempts=1,
        trust_annotations=True,
    )
    async with MCPClient([config]) as client:
        tools = await client.list_tools()
        assert len(tools) == 1
        result = await tools[0].execute({"value": "中文"}, context(tmp_path))
        assert result.content == "echo:中文" and not result.is_error
        denied = await tools[0].execute({"value": "blocked"}, context(tmp_path))
        assert denied.is_error and "denied" in denied.content
