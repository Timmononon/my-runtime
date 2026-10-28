"""Explicit operator browser leases and handoff, through governed tool actions."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from aster.config import read_yaml
from aster.customization.management import validate_browser


class Control(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    arguments: dict = Field(default_factory=dict)
    approved: bool = False
    request_id: str = Field(min_length=1, max_length=128)


def create_browser_control_router(coordinator):
    router = APIRouter(prefix="/browser-control")

    @router.get("/sessions/{session_id}")
    async def status(session_id: str):
        coordinator.store.get_session(session_id)
        config = validate_browser(read_yaml(coordinator.data_dir / "config.yaml").get("browser", {}))
        manager = coordinator.factory.browser if coordinator.factory else None
        if manager:
            return {**manager.status(session_id, config), "paused": coordinator.browser_controls().paused(session_id),
                    "receipts": coordinator.browser_controls().receipts(session_id)}
        return {"mode": config["mode"], "paused": False, "pages": [], "attached": False,
                "bridge_configured": bool(config["cdp_endpoint"]), "headed": False, "available": False}

    @router.post("/sessions/{session_id}/{operation}")
    async def control(session_id: str, operation: str, body: Control):
        mapping = {"attach": "browser_attach", "pause": "browser_handoff", "resume": "browser_handoff", "detach": "browser_close"}
        if operation not in mapping:
            raise HTTPException(404, "Unknown browser control operation")
        if not body.approved:
            raise HTTPException(403, "Explicit operator approval is required for these browser arguments")
        if operation in {"pause", "resume", "detach"} and body.arguments:
            raise HTTPException(422, "This operation does not accept extra browser arguments")
        result = await coordinator.browser_controls().execute(session_id, operation, body.arguments,
                                                               body.request_id, approved=True)
        return {"content": result.content, "is_error": result.is_error, "outcome": result.outcome, "data": result.data}

    return router
