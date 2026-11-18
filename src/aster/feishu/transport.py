"""Application-owned reads and event streaming through the managed native CLI."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import stat
import subprocess
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

import psutil

from aster.feishu.normalize import EVENT_TYPE, remote_id
from aster.platform.processes import WindowsJob
from aster.state import StateError
from aster.tools.feishu import cli_environment

_READY = f"[event] ready event_key={EVENT_TYPE}".encode("ascii")
_VERSION = b"lark-cli version 1.0.100"
_CONFIG_BYTES = 256 * 1024
_RESPONSE_BYTES = 512 * 1024
_RECORD_BYTES = 512 * 1024
_DIAGNOSTIC_BYTES = 64 * 1024
_COMMAND_TIMEOUT = 15
_STARTUP_TIMEOUT = 30
_GENERATION_INTERVAL = 1
_PAGE_SIZE = 100
_PAGE_LIMIT = 10
_SETUP = "Verify the managed Feishu CLI profile, bot permissions and event subscription."
_PROFILE_CHANGED = "The managed Feishu CLI profile changed. Discover the bot and its groups again."
_DISCONNECTED = "The Feishu event stream disconnected."
_STREAM_INTEGRITY = "The Feishu event stream lost data or returned an invalid record."
_START_FAILED = "The managed Feishu CLI process could not start."
_DISCOVERY_FAILED = "The Feishu CLI could not verify the bot and its joined groups."


class FeishuTransportError(StateError):
    def __init__(self, message: str, *, status: str = "setup_required", code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code or status


@dataclass(frozen=True)
class _Profile:
    executable: Path
    executable_signature: tuple[int, ...]
    app_id: str
    brand: str
    user_open_id: str | None
    generation: str


def _file_signature(info: os.stat_result) -> tuple[int, ...]:
    # CPython 3.12 on Windows can report different ctime from stat and fstat.
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON field")
        value[key] = item
    return value


def _invalid_constant(_value: str) -> None:
    raise ValueError("Invalid JSON number")


def _json_object(raw: bytes) -> dict:
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    if not isinstance(value, dict):
        raise TypeError("Expected a JSON object")
    return value


def _identifier(value: object, prefix: str) -> str:
    if not remote_id(value, prefix):
        raise ValueError("Invalid Feishu identifier")
    return value


def _display_name(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Invalid Feishu display name")
    value.encode("utf-8")
    return value.strip()[:200]


def _diagnostic_error(raw: bytes) -> FeishuTransportError | None:
    # 1.0.100 puts a structured envelope after optional plain diagnostics.
    for start in (0, raw.rfind(b"\n{") + 1):
        try:
            envelope = _json_object(raw[start:])
        except (TypeError, ValueError, RecursionError):
            continue
        error = envelope.get("error")
        if envelope.get("ok") is not False or not isinstance(error, dict):
            continue
        if error.get("type") in ("config", "authentication", "authorization", "policy") or error.get(
            "subtype"
        ) in ("failed_precondition", "missing_scope", "app_scope_not_applied", "already_exists"):
            return FeishuTransportError(_SETUP)
    return None


async def _cancel_tasks(*tasks: asyncio.Task) -> None:
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _discard(reader: asyncio.StreamReader) -> None:
    with suppress(OSError):
        while await reader.read(8192):
            pass


async def _reap(process: asyncio.subprocess.Process, job: WindowsJob | None, *, graceful: bool) -> None:
    # EOF is the native consumer's shutdown request. Draining during shutdown
    # lets the CLI exit when stdout was paused for application backpressure.
    if process.stdin:
        process.stdin.close()
    drains = [asyncio.create_task(_discard(pipe)) for pipe in (process.stdout, process.stderr) if pipe]
    try:
        if graceful and process.returncode is None:
            with suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=3)
        if job:
            job.close()  # A detached bus created by this launch still belongs to its Job.
        else:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        async with asyncio.timeout(5):
            await process.wait()
            await asyncio.gather(*drains)
    finally:
        await _cancel_tasks(*drains)


@asynccontextmanager
async def _owned_process(argv: list[str], *, cwd: Path, env: dict[str, str], streaming: bool = False):
    job = None
    process = None
    closing = False

    async def launch():
        nonlocal job, process
        try:
            if os.name == "nt":
                job = WindowsJob()
                options = {"creationflags": 0x00000004 | subprocess.CREATE_NO_WINDOW}  # CREATE_SUSPENDED
            else:
                options = {"start_new_session": True}
            process = await asyncio.create_subprocess_exec(
                *argv, cwd=cwd, env=env, stdin=subprocess.PIPE if streaming else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, limit=_RECORD_BYTES, **options,
            )
            if job:
                try:
                    if closing:
                        process.kill()
                    else:
                        job.assign_and_resume(process.pid)
                except (OSError, RuntimeError, psutil.Error):
                    # Assignment can fail before the Job owns the suspended root.
                    with suppress(ProcessLookupError):
                        process.kill()
                    raise
            return process
        except (OSError, RuntimeError, ValueError, psutil.Error):
            raise FeishuTransportError(_START_FAILED, status="disconnected") from None

    launched = asyncio.create_task(launch())
    try:
        yield await asyncio.shield(launched)
    finally:
        closing = True

        async def cleanup():
            try:
                with suppress(FeishuTransportError):
                    await launched
                if process is not None:
                    await _reap(process, job, graceful=streaming)
            except (OSError, TimeoutError):
                raise FeishuTransportError(_DISCONNECTED, status="disconnected") from None
            finally:
                if job:
                    job.close()

        # Cancellation during CreateProcess or pipe cleanup must not orphan the Job.
        cleanup_task = asyncio.create_task(cleanup())
        cancelled = False
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                cancelled = True
        cleanup_task.result()
        if cancelled:
            raise asyncio.CancelledError


async def _capture(reader: asyncio.StreamReader, limit: int) -> bytes:
    output = bytearray()
    while block := await reader.read(8192):
        if len(output) + len(block) > limit:
            raise FeishuTransportError(_DISCOVERY_FAILED, status="disconnected")
        output.extend(block)
    return bytes(output)


async def _record_line(reader: asyncio.StreamReader, limit: int) -> bytes:
    try:
        line = await reader.readuntil(b"\n")
    except asyncio.IncompleteReadError as error:
        if not error.partial:
            return b""
        raise FeishuTransportError(_STREAM_INTEGRITY, status="disconnected") from None
    except asyncio.LimitOverrunError:
        raise FeishuTransportError(_STREAM_INTEGRITY, status="disconnected") from None
    if len(line) > limit:
        raise FeishuTransportError(_STREAM_INTEGRITY, status="disconnected")
    return line


async def _read_while_watching(operation, monitor: asyncio.Task):
    read = asyncio.create_task(operation)
    try:
        await asyncio.wait((read, monitor), return_when=asyncio.FIRST_COMPLETED)
        if monitor.done():
            monitor.result()
            # Drain intact final records, but don't wait on a stream after its
            # diagnostic pipe has been closed without its stdout closing.
            try:
                async with asyncio.timeout(1):
                    return await read
            except TimeoutError:
                raise FeishuTransportError(_DISCONNECTED, status="disconnected") from None
        return await read
    finally:
        await _cancel_tasks(read)


class FeishuEventTransport:
    """Receive pinned CLI records. Authorization belongs to the task source."""

    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir).resolve()
        self._verified_executable: tuple[int, ...] | None = None

    def _profile(self) -> _Profile:
        executable = self.data_dir / "tools" / "feishu-cli" / ("lark-cli.exe" if os.name == "nt" else "lark-cli")
        config = self.data_dir / "feishu-cli" / "config.json"
        try:
            if any(path.resolve(strict=True) != path for path in (executable, config)):
                raise ValueError("Managed Feishu paths changed")
            executable_info = executable.stat()
            config_info = config.stat()
            if not stat.S_ISREG(executable_info.st_mode) or not stat.S_ISREG(config_info.st_mode):
                raise ValueError("Invalid managed Feishu files")
            with config.open("rb") as source:
                raw = source.read(_CONFIG_BYTES + 1)
                if len(raw) > _CONFIG_BYTES or _file_signature(os.fstat(source.fileno())) != _file_signature(
                    config_info
                ):
                    raise ValueError("Invalid managed Feishu configuration")
            if _file_signature(config.stat()) != _file_signature(config_info):
                raise ValueError("Managed Feishu configuration changed")
            document = _json_object(raw)
            apps = document.get("apps")
            if not isinstance(apps, list) or not apps or any(not isinstance(app, dict) for app in apps):
                raise ValueError("Missing managed Feishu app")
            # Mirror the pinned CLI's name-first, then App ID resolution; an
            # explicit dangling selector never falls back to the first app.
            selected = document.get("currentApp", "")
            if selected is not None and not isinstance(selected, str):
                raise TypeError("Invalid managed Feishu app selector")
            app = next((app for app in apps if app.get("name") == selected), None) if selected else apps[0]
            if app is None:
                app = next((app for app in apps if app.get("appId") == selected), {})
            app_id = _identifier(app.get("appId"), "cli_")
            if any(other is not app and app_id in (other.get("name"), other.get("appId")) for other in apps):
                raise ValueError("Ambiguous managed Feishu app")
            brand = app.get("brand") or "feishu"
            if brand not in ("feishu", "lark"):
                raise ValueError("Invalid managed Feishu brand")
            users = app.get("users", [])
            if users is None:
                users = []
            if not isinstance(users, list) or (users and not isinstance(users[0], dict)):
                raise ValueError("Invalid managed Feishu user")
            user = users[0].get("userOpenId") if users else None
            user_open_id = _identifier(user, "ou_") if user not in (None, "") else None
            signature = _file_signature(executable_info)
            # Bind the selected identity and local file versions. Secret
            # contents and OS credential stores do not enter the fingerprint.
            identity = (str(self.data_dir), app_id, brand, user_open_id, signature, _file_signature(config_info))
            generation = hashlib.sha256(json.dumps(identity).encode("utf-8")).hexdigest()
            return _Profile(executable, signature, app_id, brand, user_open_id, generation)
        except (OSError, TypeError, ValueError, RuntimeError):
            raise FeishuTransportError(_SETUP) from None

    def current_generation(self) -> str:
        """Identify the current local profile without CLI or network calls."""
        return self._profile().generation

    def _require_generation(self, generation: str) -> _Profile:
        try:
            profile = self._profile()
        except FeishuTransportError:
            raise FeishuTransportError(_PROFILE_CHANGED, code="identity_changed") from None
        if profile.generation != generation:
            raise FeishuTransportError(_PROFILE_CHANGED, code="identity_changed")
        return profile

    def _environment(self) -> dict[str, str]:
        env = {key: value for key, value in os.environ.items() if not key.upper().startswith("LARKSUITE_CLI_")}
        env.update(cli_environment(self.data_dir))
        env["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
        return env

    async def _command(self, profile: _Profile, *arguments: str) -> bytes:
        self._require_generation(profile.generation)
        argv = [str(profile.executable), *arguments]
        if arguments != ("--version",):
            argv.append(f"--profile={profile.app_id}")
        try:
            async with _owned_process(argv, cwd=self.data_dir, env=self._environment()) as process:
                stdout = asyncio.create_task(_capture(process.stdout, _RESPONSE_BYTES))
                stderr = asyncio.create_task(_capture(process.stderr, _DIAGNOSTIC_BYTES))
                try:
                    async with asyncio.timeout(_COMMAND_TIMEOUT):
                        output, diagnostics = await asyncio.gather(stdout, stderr)
                        code = await process.wait()
                finally:
                    await _cancel_tasks(stdout, stderr)
        except (OSError, TimeoutError):
            raise FeishuTransportError(_DISCOVERY_FAILED, status="disconnected") from None
        self._require_generation(profile.generation)
        if code:
            raise _diagnostic_error(diagnostics) or FeishuTransportError(_DISCOVERY_FAILED, status="disconnected")
        return output

    async def _verify_version(self, profile: _Profile) -> None:
        if self._verified_executable != profile.executable_signature:
            if (await self._command(profile, "--version")).strip() != _VERSION:
                raise FeishuTransportError("Install the pinned Feishu CLI 1.0.100 using the Aster installer.")
            self._verified_executable = profile.executable_signature

    async def _bot_get(self, profile: _Profile, path: str, *options: str) -> dict:
        result = _json_object(await self._command(profile, "api", "GET", path, "--as", "bot", *options))
        if result.get("ok") is not True or result.get("identity") != "bot" or not isinstance(result.get("data"), dict):
            raise ValueError("Unexpected bot response")
        return result["data"]

    async def discover(self) -> dict:
        profile = self._profile()
        await self._verify_version(profile)
        try:
            auth = _json_object(await self._command(profile, "auth", "status"))
            identities = auth.get("identities")
            if (auth.get("appId") != profile.app_id or auth.get("brand") != profile.brand
                    or auth.get("credentialSource") != "local" or not isinstance(identities, dict)):
                raise FeishuTransportError(_SETUP)
            bot = identities.get("bot")
            user = identities.get("user")
            if not isinstance(bot, dict) or bot.get("available") is not True or not isinstance(user, dict):
                raise FeishuTransportError(_SETUP)
            user_open_id = user.get("openId") or None
            if user_open_id is not None and _identifier(user_open_id, "ou_") != profile.user_open_id:
                raise FeishuTransportError(_SETUP)
            info = await self._bot_get(profile, "/open-apis/bot/v3/info")
            identity = {"app_id": profile.app_id, "bot_open_id": _identifier(info.get("open_id"), "ou_"),
                        "bot_name": _display_name(info.get("app_name")), "generation": profile.generation,
                        "user_open_id": user_open_id}
            groups = await self._joined_groups(profile)
        except (TypeError, ValueError, RecursionError):
            raise FeishuTransportError(_DISCOVERY_FAILED, status="disconnected") from None
        self._require_generation(profile.generation)
        return {"identity": identity, "groups": groups}

    async def _joined_groups(self, profile: _Profile) -> list[dict]:
        groups = {}
        tokens = set()
        params = {"page_size": _PAGE_SIZE}
        for _ in range(_PAGE_LIMIT):
            # This endpoint lists only chats where the bot is a member.
            response = await self._bot_get(profile, "/open-apis/im/v1/chats", "--params", json.dumps(params))
            items, has_more = response.get("items"), response.get("has_more")
            if items is None and has_more is False:
                items = []  # The API can omit an empty items field.
            if not isinstance(items, list) or len(items) > _PAGE_SIZE or type(has_more) is not bool:
                raise ValueError("Invalid joined group page")
            for item in items:
                if not isinstance(item, dict):
                    raise TypeError("Invalid joined group")
                if item.get("chat_mode") not in ("group", "topic"):
                    continue
                chat_id = _identifier(item.get("chat_id"), "oc_")
                groups.setdefault(chat_id, {"chat_id": chat_id,
                                           "name": _display_name(item.get("name") or chat_id)})
            if not has_more:
                return list(groups.values())
            page = response.get("page_token")
            if (not isinstance(page, str) or not 1 <= len(page) <= 2048
                    or not page.isascii() or not page.isprintable() or page in tokens):
                raise ValueError("Invalid joined group page token")
            tokens.add(page)
            params["page_token"] = page
        raise FeishuTransportError("The Feishu joined-group list exceeded the discovery limit.")

    async def events(self, identity: dict, status: Callable[[str, str | None], None]) -> AsyncIterator[dict]:
        """Yield CLI normalized NDJSON after its ready marker until the stream stops."""
        status("connecting", None)
        try:
            profile = self._require_generation(identity.get("generation"))
            if identity.get("app_id") != profile.app_id:
                raise FeishuTransportError(_PROFILE_CHANGED, code="identity_changed")
            await self._verify_version(profile)
            self._require_generation(profile.generation)
            command = [str(profile.executable), "event", "consume", EVENT_TYPE, "--as", "bot",
                       f"--profile={profile.app_id}"]
            async with _owned_process(command, cwd=self.data_dir, env=self._environment(), streaming=True) as process:
                self._require_generation(profile.generation)
                ready = asyncio.Event()
                listening = False

                def update(connected: bool) -> None:
                    nonlocal listening
                    if connected != listening:
                        listening = connected
                        status("listening", None) if connected else status("disconnected", _DISCONNECTED)

                monitor = asyncio.create_task(self._event_diagnostics(process, profile.generation, ready, update))
                try:
                    await _read_while_watching(ready.wait(), monitor)
                    while line := await _read_while_watching(_record_line(process.stdout, _RECORD_BYTES), monitor):
                        self._require_generation(profile.generation)
                        if listening:
                            record = _json_object(line)
                            if record.get("type") != EVENT_TYPE:
                                raise FeishuTransportError(_STREAM_INTEGRITY, status="disconnected")
                            yield record
                    # Both pipes close when CLI exits. Give any final structured
                    # error on stderr priority over the generic EOF diagnostic.
                    with suppress(TimeoutError):
                        await asyncio.wait_for(asyncio.shield(monitor), timeout=1)
                    raise FeishuTransportError(_DISCONNECTED, status="disconnected")
                finally:
                    await _cancel_tasks(monitor)
        except FeishuTransportError as error:
            status(error.status, str(error))
            raise
        except (OSError, TypeError, ValueError, RecursionError):
            status("disconnected", _STREAM_INTEGRITY)
            raise FeishuTransportError(_STREAM_INTEGRITY, status="disconnected") from None

    async def _event_diagnostics(self, process, generation, ready, update) -> None:
        clock = asyncio.get_running_loop().time
        deadline = clock() + _STARTUP_TIMEOUT
        next_check = clock()
        tail = bytearray()
        try:
            while True:
                if clock() >= next_check:
                    self._require_generation(generation)
                    next_check = clock() + _GENERATION_INTERVAL
                if not ready.is_set() and clock() >= deadline:
                    raise FeishuTransportError("The Feishu event stream did not become ready.", status="disconnected")
                timeout = next_check if ready.is_set() else min(next_check, deadline)
                try:
                    async with asyncio.timeout_at(timeout):
                        line = await _record_line(process.stderr, _DIAGNOSTIC_BYTES)
                except TimeoutError:
                    continue
                if not line:
                    if error := _diagnostic_error(tail):
                        raise error
                    if not ready.is_set():
                        raise FeishuTransportError(_DISCONNECTED, status="disconnected")
                    return
                diagnostic = line.rstrip(b"\r\n")
                if diagnostic == _READY and not ready.is_set():
                    self._require_generation(generation)
                    update(True)
                    ready.set()
                elif ready.is_set() and diagnostic.startswith(b"[source] feishu-websocket: "):
                    state = diagnostic.split(b": ", 1)[1].split(b" ", 1)[0]
                    if state == b"connected":
                        update(True)
                    elif state in (b"connecting", b"disconnected", b"reconnecting"):
                        update(False)
                elif diagnostic.startswith((b"WARN: event seq gap ", b"WARN: consume backpressure, ",
                                             b"WARN: dropping oversized frame ", b"WARN: sink write failed, ")):
                    raise FeishuTransportError(_STREAM_INTEGRITY, status="disconnected")
                tail.extend(line)
                if len(tail) > _DIAGNOSTIC_BYTES:
                    del tail[:-_DIAGNOSTIC_BYTES]
        except FeishuTransportError:
            # Stop the consumer promptly even while the receiver handles the last record.
            process.stdin.close()
            raise
