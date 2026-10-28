"""Loopback HTTP control and resumable SSE over the coordinator's durable state."""
from __future__ import annotations

import asyncio
import hmac
import json
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from aster import __version__
from aster.api.web import (
    COOKIE_NAME,
    PERMANENT_DELETE_CAPABILITIES,
    UI_CSP,
    UI_HEADER,
    UIAuth,
    create_web_router,
    public_web_request,
    request_origin,
)
from aster.domain import ToolResult, to_dict
from aster.service import ConfigurationError, RunCoordinator
from aster.state import StateError

MANAGEMENT_CAPABILITIES = ("management_v1", "mcp_health_v1", "automation_selection_v1", "performance_timeline_v1",
                           "browser_handoff_v1", "web_search_v1", "web_search_deepseek_v1", "memory_graph_v1", "memory_semantic_recall_v1",
                           "context_compaction_v2", "semantic_compaction_action_v1", "context_usage_v1", "local_embedding_v1")


def load_api_token(data_dir: Path) -> str:
    path = Path(data_dir) / ".api-token"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        token = secrets.token_urlsafe(32)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(descriptor, "w", encoding="ascii") as stream:
                stream.write(token)
    token = path.read_text(encoding="ascii").strip()
    if len(token) < 32:
        raise ConfigurationError("Local API token is invalid; remove the invalid token file while service is stopped")
    return token


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NewSession(Body):
    cwd: str | None = None
    agent_id: str = "default"
    project_id: str | None = None
    title: str = Field(default="", max_length=200)


class SessionPatch(Body):
    title: str = Field(max_length=200)


class RunRequest(Body):
    text: str = Field(default="", max_length=2_000_000)
    attachments: list[str] = Field(default_factory=list, max_length=20)
    request_id: str | None = Field(default=None, max_length=200)
    unattended: bool = False
    command: str | None = None
    command_args: dict | None = None
    provider_id: str | None = Field(default=None, max_length=200)
    model: str | None = Field(default=None, max_length=200)
    reasoning_effort: str | None = Field(default=None, max_length=30)
    selected_skill: str | None = Field(default=None, max_length=128)


class UserMessage(Body):
    text: str = Field(default="", max_length=2_000_000)
    attachments: list[str] = Field(default_factory=list, max_length=20)
    steer: bool = False
    request_id: str | None = None


class Decision(Body):
    approved: bool


class Answer(Body):
    answer: str | dict | list


class Recovery(Body):
    allow_configuration_change: bool = False


class Reconciliation(Body):
    content: str
    evidence: str = Field(min_length=1)
    is_error: bool = False


class AutomationJob(Body):
    kind: str
    session_id: str
    prompt: str = Field(min_length=1)
    config: dict
    enabled: bool = True


class AutomationPatch(Body):
    prompt: str | None = None
    config: dict | None = None
    enabled: bool | None = None


def create_app(coordinator: RunCoordinator, *, token: str | None = None,
               allowed_origins: set[str] | None = None, manage_lifecycle=True,
               static_root: Path | None = None, ui_auth: UIAuth | None = None) -> FastAPI:
    token = token or load_api_token(coordinator.data_dir)
    origins = allowed_origins or {"http://127.0.0.1:8765", "http://localhost:8765"}
    if coordinator.factory and coordinator.factory.browser:
        coordinator.factory.browser.protect_ui_origins(origins)
    browser_auth = ui_auth or UIAuth()

    @asynccontextmanager
    async def lifespan(app):
        if manage_lifecycle:
            await coordinator.start()
        try:
            yield
        finally:
            browser_auth.clear()
            if manage_lifecycle:
                await coordinator.close()

    app = FastAPI(title="Aster Runtime", version=__version__, docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    app.state.coordinator = coordinator
    app.state.ui_auth = browser_auth
    from aster.api.browser_control import create_browser_control_router
    from aster.api.content import create_content_router
    from aster.api.customization import create_customization_router
    from aster.api.management import create_management_router
    from aster.api.providers import create_provider_router
    from aster.api.runtime import create_runtime_router
    from aster.api.workbench import create_workbench_router
    app.include_router(create_content_router(coordinator))
    app.include_router(create_browser_control_router(coordinator))
    app.include_router(create_customization_router(coordinator))
    app.include_router(create_management_router(coordinator))
    app.include_router(create_runtime_router(coordinator))
    app.include_router(create_provider_router(coordinator))
    app.include_router(create_workbench_router(coordinator))
    app.include_router(create_web_router(coordinator, browser_auth, static_root))

    @app.middleware("http")
    async def authorize(request: Request, call_next):
        def reject(error, status):
            return JSONResponse({"error": error}, status_code=status,
                                headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

        host = request.url.hostname
        if host not in {"127.0.0.1", "localhost", "::1"} or len(request.headers.getlist("host")) != 1:
            return reject("invalid_host", 403)
        origin = request.headers.get("origin")
        if len(request.headers.getlist("origin")) > 1 or (origin and origin not in origins):
            return reject("invalid_origin", 403)
        own_origin = request_origin(request)
        exchange = request.method == "POST" and request.url.path == "/ui/auth"
        if exchange and (request.headers.get(UI_HEADER) != "1" or origin != own_origin):
            return reject("csrf_rejected", 403)
        public = (request.url.path == "/health" and request.method in {"GET", "HEAD"}) or public_web_request(request)
        request.state.auth_method = None
        if not public:
            supplied = request.headers.get("authorization")
            if supplied is not None:
                if not hmac.compare_digest(supplied, "Bearer " + token):
                    return reject("unauthorized", 401)
                request.state.auth_method = "bearer"
            else:
                grant = browser_auth.authenticate(request.cookies.get(COOKIE_NAME), own_origin)
                if grant is None:
                    return reject("unauthorized", 401)
                if (request.headers.get(UI_HEADER) != "1" or (origin is not None and origin != own_origin)
                        or (request.method not in {"GET", "HEAD"} and origin != own_origin)
                        or own_origin not in origins):
                    return reject("csrf_rejected", 403)
                request.state.auth_method = "cookie"
                request.state.ui_grant = grant
        response = await call_next(request)
        if not (request.method in {"GET", "HEAD"} and request.url.path.startswith("/assets/") and response.status_code == 200):
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = UI_CSP
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.exception_handler(KeyError)
    async def missing(request, error):
        return JSONResponse({"error": "not_found"}, status_code=404)

    @app.exception_handler(RequestValidationError)
    async def invalid_body(request, error):
        # Pydantic errors include rejected input and arbitrary field names. Keys
        # must never appear in a 422 body, including malformed or extra fields.
        return JSONResponse({"error": "invalid_request", "detail": "Request fields are invalid"}, status_code=422)

    from aster.customization.credentials import CredentialError
    from aster.providers.deepseek import ProviderError

    @app.exception_handler(CredentialError)
    async def credential_error(request, error):
        return JSONResponse({"error": "credential_store", "detail": "The local credential configuration file is unavailable"}, status_code=503)

    @app.exception_handler(ProviderError)
    async def provider_error(request, error):
        return JSONResponse({"error": "provider", "category": error.category}, status_code=502)

    @app.exception_handler(FileNotFoundError)
    async def missing_file(request, error):
        return JSONResponse({"error": "artifact_not_found"}, status_code=404)

    @app.exception_handler(PermissionError)
    async def denied(request, error):
        return JSONResponse({"error": "permission_denied"}, status_code=403)

    @app.exception_handler(ValueError)
    async def invalid(request, error):
        return JSONResponse({"error": "invalid_request", "detail": str(error)}, status_code=422)

    @app.exception_handler(StateError)
    async def conflict(request, error):
        return JSONResponse({"error": "state_conflict", "detail": str(error)}, status_code=409)

    @app.exception_handler(ConfigurationError)
    async def configuration(request, error):
        return JSONResponse({"error": "configuration", "detail": str(error)}, status_code=503)

    from aster.customization.management import ManagementUnavailable
    from aster.customization.transactions import ManagementRecoveryError

    @app.exception_handler(ManagementUnavailable)
    async def management_unavailable(request, error):
        return JSONResponse({"error": "management_unavailable", "detail": str(error)}, status_code=503)

    @app.exception_handler(ManagementRecoveryError)
    async def management_recovery_conflict(request, error):
        return JSONResponse({"error": "management_recovery_conflict", "detail": str(error)}, status_code=409)

    @app.api_route("/health", methods=["GET", "HEAD"])
    async def health():
        return {"status": "ok", "instance_id": coordinator.instance_id, "schema_version": 1, "version": __version__,
                "provider_configured": coordinator.configured(), "capabilities": [*PERMANENT_DELETE_CAPABILITIES, *MANAGEMENT_CAPABILITIES]}

    @app.get("/capabilities")
    async def capabilities():
        return {"schema_version": 1, "capabilities": ["runs_v1", "queue_v1", "events_cursor_v1", "approval_v1",
                "questions_v1", "artifacts_v1", "manual_recovery_v1", "session_lineage_v1", "attachments_v1",
                "mailbox_retract_v1", "steering_v1", "performance_v1", "structured_memory_v1", "skill_packages_v1",
                "deliverable_preview_v1", "automation_sessions_v1", "web_workbench_v1", "provider_profiles_v1",
                "session_model_selection_v1", "workbench_projects_v1", "manual_compaction_v1",
                *MANAGEMENT_CAPABILITIES,
                *PERMANENT_DELETE_CAPABILITIES], "desktop_control": False}

    @app.post("/sessions", status_code=201)
    async def create_session(body: NewSession):
        return coordinator.create_session(**body.model_dump())

    @app.get("/sessions")
    async def sessions(include_side_chats: bool = False, state: str = "active",
                       limit: int = Query(default=100, ge=1, le=100), offset: int = Query(default=0, ge=0)):
        return {"sessions": coordinator.store.list_sessions(include_side_chats=include_side_chats, state=state,
                                                           limit=limit, offset=offset)}

    @app.post("/sessions/{session_id}/archive")
    async def archive_session(session_id: str):
        return await coordinator.change_session_lifecycle(session_id, "archive")

    @app.post("/sessions/{session_id}/unarchive")
    async def unarchive_session(session_id: str):
        return await coordinator.change_session_lifecycle(session_id, "unarchive")

    @app.delete("/sessions/{session_id}")
    async def delete_session(session_id: str, permanent: bool = False):
        if not permanent:
            raise HTTPException(status_code=409, detail="Refresh the client before confirming permanent conversation deletion")
        return await coordinator.change_session_lifecycle(session_id, "delete")

    @app.post("/sessions/{session_id}/restore")
    async def restore_session(session_id: str):
        raise HTTPException(status_code=410, detail="Conversation deletion is permanent; restoration is unavailable")

    @app.get("/sessions/{session_id}")
    async def get_session(session_id: str):
        return {**coordinator.store.get_session(session_id), "runs": coordinator.store.list_runs(session_id),
                "messages": coordinator.store.history_records(session_id), "plan": coordinator.store.get_plan(session_id),
                "selection": coordinator.session_selection(session_id)}

    @app.patch("/sessions/{session_id}")
    async def rename_session(session_id: str, body: SessionPatch):
        return coordinator.store.update_session_title(session_id, body.title)

    @app.post("/sessions/{session_id}/runs", status_code=202)
    async def submit(session_id: str, body: RunRequest):
        return await coordinator.submit(session_id, **body.model_dump(exclude_unset=True))

    @app.post("/sessions/{session_id}/messages", status_code=202)
    async def inject(session_id: str, body: UserMessage):
        coordinator.store.get_session(session_id)
        mid = coordinator.inject(session_id, **body.model_dump())
        return {"schema_version": 1, "message_id": mid, "status": "queued"}

    @app.get("/runs/{run_id}")
    async def get_run(run_id: str):
        from aster.engine.telemetry import cost_estimate
        run = coordinator.store.get_run(run_id)
        usage = coordinator.store.usage(run_id)
        budget = coordinator.store.run_budget(run_id)
        kind = (coordinator.store.run_request(run_id) or {}).get("kind")
        return {**run, "kind": kind if kind in {"compaction", "tool_action"} else "task",
                "pending_requests": coordinator.store.pending_requests(run_id),
                "tool_executions": coordinator.store.tool_executions(run_id), "usage": usage, "budget": budget,
                "cost_estimate": cost_estimate(coordinator.store, run, coordinator.data_dir),
                "cost_scope": "Configured price estimate for recorded model requests; unknown usage is not zero"}

    @app.post("/runs/{run_id}/cancel")
    async def cancel(run_id: str):
        return {"accepted": coordinator.cancel(run_id), "run_id": run_id}

    @app.post("/runs/{run_id}/recover")
    async def recover(run_id: str, body: Recovery):
        return to_dict(await coordinator.recover(run_id, allow_configuration_change=body.allow_configuration_change))

    @app.post("/executions/{execution_id}/reconcile")
    async def reconcile(execution_id: str, body: Reconciliation):
        coordinator.store.reconcile_tool(execution_id, ToolResult(body.content, is_error=body.is_error), evidence=body.evidence)
        return {"execution_id": execution_id, "status": "reconciled"}

    @app.post("/approvals/{request_id}/decision")
    async def approve(request_id: str, body: Decision):
        pending = coordinator.store.get_pending(request_id)
        if pending["kind"] != "approval":
            raise HTTPException(409, "Request is not an approval")
        return {"accepted": coordinator.approvals.resolve(request_id, body.approved)}

    @app.post("/questions/{request_id}/answer")
    async def answer(request_id: str, body: Answer):
        pending = coordinator.store.get_pending(request_id)
        if pending["kind"] != "question":
            raise HTTPException(409, "Request is not a question")
        return {"accepted": coordinator.questions.resolve(request_id, body.answer)}

    @app.get("/runs/{run_id}/events")
    async def events(run_id: str, request: Request, after: int = Query(default=0, ge=0), follow: bool = True):
        coordinator.store.get_run(run_id)
        if "last-event-id" in request.headers:
            try:
                after = max(after, int(request.headers["last-event-id"]))
            except ValueError as error:
                raise HTTPException(422, "Last-Event-ID must be an integer") from error

        async def stream():
            cursor = after
            while True:
                rows = coordinator.store.events(run_id=run_id, after=cursor)
                for event in rows:
                    cursor = event["event_id"]
                    yield f"id: {cursor}\nevent: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                if len(rows) == 1000:
                    continue
                if not follow:
                    return
                completion = coordinator.store.completion_cursor(run_id)
                if completion is not None:
                    if cursor < completion:
                        # A terminal transaction may commit while the previous
                        # batch is suspended in an ASGI send. Drain its fence.
                        continue
                    return
                if await request.is_disconnected():
                    return
                yield ": keep-alive\n\n"
                await asyncio.sleep(0.25)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    @app.get("/artifacts/{artifact_id}")
    async def artifact(artifact_id: str):
        path, record = coordinator.artifact(artifact_id)
        return FileResponse(path, filename=path.name, media_type=record.get("mime_type", "application/octet-stream"),
                            content_disposition_type="attachment")

    @app.get("/events")
    async def global_events(after: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=1000)):
        return {"schema_version": 1, "events": coordinator.store.events(after=after, limit=limit)}

    @app.get("/automations")
    async def jobs(session_id: str | None = None):
        return {"jobs": coordinator.automation.list_jobs(session_id)}

    @app.post("/automations", status_code=201)
    async def create_job(body: AutomationJob):
        return coordinator.automation.create_job(**body.model_dump())

    @app.patch("/automations/{job_id}")
    async def update_job(job_id: str, body: AutomationPatch):
        return coordinator.automation.update_job(job_id, **body.model_dump(exclude_none=True))

    @app.delete("/automations/{job_id}")
    async def delete_job(job_id: str):
        coordinator.automation.delete_job(job_id)
        return {"deleted": job_id}

    @app.get("/automations/{job_id}/triggers")
    async def automation_triggers(job_id: str):
        coordinator.automation.get_job(job_id)
        return {"triggers": coordinator.automation.receipts(job_id)}

    return app
