import asyncio
import json
import os
import sys
import threading
from pathlib import Path

import psutil
import pytest

from aster.mcp import MCPClient, MCPServerConfig


@pytest.mark.windows
@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object ownership")
@pytest.mark.asyncio
async def test_stdio_close_reaps_child_and_grandchild(tmp_path):
    child = tmp_path / "child.py"
    pids = tmp_path / "pids.json"
    child.write_text(
        "import subprocess,sys,time,json,os\nfrom pathlib import Path\nPath(sys.argv[1]+'.started').write_text(str(os.getpid()))\np=subprocess.Popen([sys.executable,'-c','import time; time.sleep(300)'],stdin=subprocess.DEVNULL)\nPath(sys.argv[1]).write_text(json.dumps([os.getpid(),p.pid]))\ntime.sleep(300)\n",
        encoding="utf-8",
    )
    server = tmp_path / "server.py"
    server.write_text(
        "import asyncio,subprocess,sys,json\nfrom pathlib import Path\nfrom mcp.server.fastmcp import FastMCP\nerror=Path(sys.argv[2]).with_suffix('.errors').open('wb')\np=subprocess.Popen([sys.executable,sys.argv[1],sys.argv[2]],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=error)\nPath(sys.argv[2]+'.debug').write_text(json.dumps({'argv':sys.argv,'executable':sys.executable,'child':p.pid}))\nm=FastMCP('tree-test')\n@m.tool()\ndef ping()->str:return 'ok'\nm.run(transport='stdio')\n",
        encoding="utf-8",
    )
    config = MCPServerConfig(
        "tree",
        command=sys.executable,
        args=(str(server), str(child), str(pids)),
        cwd=tmp_path,
        timeout_seconds=5,
        connect_attempts=1,
    )
    owned = []
    client = MCPClient([config])
    try:
        assert await client.list_tools()
        for _ in range(500):
            if pids.exists():
                break
            await asyncio.sleep(0.02)
        assert pids.exists(), (
            pids.with_suffix(".errors").read_text(errors="replace") + Path(str(pids) + ".debug").read_text()
        )
        owned = [psutil.Process(pid) for pid in json.loads(pids.read_text())]
        assert all(process.is_running() for process in owned)
        await client.close()
        _, alive = await asyncio.to_thread(psutil.wait_procs, owned, timeout=5)
        assert not alive, "MCP-owned descendants survived connection close"
    finally:
        await client.close()
        for process in owned:
            if process.is_running():
                process.kill()


@pytest.mark.windows
@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object ownership")
@pytest.mark.asyncio
async def test_stdio_cancel_during_suspended_launch_reclaims_process(monkeypatch):
    from mcp import StdioServerParameters

    from aster.mcp.stdio import managed_windows_stdio
    from aster.platform.processes import WindowsJob

    entered, release = threading.Event(), threading.Event()
    owned = []
    original = WindowsJob.assign_and_resume

    def delayed_assignment(job, pid):
        owned.append(psutil.Process(pid))
        entered.set()
        if not release.wait(5):
            raise TimeoutError("test launch barrier")
        return original(job, pid)

    monkeypatch.setattr(WindowsJob, "assign_and_resume", delayed_assignment)

    async def connect():
        async with managed_windows_stdio(
            StdioServerParameters(command=sys.executable, args=["-c", "import time;time.sleep(300)"])
        ):
            pytest.fail("Cancelled launch must not yield a usable connection")

    task = asyncio.create_task(connect())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        _, alive = await asyncio.to_thread(psutil.wait_procs, owned, timeout=5)
        assert not alive
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
