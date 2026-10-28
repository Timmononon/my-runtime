"""Read current model-live context counts without starting resources or requests."""
import math

from aster.engine.context import estimate_tokens


def context_usage(store, session_id):
    # The Store lock keeps the checkpoint and its archive tail from crossing
    # a writer commit. No configuration, credential or external resource loads.
    with store._lock:
        store.get_session(session_id)
        checkpoint = store.get_checkpoint(session_id)
        messages = store.active_history(session_id)
        basis = (checkpoint or {}).get("metadata", {}).get("context_usage", {})
        message_tokens = estimate_tokens(messages)
        fields = ("context_window", "reserved_output_tokens", "system_tokens", "tools_tokens", "calibration_tokens")
        available = (isinstance(basis, dict) and basis.get("version") == 1
                     and basis.get("session_id") == session_id
                     and all(type(basis.get(key)) is int and basis[key] >= 0 for key in fields)
                     and basis["context_window"] > 0
                     and basis["reserved_output_tokens"] < basis["context_window"])
        model, provider_id = None, None
        if available:
            try:
                payload = store.run_request(checkpoint["run_id"]) or {}
            except KeyError:
                payload = {}
            snapshot = payload.get("snapshot", {})
            pin = snapshot.get("_provider", {}) or {}
            model = basis.get("model") or pin.get("model") or snapshot.get("model") or None
            provider_id = pin.get("id")
        parts = {"system_tokens": basis["system_tokens"] if available else None,
                 "tools_tokens": basis["tools_tokens"] if available else None,
                 "message_tokens": message_tokens,
                 "calibration_tokens": basis["calibration_tokens"] if available else None}
        used = sum(parts.values()) if available else None
        window = basis["context_window"] if available else None
        return {"schema_version": 1, "session_id": session_id,
                "status": "estimated" if available else "unavailable", "source": "active_context",
                "model": model, "provider_id": provider_id, "used_tokens": used, "context_window": window,
                "percent": min(100, math.floor(used / window * 100 + .5)) if available else None,
                "reserved_output_tokens": basis["reserved_output_tokens"] if available else None,
                "breakdown": parts, "updated_at": (checkpoint or {}).get("created_at")}
