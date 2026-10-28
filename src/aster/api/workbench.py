"""Authenticated project, local-directory and explicit context controls."""
import asyncio
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectCreate(Body):
    name: str = Field(min_length=1, max_length=120)
    cwd: str = Field(min_length=1, max_length=4096)


class ProjectUpdate(Body):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    cwd: str | None = Field(default=None, min_length=1, max_length=4096)


class CompactRequest(Body):
    method: Literal["deterministic", "semantic"] = "deterministic"


def create_workbench_router(coordinator):
    router = APIRouter()
    actions = coordinator.workbench

    @router.get("/workbench/projects")
    async def projects(include_removed: bool = False):
        return {"projects": actions.projects.list(include_removed=include_removed)}

    @router.post("/workbench/projects", status_code=201)
    async def create(body: ProjectCreate):
        return actions.projects.create(body.name, body.cwd)

    @router.patch("/workbench/projects/{project_id}")
    async def update(project_id: str, body: ProjectUpdate):
        return actions.projects.update(project_id, **body.model_dump(exclude_unset=True))

    @router.delete("/workbench/projects/{project_id}")
    async def remove(project_id: str, permanent: bool = False):
        if not permanent:
            raise HTTPException(status_code=409, detail="Refresh the client before confirming permanent project deletion")
        return actions.projects.remove(project_id)

    @router.post("/workbench/projects/{project_id}/restore")
    async def restore(project_id: str):
        raise HTTPException(status_code=410, detail="Project deletion is permanent; restoration is unavailable")

    @router.get("/workbench/folders")
    async def folders(path: str | None = None):
        return actions.projects.folders(path)

    @router.get("/workbench/skills")
    async def skills(agent_id: str = "default", project_id: str | None = None):
        return {"skills": actions.installed_skills(agent_id=agent_id, project_id=project_id)}

    @router.get("/sessions/{session_id}/context-usage")
    async def read_context_usage(session_id: str):
        from aster.service.context_usage import context_usage
        return context_usage(coordinator.store, session_id)

    @router.post("/sessions/{session_id}/compact")
    async def compact(session_id: str, body: CompactRequest | None = None):
        method = body.method if body else "deterministic"
        try:
            return await coordinator.manual_compact(session_id, method=method)
        except asyncio.CancelledError:
            # The action has already closed its metered run and preserved the
            # prior checkpoint. Complete the outstanding UI request explicitly;
            # direct workbench callers still receive cancellation control flow.
            return {"cancelled": True, "changed": False, "semantic_applied": False,
                    "preserved_archive": True, "method": method}

    return router
