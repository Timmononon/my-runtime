"""Exact read grants for registered evidence, including bounded branch history."""
import json
from pathlib import Path

from aster.maintenance import resolve_artifact


def read_registered_artifact(store, data_dir, session_id, raw):
    store.get_session(session_id)
    records = []
    # A branch inherits only messages through its selected history boundary.
    # No filesystem ancestor/sibling directory is granted by this lookup.
    for message in store.history(session_id):
        records.extend(message.metadata.get("artifacts", []))
        if item := message.metadata.get("output_artifact"):
            records.append(item)
    # This session's registration events cover later outputs without importing
    # additional grants from its parent or sibling conversations.
    with store._lock:
        events = store.connection.execute(
            "SELECT payload_json FROM events WHERE session_id=? AND type='artifact.registered' ORDER BY event_id",
            (session_id,),
        ).fetchall()
    for event in events:
        records.extend(json.loads(event[0]).get("artifacts", []))
    for record in records:
        identity, original = record.get("artifact_id"), record.get("path")
        if not identity or not original:
            continue
        try:
            path = resolve_artifact(data_dir, artifact_id=identity, path=original)
        except (ValueError, KeyError, OSError):
            continue
        if raw in {identity, original, str(path)} and Path(path).is_file():
            return Path(path)
    raise PermissionError("Artifact is not registered within this conversation's history boundary")
