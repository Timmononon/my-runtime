"""Explicit GitHub controls reuse Runtime authentication and action journaling."""
from typing import Literal

import httpx
from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from aster.domain import to_dict
from aster.github.transport import GithubHTTPError


async def _read_control(operation):
    try:
        return await operation
    except httpx.RequestError:
        raise GithubHTTPError("network") from None


class GithubBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceFields(GithubBody):
    repository: str = Field(min_length=3, max_length=200)
    enabled: bool = False
    mode: Literal["read_only", "tasks"] = "read_only"
    # Source creation starts with publishing disabled independently of its task mode.
    # Enabling observation or task admission alone does not authorize GitHub writes.
    allow_writes: bool = False
    poll_seconds: int = Field(default=300, ge=30, le=86400)
    allowed_actors: list[str] = Field(default_factory=list, max_length=1000)
    allowed_bots: list[str] = Field(default_factory=list, max_length=1000)
    bot_login: str | None = Field(default=None, max_length=128)
    observe_issues: bool = False
    observe_pulls: bool = False
    observe_comments: bool = False
    observe_ci: bool = False
    token: str | None = Field(default=None, min_length=1, max_length=4096)
    remove_token: bool = False


class SourceCreate(SourceFields):
    source_session_id: str = Field(min_length=1, max_length=200)


class SourcePatch(GithubBody):
    expected_revision: int = Field(ge=0)
    repository: str | None = Field(default=None, min_length=3, max_length=200)
    enabled: bool | None = None
    mode: Literal["read_only", "tasks"] | None = None
    allow_writes: bool | None = None
    poll_seconds: int | None = Field(default=None, ge=30, le=86400)
    allowed_actors: list[str] | None = Field(default=None, max_length=1000)
    allowed_bots: list[str] | None = Field(default=None, max_length=1000)
    bot_login: str | None = Field(default=None, max_length=128)
    observe_issues: bool | None = None
    observe_pulls: bool | None = None
    observe_comments: bool | None = None
    observe_ci: bool | None = None
    token: str | None = Field(default=None, min_length=1, max_length=4096)
    remove_token: bool = False


class Poll(GithubBody):
    read_only: Literal[True] | None = None


class DraftCreate(GithubBody):
    ticket_id: str | None = Field(default=None, min_length=1, max_length=200)
    kind: Literal["comment", "pull_request"]
    number: int | None = Field(default=None, ge=1)
    body: str = Field(min_length=1, max_length=100000)
    title: str | None = Field(default=None, min_length=1, max_length=300)
    head: str | None = Field(default=None, min_length=1, max_length=200)
    base: str | None = Field(default=None, min_length=1, max_length=200)
    request_id: str = Field(min_length=1, max_length=200)


class DraftPatch(GithubBody):
    expected_revision: int = Field(ge=0)
    body: str | None = Field(default=None, min_length=1, max_length=100000)
    title: str | None = Field(default=None, min_length=1, max_length=300)
    head: str | None = Field(default=None, min_length=1, max_length=200)
    base: str | None = Field(default=None, min_length=1, max_length=200)


class DraftSend(GithubBody):
    expected_revision: int = Field(ge=0)


class Binding(GithubBody):
    ticket_id: str = Field(min_length=1, max_length=200)
    number: int = Field(ge=1)


def create_github_router(coordinator):
    router = APIRouter()
    service = coordinator.github

    @router.get("/github/sources")
    async def sources(source_session_id: str | None = None):
        return {"sources": service.list_sources(source_session_id=source_session_id)}

    @router.post("/github/sources", status_code=201)
    async def create_source(body: SourceCreate):
        return service.create_source(**body.model_dump(exclude_none=True))

    @router.get("/github/sources/{source_id}")
    async def get_source(source_id: str):
        return service.get_source(source_id)

    @router.patch("/github/sources/{source_id}")
    async def update_source(source_id: str, body: SourcePatch):
        return service.update_source(source_id, **body.model_dump(exclude_unset=True))

    @router.post("/github/sources/{source_id}/check")
    async def check_source(source_id: str):
        return await _read_control(service.check_source(source_id))

    @router.post("/github/sources/{source_id}/poll")
    async def poll_source(source_id: str, body: Poll):
        return await _read_control(service.poll_source(source_id, read_only=body.read_only is True))

    @router.get("/github/sources/{source_id}/events")
    async def events(source_id: str, after: int = Query(default=0, ge=0), limit: int = Query(default=100, ge=1, le=500)):
        values = service.events(source_id, after=after, limit=limit)
        tail = values[-1]["seq"] if values else after
        has_more = bool(values and service.events(source_id, after=tail, limit=1))
        return {"events": values, "has_more": has_more, "next_after": tail if has_more else None}

    @router.get("/github/sources/{source_id}/drafts")
    async def drafts(source_id: str):
        return {"drafts": service.drafts(source_id)}

    @router.post("/github/sources/{source_id}/drafts", status_code=201)
    async def create_draft(source_id: str, body: DraftCreate):
        return service.create_draft(source_id, **body.model_dump(exclude_none=True))

    @router.patch("/github/drafts/{draft_id}")
    async def update_draft(draft_id: str, body: DraftPatch):
        return service.update_draft(draft_id, **body.model_dump(exclude_unset=True))

    @router.post("/github/drafts/{draft_id}/send")
    async def send(draft_id: str, body: DraftSend):
        draft = service.get_draft(draft_id)
        source = service.get_source(draft["source_id"])
        # A reviewed click approves this exact tool request once. Explicit
        # denials, allowed-tools, active Skills and source write gates still win.
        result = await coordinator.execute_session_tool(source["source_session_id"], "github_publish", {
            "draft_id": draft_id, "expected_revision": body.expected_revision,
        }, approved=True, unattended=False, source="github_review")
        return to_dict(result)

    @router.post("/github/drafts/{draft_id}/reconcile")
    async def reconcile(draft_id: str):
        return await _read_control(service.reconcile(draft_id))

    @router.post("/github/sources/{source_id}/bindings", status_code=201)
    async def bind(source_id: str, body: Binding):
        result = service.bind_thread(source_id, **body.model_dump())
        return await _read_control(result) if hasattr(result, "__await__") else result

    return router
