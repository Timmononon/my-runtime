"""Scoped, editable Markdown memories and access to the core history index."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path

from aster.config import safe_identifier
from aster.domain import ToolContext, ToolResult, ToolSpec

_write_lock = threading.Lock()


class MemoryService:
    def __init__(self, root: Path, *, agent_id: str, project_id: str | None = None, history_search=None):
        self.root = Path(root).resolve()
        self.agent_id = safe_identifier(agent_id)
        self.project_id = safe_identifier(project_id) if project_id is not None else None
        self.history_search = history_search

    def _directory(self, scope: str) -> Path:
        if scope == "global":
            directory = self.root / "global"
        elif scope == "agent":
            directory = self.root / "agents" / self.agent_id
        elif scope == "project" and self.project_id:
            directory = self.root / "projects" / self.project_id
        else:
            raise ValueError("Scope must be global, current agent, or current project")
        if not directory.resolve().is_relative_to(self.root):
            raise ValueError("Memory scope escapes root")
        return directory

    def _path(self, scope: str, name: str) -> Path:
        path = self._directory(scope) / (safe_identifier(name) + ".md")
        if not path.resolve().is_relative_to(self.root):
            raise ValueError("Memory path escapes root")
        return path

    def read(self, scope: str = "agent", name: str = "MEMORY") -> dict:
        path = self._path(scope, name)
        if not path.exists():
            return {"scope": scope, "name": name, "content": "", "version": None, "exists": False}
        if path.stat().st_size > 1024 * 1024:
            raise ValueError("Memory exceeds read budget")
        content = path.read_text(encoding="utf-8")
        return {
            "scope": scope,
            "name": name,
            "content": content,
            "version": hashlib.sha256(content.encode()).hexdigest(),
            "exists": True,
        }

    def write(
        self, scope: str, name: str, content: str, *, source: str, expected_version: str | None = None
    ) -> dict:
        if not source.strip() or not isinstance(content, str) or len(content.encode()) > 1024 * 1024:
            raise ValueError("Memory requires a source and bounded text content")
        path = self._path(scope, name)
        with _write_lock:
            previous = self.read(scope, name)
            # Existing notes always require an observed version; first creation uses None.
            if previous["version"] != expected_version:
                raise ValueError("Memory changed; read its current version before writing")
            path.parent.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now(UTC).isoformat()
            header = (
                f"<!-- aster-memory source={json.dumps(source, ensure_ascii=False)} updated={timestamp} -->\n"
            )
            complete = header + content
            revision = path.parent / ".history" / name
            revision.mkdir(parents=True, exist_ok=True)
            if previous["exists"]:
                old = revision / (previous["version"] + ".md")
                if not old.exists():
                    old.write_text(previous["content"], encoding="utf-8")
            descriptor, temporary = tempfile.mkstemp(prefix=".memory-", dir=path.parent)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                    stream.write(complete)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return self.read(scope, name)

    def search(
        self, query: str, *, scopes: tuple[str, ...] = ("global", "agent", "project"), limit: int = 20
    ) -> list[dict]:
        if not query.strip() or not 1 <= limit <= 100:
            return []
        matches = []
        for scope in scopes:
            if scope == "project" and self.project_id is None:
                continue
            directory = self._directory(scope)
            for path in sorted(directory.glob("*.md")):
                note = self.read(scope, path.stem)
                for number, line in enumerate(note["content"].splitlines(), 1):
                    if query.casefold() in line.casefold():
                        matches.append(
                            {
                                "scope": scope,
                                "name": path.stem,
                                "line": number,
                                "content": line[:1000],
                                "version": note["version"],
                            }
                        )
                        if len(matches) >= limit:
                            return matches
        return matches

    async def search_history(self, query: str, limit: int = 20) -> list[dict]:
        if self.history_search is None:
            raise ValueError("History search service is unavailable")
        if not query.strip() or not 1 <= limit <= 100:
            raise ValueError("Query and limit are invalid")
        result = self.history_search(query, agent_id=self.agent_id, project_id=self.project_id, limit=limit)
        return await result if inspect.isawaitable(result) else result


class MemoryReadTool:
    spec = ToolSpec(
        "memory_read",
        "Read a scoped Markdown memory with its edit version",
        {
            "type": "object",
            "properties": {
                "scope": {"enum": ["global", "agent", "project"]},
                "name": {"type": "string"},
                "query": {"type": "string"},
            },
            "additionalProperties": False,
        },
        risk="read",
        replay_safe=True,
        concurrency_safe=True,
    )

    async def execute(self, arguments: dict, context: ToolContext) -> ToolResult:
        try:
            service: MemoryService = context.services["memory"]
            scope, name = arguments.get("scope", "agent"), arguments.get("name", "MEMORY")
            data = (
                service.search(arguments["query"], scopes=(scope,))
                if arguments.get("query")
                else service.read(scope, name)
            )
            return ToolResult(json.dumps(data, ensure_ascii=False), data={"memory": data})
        except (KeyError, ValueError, OSError) as exc:
            return ToolResult(str(exc), is_error=True, error_category="validation")


class MemoryWriteTool:
    spec = ToolSpec(
        "memory_write",
        "Write a sourced memory, checking the version previously read",
        {
            "type": "object",
            "properties": {
                "scope": {"enum": ["global", "agent", "project"]},
                "name": {"type": "string"},
                "content": {"type": "string"},
                "source": {"type": "string", "minLength": 1},
                "expected_version": {"type": ["string", "null"]},
            },
            "required": ["scope", "name", "content", "source"],
            "additionalProperties": False,
        },
        risk="write",
        resource="memory",
    )

    async def execute(self, arguments: dict, context: ToolContext) -> ToolResult:
        if context.cancel.is_set():
            return ToolResult("Cancelled", is_error=True, error_category="cancelled")
        try:
            service: MemoryService = context.services["memory"]
            data = service.write(
                arguments["scope"],
                arguments["name"],
                arguments["content"],
                source=arguments["source"],
                expected_version=arguments.get("expected_version"),
            )
            return ToolResult("Memory saved", data=data)
        except (KeyError, ValueError, OSError) as exc:
            return ToolResult(str(exc), is_error=True, error_category="validation")


class SessionSearchTool:
    spec = ToolSpec(
        "session_search",
        "Search original conversation evidence in the current Agent and project",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        risk="read",
        replay_safe=True,
    )

    async def execute(self, arguments: dict, context: ToolContext) -> ToolResult:
        try:
            service: MemoryService = context.services["memory"]
            results = await service.search_history(arguments["query"], arguments.get("limit", 20))
            return ToolResult(
                json.dumps({"results": results, "no_data": not results}, ensure_ascii=False),
                data={"results": results},
            )
        except (KeyError, ValueError) as exc:
            return ToolResult(str(exc), is_error=True, error_category="configuration")
