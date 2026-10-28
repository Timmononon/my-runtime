"""Stable, collision-rejecting tool catalogue."""
from __future__ import annotations

from jsonschema import Draft202012Validator

from aster.domain import ToolSpec
from aster.tools.base import Tool


class Registry:
    def __init__(self, tools: list[Tool] | None = None):
        self._tools: dict[str, Tool] = {}
        for tool in tools or []:
            self.register(tool)

    def register(self, tool: Tool) -> None:
        spec = tool.spec
        if not spec.name or spec.name in self._tools:
            raise ValueError(f"Empty or duplicate tool name: {spec.name!r}")
        if spec.timeout_seconds <= 0:
            raise ValueError("Tool timeout must be positive")
        Draft202012Validator.check_schema(spec.parameters)
        self._tools[spec.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def specs(self) -> list[ToolSpec]:
        return [tool.spec for tool in self.all_tools()]

    def all_tools(self) -> list[Tool]:
        return [self._tools[name] for name in sorted(self._tools)]
