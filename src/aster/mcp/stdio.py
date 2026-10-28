"""Windows MCP byte transport with fail-closed Job Object ownership."""

from __future__ import annotations

import asyncio
import os
import subprocess
from contextlib import asynccontextmanager, suppress

import anyio
from mcp import types
from mcp.client.stdio import get_default_environment
from mcp.shared.message import SessionMessage

from aster.platform.processes import WindowsJob


@asynccontextmanager
async def managed_windows_stdio(parameters, *, max_message_bytes: int = 16 * 1024 * 1024):
    """Only the owned job is killed. No stdout/stderr content enters audit logs.

    The receive budget covers large tool responses; exceedance closes this
    connection. Callers may configure a larger budget for their own MCP workload.
    """
    if os.name != "nt":
        raise OSError("Managed Windows stdio requires Windows")
    job = WindowsJob()
    process = None
    tasks = []
    read_sender, read_receiver = anyio.create_memory_object_stream(0)
    write_sender, write_receiver = anyio.create_memory_object_stream(0)
    try:

        def launch_owned():
            child = subprocess.Popen(
                [parameters.command, *parameters.args],
                cwd=parameters.cwd,
                env={**get_default_environment(), **(parameters.env or {})},
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
                creationflags=0x00000004 | 0x08000000,
            )
            try:
                job.assign_and_resume(child.pid)
            except BaseException:
                child.kill()  # This unassigned process is still suspended and belongs to this launch.
                child.wait(timeout=5)
                raise
            return child

        # CreateProcess runs off-loop, but cancellation must not abandon the
        # worker between process creation and Job assignment. Retrieve its owned
        # handle first, then let finally close the complete process tree.
        launch = asyncio.create_task(asyncio.to_thread(launch_owned))
        cancelled = False
        while not launch.done():
            try:
                await asyncio.shield(launch)
            except asyncio.CancelledError:
                cancelled = True
        process = launch.result()
        if cancelled:
            raise asyncio.CancelledError()

        async def read_stdout():
            try:
                async with read_sender:
                    while True:
                        line = await asyncio.to_thread(process.stdout.readline, max_message_bytes + 1)
                        if not line:
                            return
                        if len(line) > max_message_bytes:
                            await read_sender.send(ValueError("MCP message exceeds receive budget"))
                            return
                        try:
                            message = types.JSONRPCMessage.model_validate_json(line)
                        except ValueError:
                            await read_sender.send(ValueError("MCP server emitted invalid JSON-RPC"))
                            return
                        await read_sender.send(SessionMessage(message))
            except (OSError, anyio.BrokenResourceError, anyio.ClosedResourceError):
                return

        async def write_stdin():
            def write_all(data):
                pending = memoryview(data)
                while pending:
                    count = process.stdin.write(pending)
                    if not count:
                        raise OSError("MCP stdin was closed")
                    pending = pending[count:]

            try:
                async with write_receiver:
                    async for message in write_receiver:
                        data = (
                            message.message.model_dump_json(by_alias=True, exclude_none=True) + "\n"
                        ).encode("utf-8")
                        if len(data) > max_message_bytes:
                            raise ValueError("MCP request exceeds send budget")
                        await asyncio.to_thread(write_all, data)
            except (OSError, anyio.BrokenResourceError, anyio.ClosedResourceError):
                return

        tasks = [asyncio.create_task(read_stdout()), asyncio.create_task(write_stdin())]
        yield read_receiver, write_sender
    finally:
        # Terminate the job even after a graceful parent exit: children may keep
        # pipes open. Cancelling a reader alone cannot reclaim its OS processes.
        with suppress(OSError):
            job.terminate()
        job.close()
        if process:
            with suppress(OSError, subprocess.TimeoutExpired):
                await asyncio.to_thread(process.wait, timeout=5)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for stream in (read_sender, read_receiver, write_sender, write_receiver):
            await stream.aclose()
        if process:
            for pipe in (process.stdin, process.stdout):
                if pipe:
                    with suppress(OSError):
                        pipe.close()
