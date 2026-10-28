"""Content endpoints inherit the parent application's authentication middleware."""
import json

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict

from aster.service.attachments import MAX_ATTACHMENT_BYTES


class PathAttachment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    mime_type: str | None = None


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    artifact_id: str
    interactive: bool = False


def create_content_router(coordinator) -> APIRouter:
    router = APIRouter()

    @router.post("/sessions/{session_id}/attachments", status_code=201)
    async def upload(session_id: str, request: Request, filename: str = Query(min_length=1, max_length=180)):
        coordinator.store.get_session(session_id)
        size = request.headers.get("content-length")
        if size and (not size.isdigit() or int(size) > MAX_ATTACHMENT_BYTES):
            raise HTTPException(413, "Attachment exceeds 25 MiB")
        content = bytearray()
        async for chunk in request.stream():
            if len(content) + len(chunk) > MAX_ATTACHMENT_BYTES:
                raise HTTPException(413, "Attachment exceeds 25 MiB")
            content.extend(chunk)
        return coordinator.attachments.register_bytes(session_id, filename, bytes(content),
                                                       request.headers.get("content-type"))

    @router.post("/sessions/{session_id}/attachments/from-path", status_code=201)
    async def register(session_id: str, body: PathAttachment):
        return coordinator.attachments.register_path(session_id, body.path, body.mime_type)

    @router.get("/sessions/{session_id}/attachments")
    async def listing(session_id: str):
        return {"attachments": coordinator.attachments.list(session_id)}

    def authorized_artifact(session_id, artifact_id):
        coordinator.store.get_session(session_id)
        path, record = coordinator.artifact(artifact_id)
        inherited = coordinator.store.inherited_attachment_ids(session_id)
        if record.get("session_id") != session_id and artifact_id not in inherited:
            raise PermissionError("Artifact belongs to another session")
        return path, record

    @router.get("/sessions/{session_id}/deliverables")
    async def deliverables(session_id: str):
        coordinator.store.get_session(session_id)
        inherited = coordinator.store.inherited_attachment_ids(session_id)
        with coordinator.store._lock:
            rows = coordinator.store.connection.execute(
                "SELECT session_id,payload_json FROM events WHERE type='artifact.registered' ORDER BY event_id"
            ).fetchall()
        found = {}
        for row in rows:
            for record in json.loads(row["payload_json"]).get("artifacts", []):
                if record.get("kind") == "deliverable" and (row["session_id"] == session_id or record["artifact_id"] in inherited):
                    found[record["artifact_id"]] = {**record, "download_path": f"/artifacts/{record['artifact_id']}"}
        return {"deliverables": list(found.values())}

    @router.post("/sessions/{session_id}/previews", status_code=201)
    async def preview(session_id: str, body: PreviewRequest):
        if body.interactive:
            coordinator.store.require_session_writable(session_id)
        path, record = authorized_artifact(session_id, body.artifact_id)
        lineage = coordinator.store.lineage(session_id)
        if lineage and lineage.get("closed_at") is not None:
            raise PermissionError("Closed side chats cannot create preview leases")
        manager = getattr(coordinator.factory, "preview", None)
        if manager is None:
            raise HTTPException(503, "Preview service is unavailable")
        url = manager.register(path, session_id, root=path.parent if record.get("preview_assets") else None,
                               interactive=body.interactive)
        return {"artifact_id": body.artifact_id, "preview_url": url,
                "expires_in_seconds": manager.ttl_seconds, "interactive": body.interactive}

    @router.delete("/sessions/{session_id}/previews", status_code=204)
    async def close_previews(session_id: str):
        coordinator.store.get_session(session_id)
        manager = getattr(coordinator.factory, "preview", None)
        if manager:
            manager.close_session(session_id)

    return router
