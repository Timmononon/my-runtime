from __future__ import annotations

import asyncio
import ctypes
import os
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import psutil


class WindowsJob:
    """Own a Windows process tree, including children created before parent exit."""

    def __init__(self):
        if os.name != "nt":
            raise OSError("Windows Job Objects require Windows")
        from ctypes import wintypes as w
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        self.api.CreateJobObjectW.restype = w.HANDLE
        self.api.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        self.api.SetInformationJobObject.restype = w.BOOL
        self.api.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.api.AssignProcessToJobObject.restype = w.BOOL
        self.api.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
        self.api.TerminateJobObject.restype = w.BOOL
        self.api.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        self.api.OpenProcess.restype = w.HANDLE
        self.api.OpenThread.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        self.api.OpenThread.restype = w.HANDLE
        self.api.ResumeThread.argtypes = [w.HANDLE]
        self.api.ResumeThread.restype = w.DWORD
        self.api.CloseHandle.argtypes = [w.HANDLE]
        self.api.CloseHandle.restype = w.BOOL

        class Basic(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                        ("flags", w.DWORD), ("min_working", ctypes.c_size_t),
                        ("max_working", ctypes.c_size_t), ("active", w.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD)]

        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", ctypes.c_ulonglong * 6),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]

        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = Extended()
        info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def assign_and_resume(self, pid: int):
        import psutil
        handle = self.api.OpenProcess(0x0100 | 0x0001 | 0x0400, False, pid)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not self.api.AssignProcessToJobObject(self.handle, handle):
                raise ctypes.WinError(ctypes.get_last_error())
            # Popen closes its primary thread handle. The suspended process has
            # exactly one thread; reopen it only after the job owns the process.
            threads = psutil.Process(pid).threads()
            if len(threads) != 1:
                raise RuntimeError("Expected one thread in suspended child")
            thread = self.api.OpenThread(0x0002, False, threads[0].id)
            if not thread:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                if self.api.ResumeThread(thread) == 0xFFFFFFFF:
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                self.api.CloseHandle(thread)
        finally:
            self.api.CloseHandle(handle)

    def terminate(self):
        if self.handle and not self.api.TerminateJobObject(self.handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


@dataclass
class ProcessResult:
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    cancelled: bool = False
    timed_out: bool = False
    pid: int | None = None
    artifacts: list[dict] = field(default_factory=list)
    duration: float = 0
    error: str | None = None


class ProcessSupervisor:
    def __init__(self, *, preview_bytes: int = 24_000, output_bytes: int = 20_000_000):
        self.preview_bytes, self.output_bytes = preview_bytes, output_bytes
        self._active: dict[int, threading.Event] = {}
        self._owners: dict[int, tuple[str | None, float]] = {}
        self._pending: set[threading.Event] = set()
        self._lock = threading.Lock()
        self._closed = False

    async def run(self, argv: list[str], *, cwd: Path, artifact_dir: Path,
                  cancel: asyncio.Event | None = None, env: dict[str, str] | None = None,
                  timeout: float = 60, encoding: str = "utf-8",
                  on_cancel: Callable[[], None] | None = None, session_id: str | None = None) -> ProcessResult:
        if not argv or not all(isinstance(arg, str) and "\0" not in arg for arg in argv):
            raise ValueError("argv must be a non-empty string list without NUL")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        stopped = threading.Event()
        with self._lock:
            if self._closed:
                raise RuntimeError("Process supervisor is closed")
            self._pending.add(stopped)
        task = asyncio.create_task(asyncio.to_thread(
            self._run, argv, Path(cwd), Path(artifact_dir), cancel, stopped, env, timeout, encoding, on_cancel, session_id
        ))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            stopped.set()
            # Do not abandon the owner while the OS process is still alive.
            return await asyncio.shield(task)
        finally:
            with self._lock:
                self._pending.discard(stopped)

    def _run(self, argv, cwd, artifact_dir, cancel, stopped, env, timeout, encoding, on_cancel, session_id):
        start = time.monotonic()
        if stopped.is_set() or (cancel and cancel.is_set()):
            return ProcessResult(None, cancelled=True)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        merged_env = os.environ.copy()
        merged_env.update(env or {})
        job = WindowsJob() if os.name == "nt" else None
        process = None
        readers = []
        captures = {}
        result = ProcessResult(None)
        try:
            options = {"creationflags": 0x00000004 | 0x08000000} if job else {"start_new_session": True}
            process = subprocess.Popen(argv, cwd=cwd, env=merged_env, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options)
            result.pid = process.pid
            with self._lock:
                self._active[process.pid] = stopped
                self._owners[process.pid] = (session_id, psutil.Process(process.pid).create_time())
            if job:
                job.assign_and_resume(process.pid)
            for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
                identity = uuid.uuid4().hex
                path = artifact_dir / f"{identity}-{name}.txt"
                capture = {"preview": bytearray(), "size": 0, "written": 0, "error": None}
                captures[name] = (capture, path, identity)

                def drain(stream=pipe, data=capture, target=path):
                    try:
                        with target.open("wb") as output:
                            while block := stream.read(8192):
                                data["size"] += len(block)
                                remaining = max(0, self.output_bytes - data["written"])
                                output.write(block[:remaining])
                                data["written"] += len(block[:remaining])
                                space = max(0, self.preview_bytes - len(data["preview"]))
                                data["preview"].extend(block[:space])
                    except Exception as exc:  # noqa: BLE001 - reader failure must terminate its owner
                        data["error"] = str(exc)
                        stopped.set()
                    finally:
                        stream.close()

                thread = threading.Thread(target=drain, daemon=True)
                thread.start()
                readers.append(thread)
            while process.poll() is None:
                result.cancelled = stopped.is_set() or bool(cancel and cancel.is_set())
                result.timed_out = time.monotonic() - start >= timeout
                if result.cancelled or result.timed_out:
                    if on_cancel:
                        try:
                            on_cancel()
                        except Exception as exc:  # noqa: BLE001 - caller's cleanup must not skip Job termination
                            result.error = f"External cancellation failed: {exc}"
                    if job:
                        job.terminate()
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
                    break
                time.sleep(0.025)
            result.exit_code = process.wait(timeout=10)
        except Exception as exc:  # noqa: BLE001 - always reap owned OS processes on infrastructure errors
            result.error = str(exc)
            if process and process.poll() is None:
                if job:
                    job.terminate()
                else:
                    os.killpg(process.pid, signal.SIGKILL)
                result.exit_code = process.wait(timeout=10)
        finally:
            # A bounded tool never leaves detached descendants running after its
            # root exits. Job close also closes inherited pipes held by children.
            if job:
                job.close()
            elif process:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            for thread in readers:
                thread.join(timeout=10)
                if thread.is_alive():
                    result.error = "Output reader did not finish after process-tree cleanup"
            for name, (capture, path, identity) in captures.items():
                setattr(result, name, bytes(capture["preview"]).decode(encoding, errors="replace"))
                result.artifacts.append({"artifact_id": identity, "path": str(path), "type": "text/plain",
                                         "size": capture["written"], "stream": name,
                                         "total_bytes": capture["size"],
                                         "truncated": capture["size"] > capture["written"]})
                if capture["error"]:
                    result.error = capture["error"]
            if process:
                with self._lock:
                    self._active.pop(process.pid, None)
                    self._owners.pop(process.pid, None)
            result.duration = time.monotonic() - start
        return result

    def inspect(self, session_id):
        """Only this session's managed roots; never enumerate command lines/env."""
        with self._lock:
            owned = dict(self._owners)
        rows = []
        for pid, (owner, created) in owned.items():
            if owner != session_id:
                continue
            try:
                process = psutil.Process(pid)
                if process.create_time() != created:
                    continue
                rows.append({"pid": pid, "create_time": created, "name": process.name(),
                             "status": process.status(), "managed": True})
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return rows

    def stop_owned(self, session_id, pid, expected_create_time):
        """Signal the existing owner; it alone terminates/reaps its original job."""
        with self._lock:
            record = self._owners.get(pid)
            if pid in {0, 1, os.getpid()} or not record or record[0] != session_id:
                raise PermissionError("Only this session's active Runtime-managed process roots can be stopped")
            if record[1] != expected_create_time or psutil.Process(pid).create_time() != expected_create_time:
                raise PermissionError("Process identity changed; refusing PID reuse")
            self._active[pid].set()
        return {"pid": pid, "create_time": expected_create_time, "stop_requested": True,
                "confirmed_exited": False}

    async def close(self):
        with self._lock:
            self._closed = True
            events = list(self._pending)
        for event in events:
            event.set()
        deadline = time.monotonic() + 25
        while self._pending and time.monotonic() < deadline:
            await asyncio.sleep(0.025)
        if self._pending:
            raise TimeoutError("Processes still shutting down")
