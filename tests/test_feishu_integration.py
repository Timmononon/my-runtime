"""CLI integration uses disposable state, offline subprocesses and real policy."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from aster.customization.discovery import ToolSearchTool
from aster.domain import ToolCall, ToolContext
from aster.engine.dispatcher import Dispatcher
from aster.platform.processes import ProcessResult
from aster.policy import ApprovalBroker, PermissionPolicy
from aster.state import Store
from aster.tools.factory import ToolFactory
from aster.tools.feishu import FeishuCLITool
from aster.tools.registry import Registry

ROOT = Path(__file__).resolve().parents[1]
WRITE = ["im", "+messages-send", "--chat-id", "oc_offline", "--text", "Approved offline message"]
READ = ["docs", "+fetch", "--doc", "offline-document-token"]


class RecordingCLI:
    def __init__(self, *, unknown=False):
        self.calls = []
        self.unknown = unknown

    async def run(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if self.unknown:
            return ProcessResult(1, stderr=json.dumps({
                "ok": False, "identity": "user",
                "error": {"type": "api", "code": 999999, "message": "Response interrupted"},
            }), pid=321)
        return ProcessResult(0, stdout=json.dumps({
            "ok": True, "identity": "user", "data": {"message_id": "om_offline"},
        }), pid=321)


@pytest.fixture
def runtime(tmp_path):
    store = Store(tmp_path / "runtime.db")
    sid = store.create_session(tmp_path)["id"]
    run, _ = store.create_run(sid)
    context = ToolContext(sid, run["id"], tmp_path, tmp_path / "artifacts", asyncio.Event())
    try:
        yield store, context
    finally:
        context.cancel.set()
        store.close()


def dispatcher(runtime, *, unknown=False):
    store, context = runtime
    runner = RecordingCLI(unknown=unknown)
    tool = FeishuCLITool(runner, context.cwd / "state", executable=Path(sys.executable))
    broker = ApprovalBroker(store, timeout_seconds=3)
    engine = Dispatcher(Registry([tool]), store, PermissionPolicy(), broker)
    return engine, broker, runner


async def test_factory_registers_discoverable_cli_without_launch_or_network(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Tool construction must not launch or connect")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr("httpx.AsyncClient", forbidden)
    data = tmp_path / "uncreated-state"
    factory = ToolFactory(data, browser_enabled=False)
    try:
        registry = Registry(factory.tools)
        tool = registry.get("feishu_cli")
        assert tool is not None
        context = ToolContext("offline", "offline", tmp_path, tmp_path / "artifacts", asyncio.Event(),
                              services={"registry": registry, "active_tools": set()})
        result = await ToolSearchTool().execute({"query": "select:feishu_cli"}, context)
        assert [item["name"] for item in result.data["tools"]] == ["feishu_cli"]
        chinese = await ToolSearchTool().execute({"query": "飞书"}, context)
        assert "feishu_cli" in {item["name"] for item in chinese.data["tools"]}
        assert "feishu_cli" in context.services["active_tools"]
        assert not data.exists()
    finally:
        await factory.close()


@pytest.mark.parametrize("approved", [False, True])
async def test_remote_write_waits_for_real_approval_and_only_dispatches_approved_version(runtime, approved):
    store, context = runtime
    engine, broker, runner = dispatcher(runtime)
    waiting = asyncio.Event()
    request = {}

    async def emit(kind, payload):
        if kind == "approval.waiting":
            request.update(payload)
            waiting.set()

    context.emit = emit
    task = asyncio.create_task(engine.execute([ToolCall("send", "feishu_cli", {"argv": WRITE})], context))
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        assert not runner.calls
        assert store.tool_executions(context.run_id) == []
        assert request["arguments"] == {"argv": WRITE}
        assert broker.resolve(request["request_id"], approved)
        result = (await task)[0]
        if approved:
            assert not result.is_error, result.content
            assert len(runner.calls) == 1
            assert runner.calls[0][1]["persist_output"] is False
            entry, = store.tool_executions(context.run_id)
            assert entry["risk"] == "external" and entry["state"] == "committed"
            assert not entry["replay_safe"]
        else:
            assert result.is_error and result.outcome == "not_executed"
            assert not runner.calls and store.tool_executions(context.run_id) == []
    finally:
        context.cancel.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_unknown_remote_write_blocks_subsequent_read_and_new_request_id(runtime):
    store, context = runtime
    engine, broker, runner = dispatcher(runtime, unknown=True)

    async def emit(kind, payload):
        if kind == "approval.waiting":
            broker.resolve(payload["request_id"], True)

    context.emit = emit
    first = (await engine.execute([ToolCall("first", "feishu_cli", {"argv": WRITE})], context))[0]
    assert first.is_error and first.outcome == "unknown", first.content
    assert store.tool_executions(context.run_id)[0]["state"] == "outcome_unknown"
    for argv in (READ, WRITE):
        blocked = (await engine.execute([ToolCall("new-id", "feishu_cli", {"argv": argv})], context))[0]
        assert blocked.is_error and blocked.outcome == "not_executed"
    assert len(runner.calls) == 1


async def test_reader_worker_cannot_use_external_cli_command(runtime):
    store, context = runtime
    engine, _, runner = dispatcher(runtime)
    context.services["task_guard"] = lambda: {"role": "reader"}
    result = (await engine.execute([ToolCall("write", "feishu_cli", {"argv": WRITE})], context))[0]
    assert result.is_error and result.error_category == "permission"
    assert not runner.calls and store.tool_executions(context.run_id) == []


async def test_active_skill_cannot_be_bypassed_by_cli_read_classification(runtime):
    store, context = runtime
    engine, _, runner = dispatcher(runtime)
    context.services["skill_scope"] = SimpleNamespace(check_tool=lambda name: name != "feishu_cli")
    result = (await engine.execute([ToolCall("read", "feishu_cli", {"argv": READ})], context))[0]
    assert result.is_error and result.error_category == "permission"
    assert not runner.calls and store.tool_executions(context.run_id) == []


async def test_hook_cannot_turn_read_into_write_without_fresh_approval(runtime):
    store, context = runtime
    engine, broker, runner = dispatcher(runtime)
    requests = []

    class ChangeToWrite:
        async def run(self, kind, payload, context):
            if kind == "PreToolUse":
                payload["arguments"]["argv"] = list(WRITE)
            return SimpleNamespace(allow=True)

    async def emit(kind, payload):
        if kind == "approval.waiting":
            requests.append(payload)
            broker.resolve(payload["request_id"], False)

    context.emit = emit
    context.services["hooks"] = ChangeToWrite()
    result = (await engine.execute([ToolCall("initial-read", "feishu_cli", {"argv": READ})], context))[0]
    assert result.is_error and result.outcome == "not_executed"
    assert len(requests) == 1 and requests[0]["arguments"]["argv"] == WRITE
    assert not runner.calls and store.tool_executions(context.run_id) == []


@pytest.mark.parametrize("shell_name", ["powershell.exe", "pwsh.exe"])
@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.skipif(sys.platform != "win32", reason="Windows installation script")
def test_installer_dry_run_honors_data_directory_without_writing_or_authorizing(tmp_path, shell_name, explicit):
    shell = shutil.which(shell_name)
    if not shell:
        pytest.skip(f"{shell_name} unavailable")
    chosen = tmp_path / "explicit 目录" if explicit else tmp_path / "environment 目录"
    environment = {**os.environ, "ASTER_DATA_DIR": str(tmp_path / "environment 目录"),
                   "ASTER_HOME": str(tmp_path / "legacy unselected")}
    args = ["-DataDir", str(chosen)] if explicit else []
    result = subprocess.run([
        shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
        str(ROOT / "scripts/Install-FeishuCLI.ps1"), "-DryRun", *args,
    ], env=environment, capture_output=True, check=False, timeout=15)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    payload = json.loads(result.stdout.decode("utf-8"))
    assert Path(payload["DataDirectory"]) == chosen
    assert Path(payload["Executable"]) == chosen / "tools/feishu-cli/lark-cli.exe"
    assert Path(payload["AuthConfigDirectory"]) == chosen / "feishu-cli"
    assert payload["Version"] == "1.0.100" and payload["AuthorizesAccount"] is False
    assert payload["ChangesPath"] is False
    assert not chosen.exists() and not Path(environment["ASTER_HOME"]).exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows installation script")
@pytest.mark.parametrize("shell_name", ["powershell.exe", "pwsh.exe"])
def test_installer_selects_one_native_command_and_atomically_replaces_existing_cli(tmp_path, shell_name):
    """No registry call: only the npm download boundary is replaced offline."""
    shell, node = shutil.which(shell_name), shutil.which("node.exe")
    compiler = shutil.which("powershell.exe")
    if not shell or not node or not compiler:
        pytest.skip("Both PowerShell hosts and native Node are needed for the installer regression")
    binary = tmp_path / "offline-cli.exe"
    builder = tmp_path / "build-offline-cli.ps1"
    builder.write_text(r'''param($Destination)
$ErrorActionPreference='Stop'
Add-Type -OutputAssembly $Destination -OutputType ConsoleApplication -TypeDefinition @'
using System;
public class OfflineCli {
    public static int Main(string[] args) {
        if (args.Length == 1 && args[0] == "--version") {
            Console.WriteLine("lark-cli v1.0.100");
            return 0;
        }
        return 9;
    }
}
'@
''', encoding="utf-8")
    built = subprocess.run([compiler, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                            "-File", str(builder), "-Destination", str(binary)],
                           capture_output=True, check=False, timeout=30)
    assert built.returncode == 0, built.stderr.decode("utf-8", errors="replace")
    fake_npm = tmp_path / "offline-npm.ps1"
    fake_npm.write_text(r'''$ErrorActionPreference = 'Stop'
$prefix = $args[[array]::IndexOf($args, '--prefix') + 1]
$spec = @($args | Where-Object { $_ -like '@larksuite/cli@*' })[0]
$version = $spec.Substring('@larksuite/cli@'.Length)
$package = Join-Path $prefix 'node_modules\@larksuite\cli'
[void][IO.Directory]::CreateDirectory((Join-Path $package 'scripts'))
@{name='@larksuite/cli'; version=$version} | ConvertTo-Json |
    Set-Content -LiteralPath (Join-Path $package 'package.json') -Encoding UTF8
'{}' | Set-Content -LiteralPath (Join-Path $prefix 'package-lock.json') -Encoding UTF8
@'
const fs = require('fs');
const path = require('path');
const dest = path.join(__dirname, '..', 'bin');
fs.mkdirSync(dest, { recursive: true });
fs.copyFileSync(process.env.ASTER_FEISHU_TEST_BINARY, path.join(dest, 'lark-cli.exe'));
'@ | Set-Content -LiteralPath (Join-Path $package 'scripts\install.js') -Encoding UTF8
$global:LASTEXITCODE = 0
''', encoding="utf-8")
    harness = tmp_path / "installer-multiple-commands.ps1"
    harness.write_text(r'''param($Installer, $DataDirectory, $Node, $Npm, $Version)
$ErrorActionPreference = 'Stop'
Import-Module Microsoft.PowerShell.Utility
$global:ASTER_FEISHU_TEST_NODE = $Node
$global:ASTER_FEISHU_TEST_NPM = $Npm
function global:Get-Command {
    [CmdletBinding()] param([string]$Name, [string]$CommandType)
    if ($Name -eq 'node.exe') {
        return @([pscustomobject]@{Source=$global:ASTER_FEISHU_TEST_NODE},
                 [pscustomobject]@{Source='Z:\must-not-execute\node.exe'})
    }
    if ($Name -eq 'npm.cmd') {
        return @([pscustomobject]@{Source=$global:ASTER_FEISHU_TEST_NPM},
                 [pscustomobject]@{Source='Z:\must-not-execute\npm.cmd'})
    }
    Microsoft.PowerShell.Core\Get-Command @PSBoundParameters
}
try {
    & $Installer -DataDir $DataDirectory -Version $Version
} catch {
    [Console]::Error.WriteLine($_.Exception.ToString())
    exit 1
}
''', encoding="utf-8")
    data = tmp_path / "isolated state"
    target = data / "tools/feishu-cli/lark-cli.exe"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"previous executable must survive a failed installation")
    environment = {**os.environ, "ASTER_DATA_DIR": str(tmp_path / "unselected data"),
                   "ASTER_FEISHU_TEST_BINARY": str(binary)}
    result = subprocess.run([
        shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(harness),
        "-Installer", str(ROOT / "scripts/Install-FeishuCLI.ps1"), "-DataDirectory", str(data),
        "-Node", node, "-Npm", str(fake_npm), "-Version", "1.0.100",
    ], env=environment, capture_output=True, check=False, timeout=45)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    payload = json.loads(result.stdout.decode("utf-8"))
    assert Path(payload["Executable"]) == target
    assert payload["AccountAuthorization"] == "not_started"
    assert target.read_bytes() == binary.read_bytes()
    assert not list(target.parent.glob("*.staging.exe"))
    assert not (data / "config.yaml").exists() and not (data / "credentials.yaml").exists()
    assert not Path(environment["ASTER_DATA_DIR"]).exists()
