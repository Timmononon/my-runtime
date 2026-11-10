"""Task delegation uses real Runtime intent identities and session scope."""
from __future__ import annotations

import json

from aster.domain import ToolContext, ToolResult, ToolSpec
from aster.state import StateError

_WORKER = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "minLength": 1, "maxLength": 200},
        "goal": {"type": "string", "minLength": 1, "maxLength": 100000},
        "agent_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "role": {"enum": ["worker", "reader"]},
    },
    "required": ["title", "goal"],
    "additionalProperties": False,
}


class TaskTool:
    def __init__(self, action):
        self.action = action
        common = {"ticket_id": {"type": "string", "minLength": 1, "maxLength": 200}}
        if action == "delegate":
            fields = {
                "title": {"type": "string", "minLength": 1, "maxLength": 200},
                "goal": {"type": "string", "minLength": 1, "maxLength": 100000},
                "workers": {"type": "array", "minItems": 1, "maxItems": 32, "items": _WORKER},
            }
            required, risk = ["title", "goal"], "execute"
            description = "Delegate disjoint scoped work to independent Workers and persist a task summary"
        elif action == "list":
            fields, required, risk = {}, [], "read"
            description = "List local tasks belonging to this conversation without waiting for Workers"
        elif action == "get":
            fields, required, risk = common, ["ticket_id"], "read"
            description = "Read a local task, Worker results and items needing human attention"
        else:
            fields, required, risk = common, ["ticket_id"], "write"
            description = "Cancel a local task and stop its Workers through Runtime cancellation"
        self.spec = ToolSpec(
            "task_" + action, description,
            {"type": "object", "properties": fields, "required": required, "additionalProperties": False},
            risk=risk, replay_safe=risk == "read", resource="local_tasks" if risk != "read" else None,
        )

    async def execute(self, arguments: dict, context: ToolContext) -> ToolResult:
        if context.cancel.is_set():
            return ToolResult("Cancelled", True, "cancelled", "not_executed")
        service = context.services.get("tasks")
        if service is None:
            return ToolResult("Local task service is unavailable", True, "configuration", "not_executed")
        try:
            binding = service.worker_binding(context.session_id)
            if self.action == "delegate":
                if binding:
                    # A Worker may inspect its task but cannot create another delegation layer.
                    # New worker admission remains with the source conversation and its ledger.
                    raise PermissionError("Workers cannot recursively delegate tasks")
                execution_id = context.services.get("tool_execution_id")
                store = context.services["store"]
                entry = next((item for item in store.tool_executions(context.run_id)
                              if item["id"] == execution_id and item["tool_name"] == self.spec.name
                              and item["state"] == "dispatching"), None)
                if not entry:
                    raise PermissionError("Delegation requires a durable Runtime tool intent")
                registry = context.services.get("registry")
                scope = context.services.get("skill_scope")
                if registry is None:
                    raise PermissionError("Delegation requires the originating tool registry")
                allowance = [spec.name for spec in registry.specs()
                             if scope is None or scope.check_tool(spec.name)]
                value = service.create_ticket(
                    context.session_id, arguments["title"], arguments["goal"],
                    workers=arguments.get("workers"), request_id="tool:" + execution_id,
                    parent_run_id=context.run_id, unattended=bool(context.services.get("unattended", False)),
                    tool_allowance=allowance,
                )
            elif self.action == "list":
                value = {"tickets": [service.get_ticket(binding["ticket_id"])] if binding
                         else service.list_tickets(source_session_id=context.session_id)}
            else:
                value = service.get_ticket(arguments["ticket_id"])
                own = (value["id"] == binding["ticket_id"] if binding else
                       value["source_session_id"] == context.session_id)
                if not own:
                    raise PermissionError("Task belongs to another conversation")
                if self.action == "cancel":
                    if binding:
                        raise PermissionError("A Worker cannot cancel its supervising task")
                    value = await service.cancel_ticket(value["id"])
            return ToolResult(json.dumps(value, ensure_ascii=False), data=value)
        except PermissionError as error:
            return ToolResult(str(error), True, "permission", "not_executed")
        except (KeyError, ValueError, TypeError, StateError) as error:
            return ToolResult(str(error), True, "validation", "not_executed")


def build_task_tools():
    return [TaskTool(action) for action in ("delegate", "list", "get", "cancel")]
