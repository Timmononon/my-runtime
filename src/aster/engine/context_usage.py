"""Content-free context composition, on the same scale as compaction estimates."""
from aster.engine.context import estimate_tokens


def context_usage_basis(system, tools, config, *, session_id, model="", overhead_tokens=0):
    envelope = estimate_tokens([])
    # Subtract the shared request envelope from these marginal estimates so
    # system and tool components do not count the same framing twice.
    system_tokens = max(0, estimate_tokens([], system=system) - envelope)
    tools_tokens = max(0, estimate_tokens([], tools, system) - envelope - system_tokens)
    return {"version": 1, "session_id": session_id, "model": model,
            "context_window": config.context_tokens, "reserved_output_tokens": config.reserve_tokens,
            "system_tokens": system_tokens, "tools_tokens": tools_tokens,
            "calibration_tokens": max(0, overhead_tokens)}
