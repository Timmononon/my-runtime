import asyncio
import os
import shutil
import sys
import time
from pathlib import Path

import psutil
import pytest

from aster.domain import ToolContext
from aster.platform.processes import ProcessSupervisor
from aster.tools.process import ProcessTool


async def test_dual_stream_bounded_capture(tmp_path):
    supervisor = ProcessSupervisor(preview_bytes=128, output_bytes=4096)
    result = await supervisor.run([sys.executable, "-c",
        "import sys; sys.stdout.write('x'*2000000); sys.stderr.write('y'*2000000)"],
        cwd=tmp_path, artifact_dir=tmp_path / "output", timeout=15)
    assert result.exit_code == 0, result.error
    assert len(result.stdout) == 128 and len(result.stderr) == 128
    assert all(item["size"] == 4096 and item["truncated"] for item in result.artifacts)
    assert all(Path(item["path"]).stat().st_size == 4096 for item in result.artifacts)


@pytest.mark.windows
@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object integration")
async def test_job_cancels_child_and_grandchild(tmp_path):
    supervisor = ProcessSupervisor()
    marker = tmp_path / "grandchild.pid"
    child_script = ("import subprocess,sys,time,pathlib; "
                    "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)']); "
                    f"pathlib.Path({str(marker)!r}).write_text(str(p.pid)); time.sleep(120)")
    root_script = ("import subprocess,sys,time; "
                   f"subprocess.Popen([sys.executable,'-c',{child_script!r}]); time.sleep(120)")
    cancel = asyncio.Event()
    task = asyncio.create_task(supervisor.run([sys.executable, "-c", root_script], cwd=tmp_path,
                                             artifact_dir=tmp_path / "output", cancel=cancel, timeout=30))
    deadline = time.monotonic() + 10
    while not marker.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    assert marker.exists(), (task.result() if task.done() else "child did not start")
    grandchild = int(marker.read_text())
    assert psutil.pid_exists(grandchild)
    cancel.set()
    result = await asyncio.wait_for(task, 15)
    assert result.cancelled and not result.error, result
    assert not psutil.pid_exists(grandchild)
    assert not psutil.pid_exists(result.pid)


async def test_timeout_and_pre_cancel(tmp_path):
    supervisor = ProcessSupervisor()
    result = await supervisor.run([sys.executable, "-c", "import time; time.sleep(60)"],
                                  cwd=tmp_path, artifact_dir=tmp_path / "output", timeout=0.2)
    assert result.timed_out and result.exit_code is not None
    cancel = asyncio.Event()
    cancel.set()
    result = await supervisor.run([sys.executable, "-c", "raise RuntimeError('should not run')"],
                                  cwd=tmp_path, artifact_dir=tmp_path / "output", cancel=cancel)
    assert result.cancelled and result.pid is None


async def test_outer_task_cancellation_reaps_process(tmp_path):
    supervisor = ProcessSupervisor()
    task = asyncio.create_task(supervisor.run([sys.executable, "-c", "import time; time.sleep(60)"],
                                             cwd=tmp_path, artifact_dir=tmp_path / "output"))
    await asyncio.sleep(0.25)
    task.cancel()
    result = await task
    assert result.cancelled and result.pid and not psutil.pid_exists(result.pid)


async def test_supervisor_close_stops_pending_and_rejects_new_processes(tmp_path):
    supervisor = ProcessSupervisor()
    task = asyncio.create_task(supervisor.run([sys.executable, "-c", "import time; time.sleep(60)"],
                                             cwd=tmp_path, artifact_dir=tmp_path / "output"))
    await asyncio.sleep(0)
    await supervisor.close()
    result = await task
    assert result.cancelled
    with pytest.raises(RuntimeError, match="closed"):
        await supervisor.run([sys.executable, "-c", "pass"], cwd=tmp_path, artifact_dir=tmp_path)


@pytest.mark.parametrize("backend,executable", [("powershell7", "pwsh"), ("powershell51", "powershell")])
async def test_real_powershell_unicode(tmp_path, backend, executable):
    if not shutil.which(executable):
        pytest.skip(f"{backend} is not installed")
    tool = ProcessTool(ProcessSupervisor(), shell=True)
    context = ToolContext("s", "r", tmp_path, tmp_path / "artifacts", asyncio.Event())
    result = await tool.execute({"backend": backend, "script": "[Console]::WriteLine('中文 空格')"}, context)
    assert not result.is_error, result.content
    assert "中文 空格" in result.content


async def test_wsl_requires_explicit_distribution(tmp_path):
    tool = ProcessTool(ProcessSupervisor(), shell=True)
    context = ToolContext("s", "r", tmp_path, tmp_path / "artifacts", asyncio.Event())
    result = await tool.execute({"backend": "wsl", "script": "pwd"}, context)
    assert result.is_error and "distribution" in result.content


@pytest.mark.windows
@pytest.mark.skipif(os.name != "nt", reason="WSL integration")
async def test_wsl_process_group_timeout(tmp_path):
    import subprocess
    if not shutil.which("wsl"):
        pytest.skip("WSL unavailable")
    listing = await asyncio.to_thread(subprocess.run, ["wsl", "--list", "--quiet"],
                                     capture_output=True, timeout=10, check=False)
    distributions = listing.stdout.decode("utf-16-le").strip().splitlines()
    if "Ubuntu-22.04" not in distributions:
        pytest.skip("Explicit test distribution Ubuntu-22.04 unavailable")
    warmed = await asyncio.to_thread(subprocess.run,
                                    ["wsl", "--distribution", "Ubuntu-22.04", "--exec", "/bin/true"],
                                    capture_output=True, timeout=30, check=False)
    assert warmed.returncode == 0
    context = ToolContext("s", "wsl", tmp_path, tmp_path / "artifacts", asyncio.Event())
    tool = ProcessTool(ProcessSupervisor(), shell=True)
    result = await tool.execute({"backend": "wsl", "distribution": "Ubuntu-22.04", "linux_cwd": "/tmp",
                                 "script": "printf 'ASTER_PID=%s\\n' \"$$\"; sleep 90", "timeout": 3}, context)
    assert result.is_error and result.data["timed_out"], result.content
    import re
    match = re.search(r"ASTER_PID=(\d+)", result.content)
    assert match, result.content
    checked = await asyncio.to_thread(subprocess.run,
                                     ["wsl", "--distribution", "Ubuntu-22.04", "--exec", "bash", "-c",
                                      f"kill -0 {match.group(1)} 2>/dev/null"],
                                     capture_output=True, timeout=5, check=False)
    assert checked.returncode != 0, "WSL child survived timeout"
