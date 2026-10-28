"""Run launcher argument handling in Windows PowerShell 5.1 and PowerShell 7.

An offline native executable records arguments instead of starting Aster. The
copied scripts and every possible data-directory target stay under tmp_path.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows launch scripts")
ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ("Open-Aster.ps1", "Start-Aster.ps1", "Stop-Aster.ps1", "Doctor-Aster.ps1")


@pytest.fixture(params=("powershell.exe", "pwsh.exe"))
def shell(request):
    executable = shutil.which(request.param)
    if not executable:
        pytest.skip(f"{request.param} unavailable")
    return executable


@pytest.fixture(scope="session")
def argv_recorder(tmp_path_factory):
    powershell = shutil.which("powershell.exe")
    if not powershell:
        pytest.skip("Windows PowerShell 5.1 needed to compile an offline argument recorder")
    folder = tmp_path_factory.mktemp("script-recorder")
    target = folder / "capture.exe"
    builder = folder / "build.ps1"
    builder.write_text("""param([string]$Destination)
$ErrorActionPreference = 'Stop'
Add-Type -OutputAssembly $Destination -OutputType ConsoleApplication -TypeDefinition @'
using System;
using System.Diagnostics;
using System.IO;
using System.Text;
public class Capture {
    public static int Main(string[] args) {
        string[] lines = new string[args.Length + 1];
        lines[0] = Process.GetCurrentProcess().MainModule.FileName;
        Array.Copy(args, 0, lines, 1, args.Length);
        File.WriteAllLines(Environment.GetEnvironmentVariable("ASTER_SCRIPT_TEST_CAPTURE"), lines, new UTF8Encoding(false));
        return 0;
    }
}
'@
""", encoding="utf-8")
    result = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                             "-File", str(builder), "-Destination", str(target)],
                            capture_output=True, check=False, timeout=30)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    return target


@pytest.fixture
def launcher(tmp_path, argv_recorder):
    project = tmp_path / "Aster 项目 with spaces"
    scripts = project / "scripts"
    scripts.mkdir(parents=True)
    for name in (*SCRIPTS, "Aster-Paths.ps1", "Install-Aster.ps1"):
        shutil.copy2(ROOT / "scripts" / name, scripts / name)
    runtime = project / ".venv" / "Scripts" / "python.exe"
    runtime.parent.mkdir(parents=True)
    shutil.copy2(argv_recorder, runtime)
    (project / "requirements.lock").write_text("", encoding="utf-8")
    working = tmp_path / "workspace 目录"
    working.mkdir()
    profile = tmp_path / "user profile"
    profile.mkdir()
    local = tmp_path / "local-app-data"
    local.mkdir()
    capture = tmp_path / "arguments.txt"
    environment = {**os.environ, "USERPROFILE": str(profile), "LOCALAPPDATA": str(local),
                   "ASTER_SCRIPT_TEST_CAPTURE": str(capture)}
    environment.pop("ASTER_DATA_DIR", None)
    environment.pop("ASTER_HOME", None)
    return project, working, profile, local, capture, environment


def run_script(shell, launcher, name, arguments=(), environment=None):
    project, working, _, _, _, defaults = launcher
    result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                             "-File", str(project / "scripts" / name), *arguments],
                            cwd=working, env=environment or defaults,
                            capture_output=True, check=False, timeout=15)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    return result


@pytest.mark.parametrize("name", SCRIPTS)
@pytest.mark.parametrize("priority", ("profile", "home", "data", "explicit"))
def test_launcher_directory_precedence_and_body_python_default(shell, launcher, name, priority):
    project, working, profile, local, capture, environment = launcher
    home = working / "legacy home"
    data = working / "data override"
    explicit = working / "explicit isolated"
    arguments = ["-NoBrowser"] if name == "Open-Aster.ps1" else []
    expected = profile / ".aster"
    if priority in ("home", "data", "explicit"):
        environment["ASTER_HOME"] = str(home)
        expected = home
    if priority in ("data", "explicit"):
        environment["ASTER_DATA_DIR"] = str(data)
        expected = data
    if priority == "explicit":
        arguments += ["-DataDir", str(explicit)]
        expected = explicit
    run_script(shell, launcher, name, arguments, environment)
    executable, *argv = capture.read_text(encoding="utf-8").splitlines()
    assert Path(executable) == project / ".venv" / "Scripts" / "python.exe"
    assert argv[argv.index("--data-dir") + 1] == str(expected)
    assert argv[argv.index("-m") + 1] == "aster"
    if name == "Open-Aster.ps1":
        assert argv[argv.index("--cwd") + 1] == str(working)
        assert "--no-browser" in argv
    assert not any(path.exists() for path in (profile / ".aster", local / "Aster", home, data, explicit))


@pytest.mark.parametrize("value", ("empty-env", "~/custom", "relative data", "whitespace-env", "explicit-empty"))
def test_launcher_expansion_and_explicit_python_override(shell, launcher, argv_recorder, value):
    _, working, profile, _, capture, environment = launcher
    explicit_python = working / "chosen interpreter.exe"
    shutil.copy2(argv_recorder, explicit_python)
    arguments = ["-PythonExe", str(explicit_python)]
    environment["ASTER_HOME"] = ""
    environment["ASTER_DATA_DIR"] = ""
    expected = profile / ".aster"
    if value == "~/custom":
        environment["ASTER_HOME"] = "~/custom"
        expected = profile / "custom"
    elif value == "relative data":
        environment["ASTER_DATA_DIR"] = value
        expected = working / value
    elif value == "whitespace-env":
        environment["ASTER_DATA_DIR"] = " "
        expected = (working / " ").resolve()
    elif value == "explicit-empty":
        environment["ASTER_DATA_DIR"] = str(working / "ignored override")
        arguments += ["-DataDir", ""]
        expected = working
    run_script(shell, launcher, "Doctor-Aster.ps1", arguments, environment)
    executable, *argv = capture.read_text(encoding="utf-8").splitlines()
    assert Path(executable) == explicit_python
    assert argv[argv.index("--data-dir") + 1] == str(expected)


def test_install_default_project_is_resolved_in_script_body(shell, launcher):
    project, _, profile, local, _, _ = launcher
    result = run_script(shell, launcher, "Install-Aster.ps1", ["-DryRun"])
    # Windows PowerShell uses the active console code page; JSON contains no
    # secrets and decoding follows that platform encoding when it is not UTF-8.
    try:
        output = result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        output = result.stdout.decode("mbcs")
    report = json.loads(output)
    assert report["Project"] == str(project)
    assert report["VirtualEnvironment"] == str(project / ".venv")
    assert report["GlobalEnvironmentChanged"] is False
    assert report["AutostartEnabled"] is False
    assert not (profile / ".aster").exists()
    assert not (local / "Aster").exists()
