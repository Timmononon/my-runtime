"""Mounted inside the existing authenticated app; mutations use its dispatcher."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from aster.config import read_yaml
from aster.memory.facts import StructuredMemory
from aster.memory.service import MemoryService
from aster.packages import SkillPackageManager


class Mutation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    arguments: dict = Field(default_factory=dict)
    approved: bool = False


def scoped_memory(coordinator, session_id):
    session = coordinator.store.get_session(session_id)
    return StructuredMemory(coordinator.store, MemoryService(coordinator.data_dir / "memory",
        agent_id=session["agent_id"], project_id=session["project_id"], history_search=coordinator.store.search_messages))


def create_customization_router(coordinator):
    router = APIRouter(prefix="/customization", tags=["customization"])
    packages = SkillPackageManager(coordinator.data_dir)

    @router.get("/skills")
    async def skills():
        return {"catalog": packages.catalog(), "installed": packages.installed()}

    @router.get("/sessions/{session_id}/memory")
    async def memory(session_id: str, query: str = "", scope: str = "agent", at: str | None = None, limit: int = 50):
        return scoped_memory(coordinator, session_id).recall(query=query, scopes=[scope], at=at, limit=limit)

    @router.get("/sessions/{session_id}/memory/{fact_id}/history")
    async def memory_history(session_id: str, fact_id: str, scope: str = "agent"):
        return {"revisions": scoped_memory(coordinator, session_id).history(fact_id, scope=scope)}

    @router.post("/sessions/{session_id}/{operation}")
    async def mutate(session_id: str, operation: str, body: Mutation):
        read_only = {"memory_recall", "memory_history", "memory_query", "skill_package_preview"}
        allowed = read_only | {"memory_record", "memory_update", "memory_forget", "memory_semantic_recall", "skill_package_install", "skill_package_uninstall"}
        if operation not in allowed:
            raise HTTPException(404, "Unknown customization operation")
        # The dispatcher still classifies the captured backend and applies its
        # policy. A concurrent switch to remote cannot inherit this local gate.
        local_recall = (operation == "memory_semantic_recall"
                        and read_yaml(coordinator.data_dir / "config.yaml").get("embedding", {}).get("backend") == "local")
        if operation not in read_only and not local_recall and not body.approved:
            raise HTTPException(403, "Explicit operator approval of these arguments is required")
        result = await coordinator.execute_session_tool(session_id, operation, body.arguments,
                                                         unattended=True, approved=body.approved)
        return {"content": result.content, "is_error": result.is_error, "outcome": result.outcome, "data": result.data}

    return router
