"""Repo-scoped GitHub reads, local drafts and explicit journaled publishing."""
from __future__ import annotations

import json

from aster.domain import ToolContext, ToolResult, ToolSpec
from aster.github.service import GithubSourceSelectionError
from aster.state import StateError


class GithubTool:
    def __init__(self, action):
        self.action = action
        source = {"source_id": {"type": "string", "minLength": 1, "maxLength": 200,
                  "description": "Internal configured source ID, not owner/repo or an Issue ID. "
                                 "Discover with github_read resource=sources. Omit only for a unique available source."}}
        if action == "read":
            fields = {**source, "resource": {"enum": ["sources", "repository", "issue", "pull_request", "comments", "reviews", "ci"]},
                      "number": {"type": "integer", "minimum": 1}}
            required, risk = ["resource"], "read"
            description = "Discover authorized GitHub source IDs with resource=sources, or read repository facts and discussions"
        elif action == "prepare":
            fields = {**source, "kind": {"enum": ["comment", "pull_request"]},
                      "number": {"type": "integer", "minimum": 1},
                      "ticket_id": {"type": "string", "minLength": 1, "maxLength": 200},
                      "body": {"type": "string", "minLength": 1, "maxLength": 100000},
                      "title": {"type": "string", "minLength": 1, "maxLength": 300},
                      "head": {"type": "string", "minLength": 1, "maxLength": 200},
                      "base": {"type": "string", "minLength": 1, "maxLength": 200}}
            required, risk = ["kind", "body"], "write"
            description = "Prepare a local GitHub comment or pull request draft for exact human review"
        else:
            fields = {"draft_id": {"type": "string", "minLength": 1, "maxLength": 200},
                      "expected_revision": {"type": "integer", "minimum": 0}}
            required, risk = ["draft_id", "expected_revision"], "external"
            description = "Publish one reviewed GitHub draft under its pinned identity and exact revision"
        self.spec = ToolSpec("github_" + action, description, {
            "type": "object", "properties": fields, "required": required, "additionalProperties": False,
        }, risk=risk, replay_safe=action == "read", timeout_seconds=60, resource="github" if action != "read" else None)

    async def execute(self, arguments: dict, context: ToolContext):
        if context.cancel.is_set():
            return ToolResult("Cancelled before GitHub dispatch", True, "cancelled", "not_executed")
        service = context.services.get("github")
        if service is None:
            return ToolResult("GitHub source service is unavailable", True, "configuration", "not_executed")
        try:
            task = context.services["tasks"].worker_binding(context.session_id)
            if self.action == "publish":
                draft = service.get_draft(arguments["draft_id"])
                source_id = draft["source_id"]
            else:
                draft = None
                if self.action == "read" and arguments["resource"] == "sources":
                    value = {"sources": service.tool_sources(context.session_id)}
                    if arguments.get("source_id") is not None:
                        selected = service.resolve_tool_source(context.session_id, arguments["source_id"])
                        value["sources"] = [row for row in value["sources"] if row["source_id"] == selected]
                    return ToolResult(json.dumps(value, ensure_ascii=False), data=value)
                source_id = service.resolve_tool_source(context.session_id, arguments.get("source_id"),
                                                       write=self.action != "read")
            service.scope_source(context.session_id, source_id, write=self.action != "read")
            if task and self.action != "read":
                requested_ticket = draft.get("ticket_id") if draft else arguments.get("ticket_id", task["ticket_id"])
                if requested_ticket != task["ticket_id"]:
                    raise PermissionError("A Worker can only write a draft for its bound task")
            if self.action == "read":
                value = await service.read(source_id, resource=arguments["resource"], number=arguments.get("number"))
                # The source may be changed while a read is awaiting its response.
                service.scope_source(context.session_id, source_id)
                return ToolResult(json.dumps(value, ensure_ascii=False), data=value)
            execution_id = context.services.get("tool_execution_id")
            entry = next((item for item in context.services["store"].tool_executions(context.run_id)
                          if item["id"] == execution_id and item["tool_name"] == self.spec.name
                          and item["state"] == "dispatching"), None)
            if entry is None:
                raise PermissionError("GitHub writes require a durable Runtime tool intent")
            if self.action == "publish":
                # Post-POST ambiguity belongs to the service's unknown result.
                # Never convert an uncertain mutation into known-no-effect.
                try:
                    return await service.publish(arguments["draft_id"], arguments["expected_revision"], context)
                except Exception:  # noqa: BLE001 - entering publishing cannot prove that no remote write happened.
                    return ToolResult("GitHub delivery could not be confirmed; reconcile its original receipt", True,
                                      "outcome_unknown", "unknown")
            fields = {key: value for key, value in arguments.items() if key != "source_id"}
            if task:
                fields["ticket_id"] = task["ticket_id"]
            value = service.create_draft(source_id, request_id="tool:" + execution_id, **fields)
            return ToolResult(json.dumps(value, ensure_ascii=False), data=value)
        except PermissionError:
            return ToolResult("GitHub source or draft is outside this conversation's authority", True, "permission", "not_executed")
        except GithubSourceSelectionError as error:
            return ToolResult(str(error), True, "validation", "not_executed", data={"reason": error.reason})
        except (ValueError, TypeError):
            return ToolResult("GitHub arguments are incomplete or invalid. Issue, pull request, comments and reviews "
                              "reads need a positive thread number. A comment draft needs a thread number; "
                              "a pull request draft needs title, head and base. Read the tool schema before retrying.",
                              True, "validation", "not_executed")
        except (KeyError, StateError):
            return ToolResult("GitHub action is unavailable or its reviewed state changed", True, "validation", "not_executed")


def build_github_tools():
    return [GithubTool(action) for action in ("read", "prepare", "publish")]
