"""Explicit tool authorization; this is not an OS sandbox."""
from dataclasses import dataclass, field

from aster.domain import ToolCall, ToolContext, ToolSpec


@dataclass(frozen=True)
class PermissionDecision:
    action: str
    reason: str


@dataclass
class PermissionPolicy:
    allowed_tools: set[str] = field(default_factory=set)
    denied_tools: set[str] = field(default_factory=set)
    always_ask: set[str] = field(default_factory=set)
    allowed_risks: set[str] = field(default_factory=lambda: {"read"})

    def evaluate(self, spec: ToolSpec, call: ToolCall, context: ToolContext) -> PermissionDecision:
        if spec.risk not in {"read", "write", "execute", "external"}:
            return PermissionDecision("deny", "Tool risk classification is unknown")
        if spec.name in self.denied_tools:
            return PermissionDecision("deny", "Tool is explicitly denied")
        scope = context.services.get("skill_scope")
        if scope is not None and not scope.check_tool(spec.name):
            return PermissionDecision("deny", "Active skill restricts this tool")
        if spec.name in self.always_ask:
            return PermissionDecision("ask", "Tool requires approval on every invocation")
        if spec.name in self.allowed_tools or spec.risk in self.allowed_risks:
            return PermissionDecision("allow", "Allowed by configured policy")
        return PermissionDecision("ask", f"Approval required for {spec.risk} operation")
