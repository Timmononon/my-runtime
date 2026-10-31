"""Read-only offline guard installed solely through child PYTHONPATH."""
from __future__ import annotations

import atexit
import json
import os
import sys
from pathlib import Path

_events = []


def _audit(event, args):
    if event in {"socket.connect", "socket.connect_ex", "socket.getaddrinfo", "socket.gethostbyname",
                 "socket.gethostbyaddr", "socket.sendto", "subprocess.Popen", "os.system", "os.posix_spawn"}:
        _events.append({"kind": event})
        raise RuntimeError("Offline Python provider denies " + event)


sys.addaudithook(_audit)


def _finish():
    target = os.environ.get("ASTER_PROMPTFOO_PYTHON_GUARD")
    if target:
        Path(f"{target}.{os.getpid()}.json").write_text(json.dumps({
            "pid": os.getpid(), "external_or_process_attempts": len(_events), "events": _events,
            "entry_script": Path(sys.argv[0]).name,
        }, indent=2), encoding="utf-8")


atexit.register(_finish)
