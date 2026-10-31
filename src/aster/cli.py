"""CLI and daemon entry points; all work is delegated to RunCoordinator."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import re
import sys
import webbrowser
from pathlib import Path

import httpx
import yaml

from aster.api import create_app, load_api_token
from aster.customization.credentials import CredentialStore
from aster.domain import to_dict
from aster.paths import resolve_data_dir
from aster.service import RunCoordinator
from aster.state.store import TERMINAL


def parser():
    root = argparse.ArgumentParser(prog="aster", description="Local Python agent runtime")
    root.add_argument("--data-dir", type=Path,
                      help="Runtime directory (ASTER_DATA_DIR, then ASTER_HOME, otherwise ~/.aster)")
    commands = root.add_subparsers(dest="command", required=True)
    doctor = commands.add_parser("doctor", help="Probe local dependencies without model requests")
    doctor.add_argument("--json", action="store_true")
    configure = commands.add_parser("configure", help="Store a DeepSeek key in the Runtime's credentials.yaml")
    configure.add_argument("--model", default="deepseek-flash")
    configure.add_argument("--skip-key", action="store_true", help="Update model only, without reading or writing a key")
    run = commands.add_parser("run", help="Run one request or start an interactive session")
    run.add_argument("prompt", nargs="?")
    run.add_argument("--session")
    run.add_argument("--agent", default="default")
    run.add_argument("--project")
    run.add_argument("--cwd", type=Path)
    run.add_argument("--request-id")
    run.add_argument("--command", dest="template_command", help="Expand a template from commands.yaml")
    run.add_argument("--command-arg", action="append", default=[], metavar="NAME=VALUE")
    run.add_argument("--json", action="store_true")
    run.add_argument("--no-stream", action="store_true")
    run.add_argument("--unattended", action="store_true")
    run.add_argument("--attachment", action="append", default=[], metavar="ID", help="Use a registered session attachment")
    run.add_argument("--attach", action="append", default=[], type=Path, metavar="PATH", help="Register a file inside the session workspace")
    recover = commands.add_parser("recover")
    recover.add_argument("run_id")
    recover.add_argument("--allow-configuration-change", action="store_true")
    recover.add_argument("--json", action="store_true")
    reconcile = commands.add_parser("reconcile")
    reconcile.add_argument("execution_id")
    reconcile.add_argument("--content", required=True)
    reconcile.add_argument("--evidence", required=True)
    sessions = commands.add_parser("sessions")
    sessions.add_argument("--json", action="store_true")
    sessions.add_argument("--include-side-chats", action="store_true")
    session = commands.add_parser("session", help="Create append-only branches or close a temporary side chat")
    actions = session.add_subparsers(dest="session_action", required=True)
    for action in ("fork", "side-chat"):
        branch = actions.add_parser(action)
        branch.add_argument("session_id")
        branch.add_argument("--through", type=int, dest="message_index")
        branch.add_argument("--agent", dest="agent_id")
        branch.add_argument("--title")
    actions.add_parser("close").add_argument("session_id")
    mailbox = commands.add_parser("messages", help="Inspect a session mailbox")
    mailbox.add_argument("session_id")
    mailbox.add_argument("--all", action="store_true", dest="include_terminal")
    message = commands.add_parser("message", help="Queue new instructions for a running session")
    message.add_argument("session_id")
    message.add_argument("text")
    message.add_argument("--steer", action="store_true")
    message.add_argument("--attachment", action="append", default=[])
    retract = commands.add_parser("message-retract")
    retract.add_argument("session_id")
    retract.add_argument("message_id")
    commands.add_parser("performance", help="Inspect phases, timings and execution batches").add_argument("run_id")
    web = commands.add_parser("web", help="Start/reuse the local daemon and open the authenticated workbench")
    web.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1", "localhost", "::1"])
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--cwd", type=Path, help="Initial workbench workspace; defaults to the invoking directory")
    web.add_argument("--no-browser", action="store_true", help="Start/reuse only; print the base URL without a login ticket")
    daemon = commands.add_parser("daemon")
    daemon_commands = daemon.add_subparsers(dest="action", required=True)
    for name in ("serve", "start"):
        sub = daemon_commands.add_parser(name)
        sub.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1", "localhost", "::1"])
        sub.add_argument("--port", type=int, default=8765)
    daemon_commands.add_parser("stop")
    daemon_commands.add_parser("status")
    backup = commands.add_parser("backup")
    backup.add_argument("destination", type=Path)
    backup.add_argument("--database-only", action="store_true")
    restore = commands.add_parser("restore")
    restore.add_argument("source", type=Path)
    restore.add_argument("--destination", type=Path, required=True, help="New empty data directory for a bundle")
    auto = commands.add_parser("autostart")
    auto.add_argument("action", choices=["install", "remove"])
    auto.add_argument("--dry-run", action="store_true")
    commands.add_parser("mcp", help="Expose governed tools over MCP stdio")
    artifacts = commands.add_parser("artifacts")
    artifact_commands = artifacts.add_subparsers(dest="artifact_action", required=True)
    prune = artifact_commands.add_parser("prune")
    prune.add_argument("--older-than-days", type=float, default=14)
    prune.add_argument("--apply", action="store_true")
    from aster.customization.cli import add_parsers
    add_parsers(commands)
    from aster.evaluation.cli import add_parsers as add_evaluation_parsers
    add_evaluation_parsers(commands)
    from aster.evaluation.bfcl_cli import add_parsers as add_bfcl_parsers
    add_bfcl_parsers(commands)
    return root


def _print(value):
    print(json.dumps(to_dict(value), ensure_ascii=False, indent=2))


async def _prompt(text):
    return await asyncio.to_thread(input, text)


class ConsoleSink:
    def __init__(self, *, approve, answer, stream=True, unattended=False):
        self.approve, self.answer = approve, answer
        self.stream, self.unattended = stream, unattended
        self.seen = set()
        self.streamed = False

    async def __call__(self, kind, payload):
        if kind == "model.text.delta" and self.stream:
            print(payload.get("text", ""), end="", flush=True)
            self.streamed = True
        if kind in {"approval.waiting", "approval.requested"}:
            request_id = payload["request_id"]
            if request_id in self.seen:
                return
            self.seen.add(request_id)
            allow = False
            if not self.unattended:
                print("\nApproval:", payload.get("tool_name"), json.dumps(payload.get("arguments", {}), ensure_ascii=False), file=sys.stderr)
                allow = (await _prompt("Allow this call? [y/N] ")).strip().lower() == "y"
            result = self.approve(request_id, allow)
            if asyncio.iscoroutine(result):
                await result
        if kind in {"question.waiting", "question.requested"}:
            request_id = payload["request_id"]
            if request_id in self.seen:
                return
            self.seen.add(request_id)
            text = "" if self.unattended else await _prompt("\n" + str(payload.get("question", payload)) + "\n> ")
            result = self.answer(request_id, text)
            if asyncio.iscoroutine(result):
                await result


def _show_outcome(result, sink, *, json_output=False):
    value = to_dict(result)
    if json_output:
        _print(value)
    else:
        if sink.streamed:
            print()
        elif value.get("text"):
            print(value["text"])
        if value["status"] != "completed":
            print(f"[{value['status']}] {value.get('error') or ''}", file=sys.stderr)
        print(f"Session: {value['session_id']}  Run: {value['run_id']}", file=sys.stderr)
    return 0 if value["status"] == "completed" else 2


async def _local_work(args, coordinator):
    from aster.customization.cli import handle_command
    extension = await handle_command(args, coordinator)
    if extension is not None:
        return extension
    if args.command == "sessions":
        _print({"sessions": coordinator.store.list_sessions(include_side_chats=args.include_side_chats)})
        return 0
    if args.command == "session":
        if args.session_action == "close":
            coordinator.close_side_chat(args.session_id)
            _print({"session_id": args.session_id, "closed": True})
        else:
            _print(coordinator.branch(args.session_id, message_index=args.message_index, agent_id=args.agent_id,
                                       title=args.title, side_chat=args.session_action == "side-chat"))
        return 0
    if args.command == "messages":
        _print({"messages": coordinator.store.list_mailbox(args.session_id, include_terminal=args.include_terminal)})
        return 0
    if args.command == "message":
        _print({"message_id": coordinator.inject(args.session_id, args.text, attachments=args.attachment, steer=args.steer)})
        return 0
    if args.command == "message-retract":
        _print({"retracted": coordinator.store.retract_message(args.session_id, args.message_id)})
        return 0
    if args.command == "performance":
        from aster.engine.telemetry import performance
        _print(performance(coordinator.store, args.run_id))
        return 0
    if args.command == "reconcile":
        from aster.domain import ToolResult
        coordinator.store.reconcile_tool(args.execution_id, ToolResult(args.content), evidence=args.evidence)
        _print({"status": "reconciled", "execution_id": args.execution_id})
        return 0
    sink = ConsoleSink(approve=coordinator.approvals.resolve, answer=coordinator.questions.resolve,
                       stream=not getattr(args, "json", False) and not getattr(args, "no_stream", False),
                       unattended=getattr(args, "unattended", False))
    if args.command == "recover":
        result = await coordinator.recover(args.run_id, emit=sink,
                                           allow_configuration_change=args.allow_configuration_change)
        return _show_outcome(result, sink, json_output=args.json)
    sid = args.session or coordinator.create_session(args.cwd or Path.cwd(), agent_id=args.agent, project_id=args.project)["id"]
    attachment_ids = list(args.attachment)
    for path in args.attach:
        attachment_ids.append(coordinator.attachments.register_path(sid, path)["attachment_id"])
    interactive = args.prompt is None and args.template_command is None
    while True:
        text = await _prompt("aster> ") if interactive else args.prompt or args.template_command
        if interactive and text.strip() in {"/exit", "/quit"}:
            return 0
        if not text.strip():
            if interactive:
                continue
            raise ValueError("Prompt is empty")
        run = await coordinator.submit(sid, text, request_id=args.request_id, unattended=args.unattended,
                                        command=args.template_command, command_args=_command_args(args.command_arg), attachments=attachment_ids)
        attachment_ids = []
        coordinator.add_sink(run["id"], sink)
        try:
            result = await coordinator.wait(run["id"])
        except asyncio.CancelledError:
            coordinator.cancel(run["id"])
            await coordinator.wait(run["id"])
            raise
        code = _show_outcome(result, sink, json_output=args.json)
        sink.streamed = False
        if not interactive:
            return code


async def _remote_work(args, status):
    host = status["host"]
    host = f"[{host}]" if ":" in host else host
    url = f"http://{host}:{status['port']}"
    async with httpx.AsyncClient(base_url=url, headers={"Authorization": "Bearer " + load_api_token(args.data_dir)}, timeout=None, trust_env=False) as client:
        async def request(method, path, **kwargs):
            response = await client.request(method, path, **kwargs)
            if response.is_error:
                raise RuntimeError(f"Local API {response.status_code}: {response.text}")
            return response.json()

        if args.command == "skills":
            if args.skill_action == "list":
                _print(await request("GET", "/customization/skills"))
                return 0
            sid = getattr(args, "session", None) or (await request("POST", "/sessions", json={}))['id']
            if args.skill_action == "preview":
                source = args.source if args.source.startswith("bundled:") else str(Path(args.source).resolve())
                arguments = {"source": source}
            else:
                key = "plan_id" if args.skill_action == "install" else "name"
                arguments = {key: getattr(args, key), "expected_digest": args.digest}
            value = await request("POST", f"/customization/sessions/{sid}/skill_package_{args.skill_action}",
                                  json={"arguments": arguments, "approved": args.skill_action != "preview"})
            _print(value)
            return 2 if value.get("is_error") else 0
        if args.command == "memory":
            arguments = json.loads(args.arguments)
            if not isinstance(arguments, dict):
                raise ValueError("Memory arguments must be a JSON object")
            value = await request("POST", f"/customization/sessions/{args.session}/memory_{args.memory_action}",
                                  json={"arguments": arguments, "approved": args.memory_action not in {"recall", "history"}})
            _print(value)
            return 2 if value.get("is_error") else 0

        if args.command == "sessions":
            _print(await request("GET", "/sessions", params={"include_side_chats": args.include_side_chats}))
            return 0
        if args.command == "session":
            body = {} if args.session_action == "close" else {"message_index": args.message_index, "agent_id": args.agent_id, "title": args.title}
            _print(await request("POST", f"/sessions/{args.session_id}/{args.session_action}", json=body))
            return 0
        if args.command == "messages":
            _print(await request("GET", f"/sessions/{args.session_id}/mailbox", params={"include_terminal": args.include_terminal}))
            return 0
        if args.command == "message":
            _print(await request("POST", f"/sessions/{args.session_id}/messages", json={"text": args.text, "steer": args.steer, "attachments": args.attachment}))
            return 0
        if args.command == "message-retract":
            _print(await request("DELETE", f"/sessions/{args.session_id}/mailbox/{args.message_id}"))
            return 0
        if args.command == "performance":
            _print(await request("GET", f"/runs/{args.run_id}/performance"))
            return 0
        if args.command == "reconcile":
            _print(await request("POST", f"/executions/{args.execution_id}/reconcile", json={"content": args.content, "evidence": args.evidence}))
            return 0
        sink = ConsoleSink(approve=lambda rid, allow: request("POST", f"/approvals/{rid}/decision", json={"approved": allow}),
                           answer=lambda rid, answer: request("POST", f"/questions/{rid}/answer", json={"answer": answer}),
                           stream=not getattr(args, "json", False) and not getattr(args, "no_stream", False),
                           unattended=getattr(args, "unattended", False))
        recovery = None
        if args.command == "recover":
            run_id = args.run_id
            recovery = asyncio.create_task(request("POST", f"/runs/{run_id}/recover", json={"allow_configuration_change": args.allow_configuration_change}))
            cursor = 0
        else:
            sid = args.session or (await request("POST", "/sessions", json={"cwd": str(args.cwd or Path.cwd()),
                      "agent_id": args.agent, "project_id": args.project}))["id"]
            attachment_ids = list(args.attachment)
            for path in args.attach:
                attachment_ids.append((await request("POST", f"/sessions/{sid}/attachments/from-path", json={"path": str(path.resolve())}))["attachment_id"])
            text = args.prompt or args.template_command or await _prompt("aster> ")
            if args.prompt is None and args.template_command is None and text.strip() in {"/quit", "/exit"}:
                return 0
            run_id = (await request("POST", f"/sessions/{sid}/runs", json={"text": text, "request_id": args.request_id,
                          "unattended": args.unattended, "command": args.template_command,
                          "command_args": _command_args(args.command_arg), "attachments": attachment_ids}))["id"]
            cursor = 0
        try:
            while True:
                response = await client.get(f"/runs/{run_id}/events", params={"after": cursor, "follow": "false"})
                response.raise_for_status()
                for line in response.text.splitlines():
                    if line.startswith("data: "):
                        event = json.loads(line[6:])
                        cursor = event["event_id"]
                        await sink(event["type"], event["payload"])
                run = await request("GET", f"/runs/{run_id}")
                if run["status"] in TERMINAL and (recovery is None or recovery.done()):
                    if recovery:
                        result = await recovery
                    else:
                        result = {"session_id": run["session_id"], "run_id": run_id, "status": run["status"],
                                  "text": run["answer"], "error": run["error"]}
                    code = _show_outcome(result, sink, json_output=args.json)
                    if recovery is not None or args.prompt is not None or args.template_command is not None:
                        return code
                    text = await _prompt("aster> ")
                    if text.strip() in {"/quit", "/exit"}:
                        return 0
                    run_id = (await request("POST", f"/sessions/{sid}/runs", json={"text": text,
                                                       "unattended": args.unattended}))["id"]
                    cursor, sink.streamed = 0, False
                    continue
                await asyncio.sleep(0.15)
        except asyncio.CancelledError:
            await request("POST", f"/runs/{run_id}/cancel")
            raise


async def _serve(args):
    import uvicorn

    from aster.maintenance import ServiceLease
    with ServiceLease(args.data_dir, host=args.host, port=args.port) as lease:
        coordinator = RunCoordinator(args.data_dir, instance_id=lease.instance_id)
        app = create_app(coordinator, allowed_origins={f"http://127.0.0.1:{args.port}", f"http://localhost:{args.port}", f"http://[::1]:{args.port}"})
        # Disconnected browser/SSE transports can stall Windows wait_closed.
        # Bound only HTTP draining; Uvicorn Config is the host override point.
        # Coordinator's existing tool cancellation/resource cleanup still runs.
        server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, loop="asyncio", access_log=False,
                                               proxy_headers=False, timeout_graceful_shutdown=5, log_level="warning"))

        async def stop_monitor():
            while not server.should_exit:
                if lease.stop_requested():
                    server.should_exit = True
                    return
                await asyncio.sleep(0.2)

        monitor = asyncio.create_task(stop_monitor())
        try:
            await server.serve()
        finally:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
            if not coordinator._closed:
                await coordinator.close()
    return 0


def _launch_web(args):
    from aster.maintenance import start_service

    if not 1 <= args.port <= 65535:
        raise ValueError("--port must be between 1 and 65535")
    cwd = (args.cwd or Path.cwd()).expanduser().resolve(strict=True)
    if not cwd.is_dir():
        raise ValueError("--cwd must be an existing directory")
    argv = [sys.executable, "-m", "aster", "--data-dir", str(args.data_dir), "daemon", "serve",
            "--host", args.host, "--port", str(args.port)]
    status = start_service(args.data_dir, argv, host=args.host, port=args.port)
    host, port = status.get("host"), status.get("port")
    if host not in {"127.0.0.1", "localhost", "::1"} or not isinstance(port, int) or not 1 <= port <= 65535:
        raise RuntimeError("Existing service has an invalid loopback address")
    base = f"http://{'[' + host + ']' if ':' in host else host}:{port}"
    with httpx.Client(base_url=base, timeout=10, trust_env=False, follow_redirects=False) as client:
        health = client.get("/health")
        health.raise_for_status()
        if health.json().get("instance_id") != status.get("instance_id"):
            raise RuntimeError("The listening server does not match this data directory's daemon")
        available = client.get("/")
        if available.status_code != 200:
            raise RuntimeError("Workbench is unavailable; install the built web assets and restart the daemon")
        opened = False
        if not args.no_browser:
            response = client.post("/ui/tickets", headers={"Authorization": "Bearer " + load_api_token(args.data_dir)},
                                   json={"cwd": str(cwd)})
            response.raise_for_status()
            ticket = response.json().get("ticket", "")
            if not isinstance(ticket, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", ticket):
                raise RuntimeError("The daemon returned an invalid workbench login ticket")
            # Fragment never reaches HTTP logs; never print it or the API token.
            opened = webbrowser.open(base + "/#ticket=" + ticket, new=2)
            if not opened:
                raise RuntimeError("Browser could not be opened; run aster web again after configuring a browser")
    _print({"url": base + "/", "started": not status.get("already_running", False),
            "reused": bool(status.get("already_running", False)), "browser_opened": opened,
            "instance_id": status["instance_id"], "cwd": str(cwd)})
    return 0


async def _foreground(args):
    from aster.maintenance import ServiceLease, service_status
    status = service_status(args.data_dir)
    if status["running"] and status.get("kind") == "daemon":
        return await _remote_work(args, status)
    with ServiceLease(args.data_dir, host=None, port=None):
        coordinator = RunCoordinator(args.data_dir)
        try:
            await coordinator.start()
            return await _local_work(args, coordinator)
        finally:
            await coordinator.close()


async def _mcp(args):
    from aster.maintenance import ServiceLease
    from aster.mcp.server import create_mcp_server, serve_stdio
    from aster.tools.registry import Registry
    with ServiceLease(args.data_dir, host=None, port=None):
        coordinator = RunCoordinator(args.data_dir)
        try:
            server = create_mcp_server(Registry(coordinator.base_tools), coordinator.execute_tool)
            await serve_stdio(server)
        finally:
            await coordinator.close()
    return 0


def _configure_key(data_dir, key):
    """Use the same binding as Web settings; never write a second legacy key."""
    from aster.maintenance import ServiceLease, service_status
    from aster.providers.profiles import ProviderProfiles
    from aster.state import Store

    status = service_status(data_dir)
    if status.get("running") and status.get("kind") == "daemon":
        host = status.get("host", "127.0.0.1")
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Daemon address must be loopback")
        address = "[::1]" if host == "::1" else host
        with httpx.Client(base_url=f"http://{address}:{int(status['port'])}", timeout=20, trust_env=False,
                          follow_redirects=False) as client:
            health = client.get("/health")
            health.raise_for_status()
            if health.json().get("instance_id") != status.get("instance_id"):
                raise ValueError("Daemon identity does not match the data directory")
            headers = {"Authorization": "Bearer " + load_api_token(data_dir)}
            response = client.get("/providers", headers=headers)
            response.raise_for_status()
            profile = next(row for row in response.json()["providers"] if row["id"] == "deepseek")
            response = client.patch("/providers/deepseek", headers=headers,
                                    json={"expected_revision": profile["revision"], "api_key": key})
            response.raise_for_status()
    else:
        with ServiceLease(data_dir, host=None, port=None):
            store = Store(Path(data_dir) / "runtime.db")
            try:
                profiles = ProviderProfiles(store, CredentialStore(data_dir=data_dir))
                profiles.update("deepseek", expected_revision=profiles.get("deepseek")["revision"], api_key=key)
            finally:
                store.close()


def main(argv=None):
    args = parser().parse_args(argv)
    from aster import maintenance
    try:
        if args.command == "bfcl":
            from aster.evaluation.bfcl_cli import handle_command as bfcl
            return asyncio.run(bfcl(args))
        args.data_dir = resolve_data_dir(args.data_dir)
        if args.command == "evaluate":
            from aster.evaluation.cli import handle_command as evaluate
            return asyncio.run(evaluate(args))
        if args.command == "run" and args.template_command and args.prompt is not None:
            raise ValueError("A positional prompt and --command are mutually exclusive")
        if args.command == "doctor":
            _print(maintenance.doctor(args.data_dir))
            return 0
        if args.command == "configure":
            from aster.platform.filesystem import atomic_write
            args.data_dir.mkdir(parents=True, exist_ok=True)
            config_path = args.data_dir / "config.yaml"
            settings = yaml.safe_load(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
            settings = settings or {}
            if not args.skip_key:
                key = getpass.getpass("DeepSeek API key (saved in credentials.yaml): ").strip()
                _configure_key(args.data_dir, key)
            settings["model"] = args.model
            atomic_write(config_path, yaml.safe_dump(settings, allow_unicode=True, sort_keys=False).encode())
            print("Configuration saved. Credentials are not written to config.yaml; API keys use credentials.yaml.")
            return 0
        if args.command == "daemon":
            if args.action == "serve":
                return asyncio.run(_serve(args))
            if args.action == "start":
                argv = [sys.executable, "-m", "aster", "--data-dir", str(args.data_dir), "daemon", "serve", "--host", args.host, "--port", str(args.port)]
                _print(maintenance.start_service(args.data_dir, argv, host=args.host, port=args.port))
            elif args.action == "stop":
                _print(maintenance.stop_service(args.data_dir))
            else:
                _print(maintenance.service_status(args.data_dir))
            return 0
        if args.command == "web":
            return _launch_web(args)
        if args.command == "backup":
            fn = maintenance.backup_database if args.database_only else maintenance.backup_bundle
            _print({"backup": str(fn(args.data_dir, args.destination))})
            return 0
        if args.command == "restore":
            _print(maintenance.restore_bundle(args.source, args.destination))
            return 0
        if args.command == "autostart":
            if args.action == "install":
                argv = [sys.executable, "-m", "aster", "--data-dir", str(args.data_dir), "daemon", "serve"]
                _print(maintenance.install_autostart(args.data_dir, argv, dry_run=args.dry_run))
            else:
                _print(maintenance.remove_autostart(args.data_dir, dry_run=args.dry_run))
            return 0
        if args.command == "artifacts":
            if args.older_than_days < 0:
                raise ValueError("--older-than-days must be non-negative")
            _print(maintenance.prune_orphan_artifacts(args.data_dir, min_age_seconds=args.older_than_days * 86400,
                                                     dry_run=not args.apply))
            return 0
        if args.command == "mcp":
            return asyncio.run(_mcp(args))
        return asyncio.run(_foreground(args))
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130
    except (RuntimeError, ValueError, OSError, httpx.HTTPError) as error:
        print(f"aster: {error}", file=sys.stderr)
        return 2


def _command_args(values):
    result = {}
    for value in values:
        name, separator, text = value.partition("=")
        if not separator or not name:
            raise ValueError("--command-arg must be NAME=VALUE")
        if name in result:
            raise ValueError("Duplicate command argument: " + name)
        result[name] = text
    return result
