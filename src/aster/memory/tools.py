"""Structured-memory tools use the ordinary Runtime approval and journal path."""
import json
from dataclasses import replace

from aster.domain import ToolResult, ToolSpec
from aster.providers.deepseek import ProviderError

TEXT = {"type": "string", "minLength": 1}
SCOPE = {"enum": ["global", "agent", "project"]}
SOURCES = {"type": "array", "minItems": 1, "maxItems": 20, "items": {"type": "object",
    "properties": {"kind": {"enum": ["message", "current_user", "markdown", "external", "user_supplied"]},
                   "quote": TEXT, "message_id": TEXT, "scope": SCOPE, "name": TEXT, "version": TEXT, "label": TEXT},
    "required": ["kind", "quote"], "additionalProperties": False}}


class FactTool:
    def __init__(self, name, description, properties, required=(), *, write=False):
        self.spec = ToolSpec(name, description, {"type": "object", "properties": properties,
            "required": list(required), "additionalProperties": False}, risk="write" if write else "read",
            replay_safe=not write, concurrency_safe=not write, resource="memory" if write else None)

    async def execute(self, arguments, context):
        if context.cancel.is_set():
            return ToolResult("Cancelled", True, "cancelled", "not_executed")
        try:
            memory = context.services["structured_memory"]
            args = dict(arguments)
            action = self.spec.name.removeprefix("memory_")
            if action in {"record", "update"}:
                args.update(session_id=context.session_id, run_id=context.run_id)
            if action == "update":
                action = "supersede"
            result = getattr(memory, action)(**args)
            return ToolResult(json.dumps(result, ensure_ascii=False), data={"memory": result})
        except (ValueError, KeyError, TypeError, OSError) as error:
            return ToolResult(str(error), True, "validation", "not_executed")


def build_memory_tools(embedding_config=None):
    dates = {"observed_at": TEXT, "valid_from": TEXT, "valid_to": TEXT}
    types = {"entity_type": TEXT, "target_type": TEXT}
    identity = {"fact_id": TEXT, "scope": SCOPE}
    update = {**identity, "expected_version": {"type": "integer", "minimum": 1}, "reason": TEXT}
    return [
        FactTool("memory_record", "Record an explicit sourced fact; never automatically infer memories",
                 {"entity": TEXT, "relation": TEXT, "value": {}, "sources": SOURCES, "scope": SCOPE, **dates, **types},
                 ("entity", "relation", "value", "sources"), write=True),
        FactTool("memory_recall", "Recall source-linked facts, current validity and explicit conflicts",
                 {"query": {"type": "string"}, "entity": TEXT, "relation": TEXT,
                  "scopes": {"type": "array", "items": SCOPE, "minItems": 1}, "at": TEXT,
                  "statuses": {"type": "array", "items": {"enum": ["current", "superseded", "retracted"]}},
                  "limit": {"type": "integer", "minimum": 1, "maximum": 200}}),
        FactTool("memory_update", "Supersede a fact using its observed version and new evidence",
                 {**update, "value": {}, "sources": SOURCES, **dates, **types},
                 ("fact_id", "expected_version", "value", "sources", "reason"), write=True),
        FactTool("memory_forget", "Retract a fact while preserving its audit and source history", update,
                 ("fact_id", "expected_version", "reason"), write=True),
        FactTool("memory_history", "Read the exact revisions and provenance of a scoped fact", identity, ("fact_id",)),
        FactTool("memory_query", "Query scoped direct relations, observed paths, explicit types and direct aggregation",
                 {"mode": {"enum": ["direct_relation", "path_query", "typed_neighborhood", "aggregate"]},
                  "entity": TEXT, "relation": TEXT, "path": {"type": "array", "minItems": 1, "maxItems": 32, "items": TEXT},
                  "candidate_type": TEXT, "aggregate": {"enum": ["count", "sum"]},
                  "scopes": {"type": "array", "minItems": 1, "items": SCOPE}, "at": TEXT,
                  "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                  "scan_limit": {"type": "integer", "minimum": 1, "maximum": 10000}}),
        SemanticRecallTool(embedding_config),
    ]


class SemanticRecallTool:
    spec = ToolSpec("memory_semantic_recall", "Explicitly send scoped effective facts to the configured embedding provider",
                    {"type": "object", "properties": {"query": TEXT,
                     "scopes": {"type": "array", "minItems": 1, "items": SCOPE}, "at": TEXT,
                     "limit": {"type": "integer", "minimum": 1, "maximum": 200}},
                     "required": ["query"], "additionalProperties": False}, risk="external", replay_safe=False,
                    resource="memory_embeddings")

    def __init__(self, embedding_config=None):
        from aster.memory.semantic import validate_embedding_config
        config = validate_embedding_config(embedding_config or {})
        if config["backend"] == "local":
            self.spec = replace(self.spec, description="Search scoped effective facts using the configured offline local embedding model",
                                risk="read", replay_safe=True, timeout_seconds=max(60, config["timeout_seconds"]))

    async def execute(self, arguments, context):
        if context.cancel.is_set():
            return ToolResult("Cancelled", True, "cancelled", "not_executed")
        try:
            result = await context.services["semantic_memory"].recall(**arguments, cancel=context.cancel)
            return ToolResult(json.dumps(result, ensure_ascii=False), data={"memory": result})
        except ProviderError as error:
            return ToolResult(str(error), True, error.category, "known")
        except (ValueError, KeyError, TypeError, OSError):
            return ToolResult("Semantic memory arguments or configuration are invalid", True, "validation", "not_executed")
