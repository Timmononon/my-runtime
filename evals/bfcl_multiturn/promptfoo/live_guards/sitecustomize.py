"""Narrow networking admission for the explicitly authorized live provider child."""
from __future__ import annotations

import atexit
import ipaddress
import json
import os
import socket
import sys
from contextlib import contextmanager
from pathlib import Path

_active = False
_events = []
_allowed = []
_resolved = set()
_ports = set()
for _entry in json.loads(os.environ.get("ASTER_PF_LIVE_ENDPOINTS", "[]")):
    _host, _port = _entry
    _allowed.append(str(_host).lower())
    _ports.add(int(_port))
    try:
        _resolved.add(str(ipaddress.ip_address(_host)))
    except ValueError:
        pass
_counts = {"authorized_dns_events": 0, "authorized_connect_events": 0, "local_asyncio_ipc": 0}
_source = os.environ.get("ASTER_PF_LIVE_SOURCE_RUNTIME")
_source = Path(_source).resolve() if _source else None
_original_getaddrinfo = socket.getaddrinfo


def _hostname(value):
    return value.decode("ascii").lower() if isinstance(value, bytes) else str(value).lower()


def _deny(kind):
    _events.append(kind)
    raise PermissionError("Live provider guard rejected " + kind)


def _dns(host, *args, **kwargs):
    if not _active or _hostname(host) not in {*_allowed, *_resolved}:
        _deny("dns_outside_authorized_model_phase")
    results = _original_getaddrinfo(host, *args, **kwargs)
    _resolved.update(str(item[4][0]) for item in results)
    return results


socket.getaddrinfo = _dns


@contextmanager
def authorized_backend():
    global _active
    if _active:
        _deny("nested_backend_phase")
    _active = True
    try:
        yield
    finally:
        _active = False


def _audit(event, args):
    if event in {"subprocess.Popen", "os.system", "os.posix_spawn", "os.exec", "os.spawn"}:
        _deny("child_process")
    if event in {"socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr"}:
        if not _active or not args or _hostname(args[0]) not in {*_allowed, *_resolved}:
            _deny("dns_outside_endpoint")
        _counts["authorized_dns_events"] += 1
    if event in {"socket.connect", "socket.connect_ex", "socket.sendto"}:
        address = args[-1] if args else None
        if not _active or not isinstance(address, tuple) or len(address) < 2:
            _deny("network_outside_authorized_model_phase")
        host, port = str(address[0]), address[1]
        # Windows asyncio's private self-pipe uses a new loopback TCP pair.
        if host in {"127.0.0.1", "::1"} and port not in _ports:
            _counts["local_asyncio_ipc"] += 1
        elif host in _resolved and port in _ports:
            _counts["authorized_connect_events"] += 1
        else:
            _deny("connect_outside_endpoint")
    if event == "open" and _source is not None and args and isinstance(args[0], (str, bytes, os.PathLike)):
        candidate = Path(os.fsdecode(args[0])).resolve()
        mode, flags = args[1] or "", args[2] or 0
        write = any(char in mode for char in "wax+") or flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT)
        if write and candidate.is_relative_to(_source):
            _deny("source_runtime_write")
    if event in {"os.mkdir", "os.remove", "os.rmdir", "os.rename"} and _source is not None:
        for value in args[:2] if event == "os.rename" else args[:1]:
            if isinstance(value, (str, bytes, os.PathLike)) and Path(os.fsdecode(value)).resolve().is_relative_to(_source):
                _deny("source_runtime_mutation")


sys.addaudithook(_audit)


def _finish():
    target = os.environ.get("ASTER_PROMPTFOO_PYTHON_GUARD")
    if target:
        Path(f"{target}.{os.getpid()}.json").write_text(json.dumps({
            "pid": os.getpid(), "entry_script": Path(sys.argv[0]).name,
            "blocked_events": _events, "blocked_attempts": len(_events), **_counts,
        }, indent=2), encoding="utf-8")


atexit.register(_finish)
