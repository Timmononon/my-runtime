from __future__ import annotations

import base64
import shlex
import shutil
import subprocess
import uuid

from jsonschema import validate

from aster.domain import ToolContext, ToolResult, ToolSpec
from aster.platform.filesystem import scoped_path
from aster.platform.processes import ProcessSupervisor
from aster.tools.files import TEXT, failure, schema


class ProcessTool:
    def __init__(self, supervisor: ProcessSupervisor, *, shell=False):
        self.supervisor, self.shell = supervisor, shell
        common = {"cwd": TEXT, "timeout": {"type": "number", "minimum": 0.1, "maximum": 3600},
                  "env": {"type": "object", "additionalProperties": {"type": "string"}}, "encoding": TEXT,
                  "skill_name": TEXT}
        if shell:
            properties = {**common, "backend": {"enum": ["powershell7", "powershell51", "wsl"]},
                          "script": TEXT, "distribution": TEXT, "linux_cwd": TEXT,
                          "conda_environment": TEXT, "conda_executable": TEXT}
            required = ["backend", "script"]
        else:
            properties = {**common, "executable": TEXT, "argv": {"type": "array", "items": {"type": "string"}}}
            required = ["executable", "argv"]
        self.spec = ToolSpec("shell_exec" if shell else "process_exec",
                             "Execute a bounded explicitly selected shell" if shell else "Execute a program with explicit argv",
                             schema(properties, required), risk="execute", timeout_seconds=3615, resource="process")

    async def execute(self, arguments: dict, context: ToolContext) -> ToolResult:
        try:
            validate(arguments, self.spec.parameters)
            cwd = scoped_path(arguments.get("cwd", str(context.cwd)), context, must_exist=True)
            if not cwd.is_dir():
                raise ValueError("cwd must be a directory")
            on_cancel = None
            if self.shell:
                backend = arguments["backend"]
                if backend in {"powershell7", "powershell51"}:
                    executable = shutil.which("pwsh" if backend == "powershell7" else "powershell")
                    if not executable:
                        raise ValueError(f"Requested shell is unavailable: {backend}; no implicit fallback")
                    script = ("[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
                              "$OutputEncoding = [Console]::OutputEncoding; $ErrorActionPreference='Stop';\n"
                              + arguments["script"])
                    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
                    argv = [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
                else:
                    if not arguments.get("distribution") or not arguments.get("linux_cwd", "").startswith("/"):
                        raise ValueError("WSL requires explicit distribution and absolute linux_cwd")
                    executable = shutil.which("wsl")
                    if not executable:
                        raise ValueError("WSL is not installed")
                    script = arguments["script"]
                    if arguments.get("conda_environment"):
                        script = shlex.quote(arguments.get("conda_executable", "conda")) + \
                                 " run --no-capture-output -n " + shlex.quote(arguments["conda_environment"]) + \
                                 " bash -c " + shlex.quote(script)
                    # The per-invocation marker scopes cleanup to the launched setsid group,
                    # rather than other processes sharing the selected WSL distribution.
                    marker = f"/tmp/aster-{uuid.uuid4().hex}.pid"
                    wrapper = (f"setsid bash -c {shlex.quote(script)} & child=$!; printf '%s' \"$child\" > {marker}; "
                               f"wait \"$child\"; code=$?; rm -f -- {marker}; exit \"$code\"")
                    argv = [executable, "--distribution", arguments["distribution"], "--cd", arguments["linux_cwd"],
                            "--exec", "bash", "-c", wrapper]

                    def cleanup():
                        command = (f"if test -f {marker}; then p=$(cat -- {marker}); "
                                   "case $p in ''|*[!0-9]*) exit 2;; esac; "
                                   f"kill -TERM -- -\"$p\" 2>/dev/null; sleep 0.2; kill -KILL -- -\"$p\" 2>/dev/null; rm -f -- {marker}; fi")
                        completed = subprocess.run([executable, "--distribution", arguments["distribution"],
                                                    "--exec", "bash", "-c", command],
                                                   check=False, capture_output=True, timeout=5,
                                                   creationflags=0x08000000)
                        if completed.returncode:
                            raise RuntimeError("WSL process-group cleanup did not confirm success")
                    on_cancel = cleanup
            else:
                argv = [arguments["executable"], *arguments["argv"]]
            environment = dict(arguments.get("env", {}))
            scope = context.services.get("skill_scope")
            if scope:
                environment = scope.environment(environment, skill_name=arguments.get("skill_name"))
            result = await self.supervisor.run(argv, cwd=cwd, artifact_dir=context.artifact_dir,
                                               cancel=context.cancel, env=environment,
                                               timeout=arguments.get("timeout", 60),
                                               encoding=arguments.get("encoding", "utf-8"), on_cancel=on_cancel,
                                               session_id=context.session_id)
            for item in result.artifacts:
                item.update(session_id=context.session_id, run_id=context.run_id, mime_type="text/plain")
            category = "cancelled" if result.cancelled else "timeout" if result.timed_out else "process"
            failed = result.cancelled or result.timed_out or result.error or result.exit_code != 0
            return ToolResult(f"exit_code={result.exit_code}; elapsed={result.duration:.2f}s\n"
                              f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
                              + (f"\nerror: {result.error}" if result.error else ""), bool(failed),
                              category if failed else None,
                              "unknown" if result.pid and (result.cancelled or result.timed_out or result.error) else "known",
                              result.artifacts, {"exit_code": result.exit_code, "pid": result.pid,
                                                 "cancelled": result.cancelled, "timed_out": result.timed_out})
        except Exception as exc:  # noqa: BLE001 - tool boundary serializes failures
            return failure(exc)
