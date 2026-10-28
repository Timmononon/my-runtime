"""Resolve the user's Runtime directory without creating or migrating state."""
from __future__ import annotations

import os
from pathlib import Path


def resolve_data_dir(explicit: str | Path | None = None) -> Path:
    """Explicit directory > ASTER_DATA_DIR > ASTER_HOME > ~/.aster."""
    chosen = explicit
    if chosen is None:
        chosen = os.environ.get("ASTER_DATA_DIR") or os.environ.get("ASTER_HOME") or None
    path = Path(chosen).expanduser() if chosen is not None else Path.home() / ".aster"
    return path.resolve()
