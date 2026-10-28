"""Expose the existing registry via MCP while delegating execution to policy/core."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from aster.domain import ToolResult


def create_mcp_server(registry, execute: Callable[[str, dict], Awaitable[ToolResult]], *, name="aster"):
    """execute MUST use the same durable dispatcher as local runs.

    It is deliberately required: exporting registry.get(...).execute directly
    would bypass approvals, journaling and cancellation.
    """
    from mcp import types
    from mcp.server import Server

    server = Server(name, version="0.1.0")

    @server.list_tools()
    async def list_tools():
        return [
            types.Tool(
                name=spec.name,
                description=spec.description,
                inputSchema=spec.parameters,
                annotations=types.ToolAnnotations(
                    readOnlyHint=spec.risk == "read", idempotentHint=spec.replay_safe
                ),
            )
            for spec in registry.specs()
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        if registry.get(name) is None:
            return types.CallToolResult(
                isError=True, content=[types.TextContent(type="text", text="Unknown tool")]
            )
        result = await execute(name, arguments)
        return types.CallToolResult(
            isError=result.is_error,
            content=[types.TextContent(type="text", text=result.content)],
            structuredContent={
                "outcome": result.outcome,
                "error_category": result.error_category,
                "data": result.data,
                "artifacts": result.artifacts,
            },
        )

    return server


async def serve_stdio(server):
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())
