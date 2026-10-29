from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import json
import math
import os
import re
import time
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit

from aster.config import safe_identifier
from aster.domain import ToolContext, ToolResult, ToolSpec
from aster.providers.deepseek import _cancellable
from aster.providers.images import MAX_SOURCE_IMAGE_BYTES, prepare_model_image


@dataclass(frozen=True)
class MCPServerConfig:
    name: str
    command: str | None = None
    args: tuple[str, ...] = ()
    cwd: Path | None = None
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = 60
    connect_attempts: int = 3
    trust_annotations: bool = False
    required: bool = False
    # Optional catalogs get a short admission budget so an unavailable extension
    # cannot stall ordinary model/local-tool work; operators can raise this value.
    discovery_timeout_seconds: float = 2
    # Failed MCP services recover independently, then stop after this many rounds;
    # a manual reconnect or replacement resets the operator-visible exhausted state.
    reconnect_attempts: int = 6
    reconnect_initial_seconds: float = 5
    reconnect_max_seconds: float = 300

    def __post_init__(self):
        safe_identifier(self.name)
        if bool(self.command) == bool(self.url):
            raise ValueError("Configure exactly one MCP stdio command or HTTP URL")
        if self.url:
            endpoint = urlsplit(self.url)
            if (
                endpoint.scheme not in {"http", "https"}
                or not endpoint.hostname
                or endpoint.username
                or endpoint.password
                or endpoint.query
                or endpoint.fragment
            ):
                raise ValueError("MCP URL must not contain credentials, query parameters or a fragment")
            if endpoint.scheme == "http" and endpoint.hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise ValueError("Remote MCP endpoints require HTTPS")
        timings = (self.timeout_seconds, self.discovery_timeout_seconds,
                   self.reconnect_initial_seconds, self.reconnect_max_seconds)
        if any(not math.isfinite(value) or value <= 0 for value in timings):
            raise ValueError("MCP timeouts and reconnect delays must be finite and positive")
        if self.connect_attempts < 1 or self.reconnect_attempts < 0:
            raise ValueError("MCP timeout and connection attempts must be positive")
        if not isinstance(self.required, bool) or not isinstance(self.trust_annotations, bool):
            raise TypeError("MCP required and trust_annotations must be booleans")


class MCPTransportError(RuntimeError):
    def __init__(self, message: str, *, dispatched: bool):
        super().__init__(message)
        self.dispatched = dispatched


@asynccontextmanager
async def sdk_connection(config: MCPServerConfig, *, secret_resolver=None):
    """Transport/session contexts enter and exit in their single owner task."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from aster.customization.credentials import resolve_environment

    async with AsyncExitStack() as stack:
        if config.command:
            params = StdioServerParameters(
                command=config.command,
                args=list(config.args),
                env=resolve_environment(config.env, resolver=secret_resolver),
                cwd=str(config.cwd) if config.cwd else None,
            )
            if os.name == "nt":
                from .stdio import managed_windows_stdio

                streams = await stack.enter_async_context(managed_windows_stdio(params))
            else:
                streams = await stack.enter_async_context(stdio_client(params))
            read, write = streams
        else:
            import httpx
            from mcp.client.streamable_http import streamable_http_client

            http = await stack.enter_async_context(
                httpx.AsyncClient(
                    headers=resolve_environment(config.headers, resolver=secret_resolver),
                    timeout=config.timeout_seconds,
                    follow_redirects=False,
                )
            )
            streams = await stack.enter_async_context(streamable_http_client(config.url, http_client=http))
            read, write = streams[:2]
        session = await stack.enter_async_context(
            ClientSession(read, write, read_timeout_seconds=timedelta(seconds=config.timeout_seconds))
        )
        await session.initialize()
        yield session


@dataclass
class _Request:
    method: str
    name: str | None
    arguments: dict
    future: asyncio.Future
    cancel: asyncio.Event | None


class _Connection:
    def __init__(self, config: MCPServerConfig, connector, *, on_failure=None, binding_guard=None):
        self.config = copy.deepcopy(config)
        self.connector = connector
        self.queue: asyncio.Queue[_Request | None] = asyncio.Queue()
        self.task: asyncio.Task | None = None
        self.closed = False
        self.connected = False
        self.on_failure = on_failure
        self.binding_guard = binding_guard

    def _valid(self):
        return not self.closed and (self.binding_guard is None or self.binding_guard())

    def _failed(self):
        self.connected = False
        if self.on_failure is not None and not self.closed:
            self.on_failure(self)

    async def request(
        self,
        method: str,
        *,
        name: str | None = None,
        arguments: dict | None = None,
        cancel: asyncio.Event | None = None,
    ):
        if not self._valid():
            raise MCPTransportError("MCP server was closed or replaced", dispatched=False)
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._owner(), name=f"mcp:{self.config.name}")
            self.task.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        future = asyncio.get_running_loop().create_future()
        await self.queue.put(_Request(method, name, arguments or {}, future, cancel))
        # Do not cancel the owner when the caller is cancelled: the owner must
        # settle the in-flight result and close SDK task groups in the same task.
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            if cancel is not None:
                cancel.set()
            future.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
            raise

    async def _owner(self):
        stack: AsyncExitStack | None = None
        session = None
        request = None
        dispatched = False
        try:
            while True:
                request = await self.queue.get()
                dispatched = False
                if request is None:
                    break
                if not self._valid() or (request.cancel and request.cancel.is_set()):
                    request.future.set_exception(
                        MCPTransportError("MCP request cancelled before dispatch", dispatched=False)
                    )
                    continue
                if session is None:
                    for attempt in range(self.config.connect_attempts):
                        stack = AsyncExitStack()
                        try:
                            # SDK task-group lifetime belongs to this task, so do
                            # not enter the context inside wait_for/create_task.
                            async with asyncio.timeout(self.config.timeout_seconds):
                                session = await stack.enter_async_context(self.connector(self.config))
                            self.connected = True
                            break
                        except Exception:  # noqa: BLE001 -- third-party SDK errors are normalized at transport boundary
                            await stack.aclose()
                            stack = None
                            if attempt + 1 < self.config.connect_attempts:
                                await asyncio.sleep(min(2, 0.1 * 2**attempt))
                    if session is None:
                        self._failed()
                        request.future.set_exception(
                            MCPTransportError("MCP connection failed before dispatch", dispatched=False)
                        )
                        continue
                dispatched = False
                try:
                    if not self._valid() or (request.cancel and request.cancel.is_set()):
                        raise MCPTransportError("MCP request cancelled before dispatch", dispatched=False)
                    dispatched = True

                    async def call(request=request, session=session):
                        async with asyncio.timeout(self.config.timeout_seconds):
                            if request.method == "list":
                                tools, cursor, seen_cursors = [], None, set()
                                while True:
                                    page = await session.list_tools(cursor=cursor)
                                    tools.extend(page.tools)
                                    cursor = page.nextCursor
                                    if not cursor:
                                        return tools
                                    if cursor in seen_cursors:
                                        raise MCPTransportError(
                                            "Repeated MCP catalog cursor", dispatched=False
                                        )
                                    seen_cursors.add(cursor)
                            if request.method == "ping":
                                return await session.send_ping()
                            return await session.call_tool(
                                request.name,
                                request.arguments,
                                read_timeout_seconds=timedelta(seconds=self.config.timeout_seconds),
                            )

                    result = await _cancellable(call(), request.cancel)
                    if not request.future.done():
                        request.future.set_result(result)
                except (Exception, asyncio.CancelledError):  # noqa: BLE001 -- dispatched failures must become unknown, never replayed
                    if not request.future.done():
                        request.future.set_exception(
                            MCPTransportError("MCP request interrupted; not replayed", dispatched=dispatched)
                        )
                    # A repaired transport never implies the previous call had no effect.
                    if stack:
                        await stack.aclose()
                    stack, session = None, None
                    self._failed()
        except Exception:  # noqa: BLE001 -- any SDK owner failure is normalized for its pending callers.
            self._failed()
        finally:
            if request is not None and not request.future.done():
                request.future.set_exception(
                    MCPTransportError("MCP transport owner stopped; not replayed", dispatched=dispatched)
                )
            if stack:
                try:
                    await stack.aclose()
                except Exception:  # noqa: BLE001 -- SDK shutdown errors cannot strand pending callers.
                    self._failed()
            self.connected = False
            if not self.closed:
                self._failed()
            while not self.queue.empty():
                pending = self.queue.get_nowait()
                if pending is not None and not pending.future.done():
                    pending.future.set_exception(MCPTransportError("MCP client closed", dispatched=False))

    async def close(self):
        self.closed = True
        if self.task is not None:
            await self.queue.put(None)
            await self.task


def _wire_dict(value):
    return value.model_dump(by_alias=True, exclude_none=True) if hasattr(value, "model_dump") else value


class MCPTool:
    def __init__(self, connection: _Connection, definition):
        raw = _wire_dict(definition)
        self.connection = connection
        self.remote_name = raw["name"]
        base = re.sub(r"[^A-Za-z0-9_]", "_", connection.config.name + "__" + self.remote_name)
        digest = hashlib.sha256((connection.config.name + "\0" + self.remote_name).encode()).hexdigest()[:8]
        name = "mcp_" + base[:46] + "_" + digest
        annotations = raw.get("annotations") or {}
        read_only = connection.config.trust_annotations and annotations.get("readOnlyHint") is True
        replay = connection.config.trust_annotations and (
            read_only or annotations.get("idempotentHint") is True
        )
        self.spec = ToolSpec(
            name,
            raw.get("description") or self.remote_name,
            raw.get("inputSchema") or {"type": "object"},
            risk="read" if read_only else "external",
            concurrency_safe=False,
            replay_safe=replay,
            timeout_seconds=connection.config.timeout_seconds,
            resource=f"mcp:{connection.config.name}",
        )

    async def execute(self, arguments: dict, context: ToolContext) -> ToolResult:
        try:
            response = await self.connection.request(
                "call", name=self.remote_name, arguments=arguments, cancel=context.cancel
            )
        except MCPTransportError as exc:
            unknown = exc.dispatched and self.spec.risk != "read"
            return ToolResult(
                str(exc),
                is_error=True,
                error_category="mcp_transport",
                outcome="unknown" if unknown else "known",
                data={"dispatched": exc.dispatched},
            )
        raw = _wire_dict(response)
        texts, parts, artifacts = [], [], []
        for block in raw.get("content", []):
            if block.get("type") == "text":
                texts.append(block.get("text", ""))
                parts.append({"type": "input_text", "text": block.get("text", "")})
            elif block.get("type") == "image":
                try:
                    encoded = block.get("data", "")
                    if not isinstance(encoded, str) or len(encoded) > ((MAX_SOURCE_IMAGE_BYTES + 2) // 3) * 4:
                        raise ValueError("Image exceeds the 25 MiB source budget; request a smaller image")
                    data = base64.b64decode(encoded, validate=True)
                    mime = block.get("mimeType", "image/png")
                    suffix = {
                        "image/png": ".png",
                        "image/jpeg": ".jpg",
                        "image/webp": ".webp",
                        "image/gif": ".gif",
                    }[mime]
                    # Identity belongs to this registered file, not its bytes:
                    # repeated images in different runs have different paths.
                    artifact_id = uuid.uuid4().hex
                    path = context.artifact_dir / "mcp" / (artifact_id + suffix)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
                    artifact = {
                        "artifact_id": artifact_id,
                        "path": str(path),
                        "mime_type": mime,
                        "size": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "session_id": context.session_id,
                        "run_id": context.run_id,
                    }
                    artifacts.append(artifact)
                    prepared = await asyncio.to_thread(prepare_model_image, data, mime)
                    if prepared.changed:
                        model_id = uuid.uuid4().hex
                        model_path = path.with_name(model_id + "-model.jpg")
                        await asyncio.to_thread(model_path.write_bytes, prepared.data)
                        model_artifact = {**artifact, "artifact_id": model_id, "path": str(model_path),
                                          "mime_type": prepared.mime_type, "size": len(prepared.data),
                                          "sha256": hashlib.sha256(prepared.data).hexdigest(),
                                          "source_artifact_id": artifact_id,
                                          "width": prepared.width, "height": prepared.height}
                        artifacts.append(model_artifact)
                        path, mime, artifact_id = model_path, prepared.mime_type, model_id
                        texts.append("[MCP image uses a bounded model preview; the original image is registered as an artifact.]")
                        parts.append({"type": "input_text", "text": texts[-1]})
                    parts.append(
                        {
                            "type": "input_image",
                            "path": str(path),
                            "mime_type": mime,
                            "artifact_id": artifact_id,
                        }
                    )
                except (ValueError, KeyError):
                    texts.append("[MCP image could not be decoded within the supported PNG/JPEG/GIF/WebP and 25 MiB/64 megapixel budgets; "
                                 "request a valid cropped or smaller image. Any decoded original bytes remain registered.]")
                    parts.append({"type": "input_text", "text": texts[-1]})
            else:
                # Preserve non-text resources as data; never follow embedded instructions.
                texts.append(json.dumps(block, ensure_ascii=False))
                parts.append({"type": "input_text", "text": texts[-1]})
        return ToolResult(
            "\n".join(texts),
            is_error=bool(raw.get("isError", False)),
            artifacts=artifacts,
            data={"content_parts": parts, "structured_content": raw.get("structuredContent")},
        )


@dataclass
class _ServerState:
    connection: _Connection
    generation: int = 1
    status: str = "not_connected"
    definitions: list = field(default_factory=list)
    attempts: int = 0
    next_retry_at: float | None = None
    last_success_at: float | None = None
    error_category: str | None = None
    catalog_task: asyncio.Task | None = None
    recovery_task: asyncio.Task | None = None


class MCPClient:
    def __init__(self, configs: list[MCPServerConfig] = (), *, connector=None, secret_resolver=None,
                 binding_guard=None):
        self.connector = connector or partial(sdk_connection, secret_resolver=secret_resolver)
        self.connections: dict[str, _Connection] = {}
        self._states: dict[str, _ServerState] = {}
        self._closed = False
        self._lifecycle_lock = asyncio.Lock()
        self.binding_guard = binding_guard
        for config in configs:
            if config.name in self.connections:
                raise ValueError("Duplicate MCP server name")
            connection = _Connection(config, self.connector, on_failure=self._failed, binding_guard=binding_guard)
            self.connections[config.name] = connection
            self._states[config.name] = _ServerState(connection)

    def health(self) -> list[dict]:
        """Only safe operational metadata; transport/config values never leave here."""
        return [{"name": name, "status": state.status, "required": state.connection.config.required,
                 "generation": state.generation, "connected": state.connection.connected,
                 "tool_count": len(state.definitions) if state.status == "healthy" else 0,
                 "reconnect_attempts": state.attempts, "next_retry_at": state.next_retry_at,
                 "last_success_at": state.last_success_at, "error_category": state.error_category}
                for name, state in sorted(self._states.items())]

    def _current(self, name, state):
        return (not self._closed and self._states.get(name) is state and state.connection._valid())

    def _failed(self, connection):
        name = connection.config.name
        state = self._states.get(name)
        if state is None or state.connection is not connection or not self._current(name, state):
            return
        state.status = "unavailable"
        state.error_category = "mcp_transport"
        self._schedule_recovery(name, state)

    def _schedule_recovery(self, name, state):
        if not self._current(name, state):
            return
        if state.recovery_task is not None and not state.recovery_task.done():
            return
        if state.attempts >= state.connection.config.reconnect_attempts:
            state.status = "exhausted"
            state.next_retry_at = None
            return
        state.recovery_task = asyncio.create_task(self._recover(name, state), name=f"mcp-reconnect:{name}")

    async def _refresh(self, name, state):
        state.status = "connecting" if state.attempts == 0 else "retrying"
        try:
            definitions = await state.connection.request("list")
            # Validate the catalogue before making it available to a run.
            for definition in definitions:
                MCPTool(state.connection, definition)
        except Exception:  # noqa: BLE001 -- SDK/protocol failures are sanitized operational health.
            if self._current(name, state):
                self._failed(state.connection)
            raise MCPTransportError("MCP catalog is unavailable", dispatched=False) from None
        # A replacement can retire this state while discovery awaits the server;
        # discard its catalogue instead of publishing tools from an old generation.
        if not self._current(name, state):
            raise MCPTransportError("MCP server was replaced or closed", dispatched=False)
        state.definitions = definitions
        state.status = "healthy"
        state.error_category = None
        state.last_success_at = time.time()
        state.next_retry_at = None
        state.attempts = 0
        return definitions

    async def _catalog(self, name, state):
        if state.status == "healthy" and state.connection.connected:
            return state.definitions
        if state.catalog_task is None or state.catalog_task.done():
            state.catalog_task = asyncio.create_task(self._refresh(name, state), name=f"mcp-catalog:{name}")
            # A foreground admission timeout leaves the owner alive. Consume a
            # subsequent failure even when shutdown wins before its recovery task.
            state.catalog_task.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None
            )
        return await asyncio.shield(state.catalog_task)

    async def _recover(self, name, state):
        config = state.connection.config
        try:
            while self._current(name, state) and state.attempts < config.reconnect_attempts:
                delay = min(config.reconnect_max_seconds,
                            config.reconnect_initial_seconds * 2**state.attempts)
                state.next_retry_at = time.time() + delay
                await asyncio.sleep(delay)
                if not self._current(name, state) or state.status == "healthy":
                    return
                state.attempts += 1
                state.next_retry_at = None
                try:
                    await self._catalog(name, state)
                    return
                except MCPTransportError:
                    pass
            if self._current(name, state):
                state.status = "exhausted"
                state.next_retry_at = None
        finally:
            if self._current(name, state):
                state.next_retry_at = None

    async def _available_tools(self, name, *, admission):
        state = self._states[name]
        try:
            if state.connection.config.required or not admission:
                definitions = await self._catalog(name, state)
            else:
                async with asyncio.timeout(state.connection.config.discovery_timeout_seconds):
                    definitions = await self._catalog(name, state)
            return [MCPTool(state.connection, item) for item in definitions]
        except (MCPTransportError, TimeoutError):
            self._schedule_recovery(name, state)
            if state.connection.config.required:
                raise MCPTransportError(f"Required MCP service {name} is unavailable", dispatched=False) from None
            return []

    async def list_tools(self, server: str | None = None, *, admission=False) -> list[MCPTool]:
        if self._closed:
            raise MCPTransportError("MCP client is closed", dispatched=False)
        names = [server] if server else sorted(self.connections)
        groups = await asyncio.gather(*(self._available_tools(name, admission=admission) for name in names))
        return [tool for group in groups for tool in group]

    async def _retire(self, state):
        # Stop timers first. No abandoned callback may respawn a subprocess while
        # the connection owner drains/tears down its SDK task groups.
        tasks = [task for task in (state.recovery_task, state.catalog_task)
                 if task is not None and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        state.status = "closed"
        state.next_retry_at = None
        await state.connection.close()

    async def replace_server(self, config: MCPServerConfig):
        async with self._lifecycle_lock:
            if self._closed:
                raise MCPTransportError("MCP client is closed", dispatched=False)
            old = self._states.get(config.name)
            generation = old.generation + 1 if old else 1
            if old is not None:
                # Invalidate before awaiting cleanup, which can yield to a retry timer.
                old.connection.closed = True
                await self._retire(old)
            connection = _Connection(config, self.connector, on_failure=self._failed,
                                     binding_guard=self.binding_guard)
            self.connections[config.name] = connection
            self._states[config.name] = _ServerState(connection, generation=generation)

    async def reconnect(self, server: str) -> list[dict]:
        await self.replace_server(self.connections[server].config)
        await self.list_tools(server)
        return [item for item in self.health() if item["name"] == server]

    async def close(self):
        self.invalidate()
        async with self._lifecycle_lock:
            await asyncio.gather(*(self._retire(state) for state in self._states.values()))

    def invalidate(self):
        """Reject new dispatch immediately; close later drains material calls."""
        self._closed = True
        for state in self._states.values():
            state.connection.closed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()
