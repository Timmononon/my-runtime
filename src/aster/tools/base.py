"""Structural tool protocol shared by built-in tools and MCP adapters."""
from typing import Protocol, runtime_checkable

from aster.domain import ToolContext, ToolResult, ToolSpec


# Implementations supply these members without inheriting this protocol.
# Runtime admission and journaling are enforced by Dispatcher before execution.
@runtime_checkable
class Tool(Protocol):
    spec: ToolSpec

    async def execute(self, arguments: dict, context: ToolContext) -> ToolResult: ...


__all__ = ["Tool", "ToolContext", "ToolResult", "ToolSpec"]
