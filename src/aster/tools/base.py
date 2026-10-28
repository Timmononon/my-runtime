"""Structural tool protocol shared by built-in tools and MCP adapters."""
from typing import Protocol, runtime_checkable

from aster.domain import ToolContext, ToolResult, ToolSpec


@runtime_checkable
class Tool(Protocol):
    spec: ToolSpec

    async def execute(self, arguments: dict, context: ToolContext) -> ToolResult: ...


__all__ = ["Tool", "ToolContext", "ToolResult", "ToolSpec"]
