"""Authenticated local task management over the existing Runtime boundary."""
from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field


class TaskBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkerRequest(TaskBody):
    title: str = Field(min_length=1, max_length=200)
    goal: str = Field(min_length=1, max_length=100_000)
    agent_id: str | None = Field(default=None, min_length=1, max_length=128)
    role: Literal["worker", "reader"] = "worker"


class TicketRequest(TaskBody):
    source_session_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=200)
    goal: str = Field(min_length=1, max_length=100_000)
    workers: list[WorkerRequest] | None = Field(default=None, min_length=1, max_length=32)
    request_id: str | None = Field(default=None, min_length=1, max_length=200)
    unattended: bool = False


class CandidateRequest(TaskBody):
    source_session_id: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=100_000)
    request_id: str = Field(min_length=1, max_length=200)
    # Local candidate submission cannot claim the identity of a remote event source.
    # GitHub actor identity and admission belong to the separately validated source path.
    source: Literal["local"] = "local"


class TaskRule(TaskBody):
    id: str = Field(min_length=1, max_length=128)
    enabled: bool = True
    contains: list[str] = Field(min_length=1, max_length=32)


class TaskSettings(TaskBody):
    expected_revision: int = Field(ge=0)
    mode: Literal["off", "observe", "auto"]
    rules: list[TaskRule] = Field(max_length=100)


class TaskAction(TaskBody):
    expected_revision: int | None = Field(default=None, ge=0)


class TriageSettings(TaskBody):
    expected_revision: int = Field(ge=0)
    enabled: bool | None = None
    provider_id: str | None = Field(default=None, min_length=1, max_length=200)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    reasoning_effort: str | None = Field(default=None, max_length=30)
    processing_scope: str | None = Field(default=None, max_length=8000)
    timeout_seconds: int | None = Field(default=None, ge=1, le=120)
    max_input_chars: int | None = Field(default=None, ge=1000, le=200000)
    max_output_tokens: int | None = Field(default=None, ge=64, le=4096)
    max_requests_per_hour: int | None = Field(default=None, ge=1, le=10000)
    max_calls_per_poll: int | None = Field(default=None, ge=1, le=100)


class TaskInput(TaskAction):
    text: str = Field(min_length=1, max_length=100_000)
    request_id: str | None = Field(default=None, min_length=1, max_length=200)


def create_tasks_router(coordinator):
    router = APIRouter()
    service = coordinator.tasks

    @router.get("/tasks")
    async def list_tickets(source_session_id: str | None = None):
        return {"tickets": service.list_tickets(source_session_id=source_session_id)}

    @router.get("/tasks/overview")
    async def overview():
        return service.overview()

    @router.get("/tasks/settings")
    async def settings():
        return service.settings()

    @router.patch("/tasks/settings")
    async def update_settings(body: TaskSettings):
        return service.update_settings(**body.model_dump())

    @router.get("/tasks/triage/settings")
    async def triage_settings():
        return coordinator.task_triage.settings()

    @router.patch("/tasks/triage/settings")
    async def update_triage_settings(body: TriageSettings):
        # Omitted optional settings leave the saved values intact.
        # Explicit false remains in the patch so disabling triage is not lost as an empty value.
        return coordinator.task_triage.update_settings(**body.model_dump(exclude_none=True))

    @router.get("/tasks/triage/decisions")
    async def triage_decisions(source_session_id: str | None = None, limit: int = Query(default=100, ge=1, le=500)):
        return {"decisions": coordinator.task_triage.list_decisions(source_session_id=source_session_id, limit=limit)}

    @router.get("/tasks/candidates")
    async def candidates(source_session_id: str | None = None):
        return {"candidates": service.list_candidates(source_session_id=source_session_id)}

    @router.post("/tasks/candidates", status_code=201)
    async def evaluate_candidate(body: CandidateRequest):
        return service.evaluate_candidate(**body.model_dump())

    @router.post("/tasks", status_code=201)
    async def create_ticket(body: TicketRequest):
        return service.create_ticket(**body.model_dump(exclude_none=True))

    @router.get("/tasks/{ticket_id}")
    async def get_ticket(ticket_id: str):
        return service.get_ticket(ticket_id)

    @router.post("/tasks/{ticket_id}/cancel")
    async def cancel_ticket(ticket_id: str, body: TaskAction):
        return await service.cancel_ticket(ticket_id, **body.model_dump())

    @router.post("/tasks/{ticket_id}/retry")
    async def retry_ticket(ticket_id: str, body: TaskAction):
        return await service.retry_ticket(ticket_id, **body.model_dump())

    @router.post("/tasks/{ticket_id}/inputs")
    async def add_input(ticket_id: str, body: TaskInput):
        return await service.add_input(ticket_id, **body.model_dump())

    return router
