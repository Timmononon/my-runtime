"""Plans, questions and local automation share the core services and policies."""

from __future__ import annotations

import json

from aster.customization.discovery import ToolSearchTool
from aster.customization.skills import UseSkillTool
from aster.domain import ToolContext, ToolResult, ToolSpec
from aster.memory import MemoryReadTool, MemoryWriteTool, SessionSearchTool


class SetWorkPlanTool:
    spec = ToolSpec(
        "set_work_plan",
        "Replace the task's complete work plan with a versioned snapshot",
        {
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 8,
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "minLength": 1},
                            "text": {"type": "string", "minLength": 1},
                            "status": {"enum": ["pending", "in_progress", "completed"]},
                        },
                        "required": ["id", "text", "status"],
                        "additionalProperties": False,
                    },
                },
                "expected_revision": {"type": "integer", "minimum": 0},
            },
            "required": ["steps"],
            "additionalProperties": False,
        },
        risk="read",
        resource="plan",
    )

    async def execute(self, arguments: dict, context: ToolContext) -> ToolResult:
        if context.cancel.is_set():
            return ToolResult("Cancelled", is_error=True, error_category="cancelled", outcome="not_executed")
        try:
            steps = arguments["steps"]
            if not isinstance(steps, list) or not 2 <= len(steps) <= 8:
                raise ValueError("Plan needs two to eight steps")
            ids = [step["id"] for step in steps]
            if len(set(ids)) != len(ids) or sum(step["status"] == "in_progress" for step in steps) > 1:
                raise ValueError("Step IDs must be unique and at most one step may be in progress")
            store = context.services["store"]
            run = store.get_run(context.run_id)
            # Model-reported step status never overrides the runtime's terminal state.
            snapshot = {
                "run_id": context.run_id,
                "steps": steps,
                "lifecycle": "completed" if run["status"] == "completed" else "active",
            }
            plan = store.set_plan(
                context.session_id,
                context.run_id,
                snapshot,
                expected_revision=arguments.get("expected_revision"),
            )
            return ToolResult(json.dumps(plan, ensure_ascii=False), data=plan)
        except (KeyError, ValueError, TypeError) as exc:
            return ToolResult(str(exc), is_error=True, error_category="validation", outcome="not_executed")


class AskUserQuestionTool:
    spec = ToolSpec(
        "ask_user_question",
        "Ask the user for missing information and wait for their answer",
        {
            "type": "object",
            "properties": {
                "question": {"type": "string", "minLength": 1},
                "options": {"type": "array", "minItems": 2, "maxItems": 6, "items": {"type": "string"}},
            },
            "required": ["question"],
            "additionalProperties": False,
        },
        risk="read",
        timeout_seconds=600,
    )

    async def execute(self, arguments: dict, context: ToolContext) -> ToolResult:
        broker = context.services.get("questions")
        if broker is None:
            return ToolResult(
                "No question channel is available",
                is_error=True,
                error_category="configuration",
                outcome="not_executed",
            )
        answer = await broker.ask(arguments, context)
        if answer is None:
            return ToolResult(
                "Question was cancelled, expired, or unavailable",
                is_error=True,
                error_category="question_unanswered",
                outcome="not_executed",
            )
        return ToolResult(json.dumps(answer, ensure_ascii=False), data={"answer": answer})


class ScheduleTool:
    def __init__(self, action: str):
        self.action = action
        common = {"job_id": {"type": "string", "minLength": 1}}
        if action == "create":
            fields = {
                "kind": {"enum": ["schedule", "watcher", "heartbeat"]},
                "prompt": {"type": "string", "minLength": 1},
                "config": {"type": "object"},
                "enabled": {"type": "boolean"},
            }
            required = ["kind", "prompt", "config"]
        elif action == "update":
            fields = {
                **common,
                "prompt": {"type": "string", "minLength": 1},
                "config": {"type": "object"},
                "enabled": {"type": "boolean"},
            }
            required = ["job_id"]
        elif action == "delete":
            fields, required = common, ["job_id"]
        else:
            fields, required = {"include_results": {"type": "boolean"}}, []
        self.spec = ToolSpec(
            "schedule_" + action,
            f"{action.capitalize()} local schedules, file watchers and heartbeat jobs",
            {"type": "object", "properties": fields, "required": required, "additionalProperties": False},
            risk="read" if action == "list" else "write",
            replay_safe=action == "list",
            resource="automation",
        )

    async def execute(self, arguments: dict, context: ToolContext) -> ToolResult:
        service = context.services.get("automation")
        if service is None:
            return ToolResult(
                "Automation service is unavailable",
                is_error=True,
                error_category="configuration",
                outcome="not_executed",
            )
        if context.cancel.is_set():
            return ToolResult("Cancelled", is_error=True, error_category="cancelled", outcome="not_executed")
        try:
            if self.action == "create":
                config = dict(arguments["config"])
                # Relative watcher/heartbeat paths use this run's CWD, never process CWD.
                from pathlib import Path

                if arguments["kind"] == "watcher" and config.get("path"):
                    config["path"] = str((context.cwd / Path(config["path"])).resolve())
                if arguments["kind"] == "heartbeat" and config.get("target", {}).get("kind") == "file":
                    config["target"] = {
                        **config["target"],
                        "path": str((context.cwd / Path(config["target"]["path"])).resolve()),
                    }
                value = service.create_job(
                    arguments["kind"],
                    context.session_id,
                    arguments["prompt"],
                    config,
                    enabled=arguments.get("enabled", True),
                )
            elif self.action == "list":
                value = {"jobs": service.list_jobs(context.session_id)}
                if arguments.get("include_results"):
                    value["results"] = [
                        receipt for job in value["jobs"] for receipt in service.receipts(job["id"])
                    ]
            else:
                job = service.get_job(arguments["job_id"])
                if job["session_id"] != context.session_id:
                    raise ValueError("Automation belongs to another session")
                if self.action == "delete":
                    service.delete_job(job["id"])
                    value = {"deleted": job["id"]}
                else:
                    value = service.update_job(
                        job["id"],
                        **{
                            key: arguments[key] for key in ("prompt", "config", "enabled") if key in arguments
                        },
                    )
            return ToolResult(json.dumps(value, ensure_ascii=False), data=value)
        except (KeyError, ValueError, TypeError, OSError) as exc:
            return ToolResult(str(exc), is_error=True, error_category="validation", outcome="not_executed")


def build_interaction_tools() -> list:
    """Return all extension tools once; callers should not register duplicates."""
    return [
        UseSkillTool(),
        ToolSearchTool(),
        MemoryReadTool(),
        MemoryWriteTool(),
        SessionSearchTool(),
        SetWorkPlanTool(),
        AskUserQuestionTool(),
        *(ScheduleTool(action) for action in ("create", "list", "update", "delete")),
    ]
