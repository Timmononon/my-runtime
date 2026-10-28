"""Authenticated, operator-only profile and next-run selection routes."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from aster.providers.profiles import PROTOCOLS


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelMetadata(Body):
    id: str = Field(min_length=1, max_length=200)
    name: str | None = Field(default=None, max_length=200)
    context_window: int | None = Field(default=None, ge=1, le=10_000_000)
    max_output_tokens: int | None = Field(default=None, ge=1, le=10_000_000)
    input_modalities: list[Literal["text", "image"]] = Field(default_factory=lambda: ["text"])
    effort_levels: list[str] = Field(default_factory=list, max_length=10)


class NewProfile(Body):
    name: str = Field(min_length=1, max_length=100)
    base_url: str = Field(max_length=2000)
    protocol: Literal["deepseek_responses", "openai_responses", "openai_chat"]
    models: list[ModelMetadata] = Field(default_factory=list, max_length=1000)
    api_key: SecretStr | None = None


class ProfilePatch(Body):
    expected_revision: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    base_url: str | None = Field(default=None, max_length=2000)
    protocol: Literal["deepseek_responses", "openai_responses", "openai_chat"] | None = None
    models: list[ModelMetadata] | None = Field(default=None, max_length=1000)
    api_key: SecretStr | None = None


class Revision(Body):
    expected_revision: int = Field(ge=1)


class Selection(Body):
    provider_id: str | None = Field(default=None, max_length=200)
    model: str | None = Field(default=None, max_length=200)
    reasoning_effort: str | None = Field(default=None, max_length=30)
    selected_skill: str | None = Field(default=None, max_length=128)


def _fields(body):
    values = body.model_dump(exclude_unset=True)
    if "api_key" in values:
        if body.api_key is None:
            raise ValueError("Omit API key to keep it; use the key deletion endpoint to disconnect")
        values["api_key"] = body.api_key.get_secret_value()
    return values


def create_provider_router(coordinator):
    router = APIRouter(tags=["provider settings"])

    @router.get("/providers")
    async def providers():
        return {"providers": coordinator.providers.list(), "protocols": PROTOCOLS}

    @router.post("/providers", status_code=201)
    async def create(body: NewProfile):
        return coordinator.providers.create(**_fields(body))

    @router.patch("/providers/{provider_id}")
    async def update(provider_id: str, body: ProfilePatch):
        return coordinator.providers.update(provider_id, **_fields(body))

    @router.delete("/providers/{provider_id}")
    async def delete(provider_id: str, expected_revision: int = Query(ge=1)):
        return coordinator.providers.delete(provider_id, expected_revision)

    @router.delete("/providers/{provider_id}/key")
    async def disconnect(provider_id: str, body: Revision):
        return coordinator.providers.delete_key(provider_id, body.expected_revision)

    @router.post("/providers/{provider_id}/models/fetch")
    async def fetch(provider_id: str, body: Revision):
        from aster.providers.deepseek import ProviderError
        try:
            return await coordinator.providers.fetch_models(provider_id, body.expected_revision)
        except ProviderError as error:
            return JSONResponse({"error": "provider", "category": error.category,
                                 "status_code": error.status_code}, status_code=502)

    @router.get("/sessions/{session_id}/selection")
    async def selection(session_id: str):
        return coordinator.session_selection(session_id)

    @router.patch("/sessions/{session_id}/selection")
    async def select(session_id: str, body: Selection):
        return coordinator.select_session(session_id, **body.model_dump(exclude_unset=True))

    return router
