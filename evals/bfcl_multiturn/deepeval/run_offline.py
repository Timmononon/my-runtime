"""Launch the pinned DeepEval in a clean child with an isolated working directory."""
from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
from pathlib import Path


def child_environment(scratch: Path) -> dict[str, str]:
    # Deliberately do not inherit provider credentials, proxies, dotenv or login state.
    allowed = {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "PATH", "NUMBER_OF_PROCESSORS"}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env.update({
        "HOME": str(scratch), "USERPROFILE": str(scratch),
        "APPDATA": str(scratch / "appdata"), "LOCALAPPDATA": str(scratch / "localappdata"),
        "TEMP": str(scratch), "TMP": str(scratch),
        "ASTER_HOME": str(scratch / "aster-runtime-unused"),
        "ASTER_FRAMEWORK_BLOCKED_RUNTIME": str(Path.home() / ".aster"),
        "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "DEEPEVAL_TELEMETRY_OPT_OUT": "1", "DEEPEVAL_TELEMETRY_ENABLED": "0",
        "DEEPEVAL_DISABLE_DOTENV": "1", "DEEPEVAL_DISABLE_LEGACY_KEYFILE": "1",
        "DEEPEVAL_UPDATE_WARNING_OPT_IN": "0",
        "DEEPEVAL_FILE_SYSTEM": "READ_ONLY", "DEEPEVAL_CACHE_FOLDER": str(scratch / "cache"),
        "DEEPEVAL_LOCAL_STORE": "json", "DEEPEVAL_NO_INSPECT_PROMPT": "1",
        "ENABLE_DEEPEVAL_CACHE": "0", "CONFIDENT_OPEN_BROWSER": "0", "CONFIDENT_TRACE_FLUSH": "0",
        "CONFIDENT_TRACE_SAMPLE_RATE": "0", "OTEL_SDK_DISABLED": "true",
        "OTEL_TRACES_EXPORTER": "none", "OTEL_METRICS_EXPORTER": "none", "OTEL_LOGS_EXPORTER": "none",
    })
    return env


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--test", action="store_true", help="Run isolated adapter tests instead of replay")
    args = parser.parse_args()
    if not args.test and args.bundle is None:
        parser.error("--bundle is required unless --test is selected")
    output = args.output.resolve()
    if output.exists():
        parser.error("Evidence output must be new; existing evidence is never overwritten")
    root = Path(__file__).resolve().parents[3]
    python = root / ".eval-tools" / "deepeval-venv" / "Scripts" / "python.exe"
    if not python.is_file():
        parser.error("Install the pinned DeepEval environment first (see requirements.lock.txt)")
    command = [str(python), "-I", str(Path(__file__).with_name("worker.py")), "--output", str(output)]
    if args.test:
        command.append("--test")
    else:
        command.extend(["--bundle", str(args.bundle.resolve())])
    with tempfile.TemporaryDirectory(prefix="aster-deepeval-offline-") as temporary:
        scratch = Path(temporary)
        return subprocess.run(command, cwd=scratch, env=child_environment(scratch), check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
