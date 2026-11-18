"""Operator management for Feishu group task sources."""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

SourceName = Annotated[str, Field(min_length=1, max_length=100)]
Sender = Annotated[str, Field(min_length=1, max_length=96)]
Keyword = Annotated[str, Field(min_length=1, max_length=120)]


class FeishuTaskBody(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class SourceCreate(FeishuTaskBody):
    source_session_id: str = Field(min_length=1, max_length=128)
    chat_id: str = Field(min_length=1, max_length=96)
    name: SourceName | None = None
    allowed_senders: list[Sender] = Field(default_factory=list, max_length=100)
    required_keywords: list[Keyword] = Field(default_factory=list, max_length=30)


class SourcePatch(FeishuTaskBody):
    expected_revision: int = Field(ge=0)
    name: SourceName | None = None
    enabled: bool | None = None
    allowed_senders: list[Sender] | None = Field(default=None, max_length=100)
    required_keywords: list[Keyword] | None = Field(default=None, max_length=30)


def create_feishu_tasks_router(coordinator):
    router = APIRouter(prefix="/feishu")
    service = coordinator.feishu

    @router.post("/discover")
    async def discover(body: FeishuTaskBody | None = None):
        return await service.discover()

    @router.get("/sources")
    async def list_sources(source_session_id: str | None = None):
        return {"sources": service.list_sources(source_session_id=source_session_id)}

    @router.post("/sources", status_code=201)
    async def create_source(body: SourceCreate):
        return await service.create_source(**body.model_dump())

    @router.get("/sources/{source_id}")
    async def get_source(source_id: str):
        return service.get_source(source_id)

    @router.patch("/sources/{source_id}")
    async def update_source(source_id: str, body: SourcePatch):
        return await service.update_source(source_id, **body.model_dump(exclude_unset=True))

    @router.get("/sources/{source_id}/events")
    async def events(source_id: str, before: int | None = Query(default=None, ge=1),
                     limit: int = Query(default=50, ge=1, le=100)):
        rows = service.events(source_id, before=before, limit=limit)
        tail = rows[-1]["seq"] if rows else None
        has_more = bool(tail and service.events(source_id, before=tail, limit=1))
        return {"events": rows, "next_before": tail if has_more else None}

    @router.get("/sources/{source_id}/tickets")
    async def tickets(source_id: str):
        return {"tickets": service.tickets(source_id)}

    return router
