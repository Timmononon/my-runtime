"""Prepare Aster's local workbench for the Electron main process."""
from __future__ import annotations

import argparse
import errno
import json
import os
import re
import socket
import stat
import sys
from pathlib import Path

import httpx
import psutil

import aster
from aster.api import load_api_token
from aster.config import load_config
from aster.maintenance import service_status, start_service, stop_service
from aster.paths import resolve_data_dir


def available_loopback_port(preferred: int = 8765) -> int:
    with socket.socket() as probe:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            probe.bind(("127.0.0.1", preferred))
        except OSError as error:
            if error.errno not in (errno.EADDRINUSE, errno.EACCES):
                raise
            probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def start_workbench_service(data_dir: Path, port: int | None) -> dict:
    automatic = port is None
    if automatic:
        # start_service rechecks ownership under its startup lock.
        current = service_status(data_dir)
        if current["running"] and current.get("kind") == "daemon":
            port = current.get("port")
        else:
            port = available_loopback_port()
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("--port must be between 1 and 65535")

    def start(candidate: int) -> dict:
        argv = [sys.executable, "-m", "aster", "--data-dir", str(data_dir), "daemon", "serve",
                "--host", "127.0.0.1", "--port", str(candidate)]
        return start_service(data_dir, argv, host="127.0.0.1", port=candidate)

    try:
        return start(port)
    except RuntimeError:
        if not automatic:
            raise
        # Another process can acquire the port between the probe and daemon bind.
        replacement = available_loopback_port(port)
        if replacement == port:
            raise
        return start(replacement)


def prepare_launch(data_dir: Path, cwd: Path | None, port: int | None) -> dict[str, str]:
    if cwd is None:
        cwd = load_config(data_dir).cwd
        cwd.mkdir(parents=True, exist_ok=True)
    cwd = cwd.expanduser().resolve(strict=True)
    if not cwd.is_dir():
        raise ValueError("--cwd must be an existing directory")

    static_root = Path(aster.__file__).resolve().parent / "web" / "static"
    index, assets = static_root / "index.html", static_root / "assets"
    if not index.is_file() or not assets.is_dir() or index.resolve() != index or assets.resolve() != assets:
        raise RuntimeError("The installed Aster desktop assets are missing or invalid")

    status = start_workbench_service(data_dir, port)
    host, port = status.get("host"), status.get("port")
    if host not in {"127.0.0.1", "localhost", "::1"} or type(port) is not int or not 1 <= port <= 65535:
        raise RuntimeError("Existing service has an invalid loopback address")
    base = f"http://{'[' + host + ']' if ':' in host else host}:{port}"
    with httpx.Client(base_url=base, timeout=10, trust_env=False, follow_redirects=False) as client:
        health = client.get("/health")
        health.raise_for_status()
        details = health.json()
        if (not status.get("instance_id") or not isinstance(details, dict) or details.get("status") != "ok"
                or details.get("instance_id") != status["instance_id"]):
            raise RuntimeError("The listening server does not match this data directory's daemon")
        response = client.post("/ui/tickets", headers={"Authorization": "Bearer " + load_api_token(data_dir)},
                               json={"cwd": str(cwd)})
        response.raise_for_status()
        body = response.json()
        ticket = body.get("ticket") if isinstance(body, dict) else None
        if not isinstance(ticket, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", ticket):
            raise RuntimeError("The daemon returned an invalid workbench login ticket")
    return {"url": base + "/", "ticket": ticket, "data_dir": str(data_dir), "static_root": str(static_root)}


def stop_installed_daemons(only_data_dir: Path | None = None) -> None:
    """Let setup replace this installation only after its windows and daemons close."""
    resources = Path(__file__).resolve().parent
    python = (resources / "runtime" / "python.exe").resolve()
    desktop = resources.parent / "Aster.exe"
    if resources.name != "resources" or Path(sys.executable).resolve() != python or not desktop.is_file():
        raise RuntimeError("Use the installed Aster runtime for this operation.")
    if only_data_dir is not None:
        only_data_dir = only_data_dir.resolve()

    candidates = []
    for process in psutil.process_iter(["exe"]):
        executable = process.info["exe"]
        if not executable:
            continue
        if Path(executable) == desktop:
            raise RuntimeError("Close the installed Aster desktop before continuing.")
        if Path(executable) == python and process.pid != os.getpid():
            candidates.append(process)

    for process in candidates:
        try:
            argv = process.cmdline()
            if argv[1:4] != ["-m", "aster", "--data-dir"] or argv[5:7] != ["daemon", "serve"]:
                continue
            data_dir = Path(argv[4])
            if only_data_dir is not None and data_dir != only_data_dir:
                continue
            if not data_dir.is_absolute() or not (data_dir / "service.json").is_file():
                raise RuntimeError("Could not verify the installed Aster service.")
            current = service_status(data_dir)
            if not process.is_running():
                continue
            if (not current["running"] or current.get("kind") != "daemon" or current.get("pid") != process.pid
                    or Path(current["executable"]) != python or not current.get("instance_id")):
                raise RuntimeError("Could not verify the installed Aster service.")
            result = stop_service(data_dir, expected_instance_id=current["instance_id"], timeout=25, force=False)
            if not result["stopped"]:
                raise RuntimeError("The installed Aster service is still running.")
        except psutil.NoSuchProcess:
            continue


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--cwd", type=Path)
    parser.add_argument("--port", type=int)
    parser.add_argument("--stop-installed", action="store_true", help="Stop daemons owned by this installation")
    args = parser.parse_args()

    if args.stop_installed:
        if args.cwd is not None or args.port is not None:
            parser.error("--cwd and --port are only valid when opening the desktop")
        try:
            stop_installed_daemons(args.data_dir)
        except (OSError, ValueError, RuntimeError, psutil.Error) as error:
            message = str(error) if isinstance(error, RuntimeError) else type(error).__name__
            print(f"Unable to release the Aster installation: {message}", file=sys.stderr)
            return 2
        return 0

    # Electron captures the ticket through a pipe. Refuse a terminal or file.
    try:
        piped = sys.stdout is not None and stat.S_ISFIFO(os.fstat(sys.stdout.fileno()).st_mode)
    except (OSError, ValueError):
        piped = False
    if not piped:
        parser.error("The desktop launcher requires a stdout pipe")

    try:
        launch = prepare_launch(resolve_data_dir(args.data_dir), args.cwd, args.port)
    except (OSError, ValueError, RuntimeError, httpx.HTTPError) as error:
        print(f"Could not prepare the Aster workbench ({type(error).__name__}).", file=sys.stderr)
        return 2
    print(json.dumps(launch), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
