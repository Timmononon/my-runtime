"""Session lineage, steering, and content-free runtime inspection."""
from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from aster.engine.telemetry import performance


class Branch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message_index: int | None = Field(default=None, ge=0)
    agent_id: str | None = None
    title: str | None = None


class SideChat(Branch):
    text: str | None = None
    attachments: list[str] = Field(default_factory=list)


def create_runtime_router(coordinator):
    router = APIRouter()

    @router.post("/sessions/{session_id}/fork", status_code=201)
    async def fork(session_id: str, body: Branch):
        return coordinator.branch(session_id, **body.model_dump())

    @router.post("/sessions/{session_id}/side-chat", status_code=201)
    async def side_chat(session_id: str, body: SideChat):
        child = coordinator.branch(session_id, side_chat=True, **body.model_dump(exclude={"text", "attachments"}))
        if body.text or body.attachments:
            child["run"] = await coordinator.submit(child["id"], body.text or "", attachments=body.attachments)
        return child

    @router.post("/sessions/{session_id}/close")
    async def close(session_id: str):
        coordinator.close_side_chat(session_id)
        return {"session_id": session_id, "closed": True, "audit_retained": True}

    @router.get("/sessions/{session_id}/lineage")
    async def lineage(session_id: str):
        coordinator.store.get_session(session_id)
        return {"lineage": coordinator.store.lineage(session_id), "ancestors": coordinator.store.session_ancestors(session_id)}

    @router.get("/sessions/{session_id}/mailbox")
    async def mailbox(session_id: str, include_terminal: bool = False):
        return {"messages": coordinator.store.list_mailbox(session_id, include_terminal=include_terminal)}

    @router.delete("/sessions/{session_id}/mailbox/{message_id}")
    async def retract(session_id: str, message_id: str):
        return {"message_id": message_id, "retracted": coordinator.store.retract_message(session_id, message_id)}

    @router.get("/runs/{run_id}/performance")
    async def inspect(run_id: str):
        return performance(coordinator.store, run_id, data_dir=coordinator.data_dir)

    return router
