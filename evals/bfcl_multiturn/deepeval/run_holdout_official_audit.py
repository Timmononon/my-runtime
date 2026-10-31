"""Launch guarded simulator replay using existing project-only dependencies."""
from __future__ import annotations

import argparse
import importlib.util
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[2]
_SPEC = importlib.util.spec_from_file_location("aster_holdout_audit_environment", HERE / "run_offline.py")
_environment = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_environment)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("a0", "a3", "mf-gate", "output"):
        parser.add_argument("--" + name, type=Path, required=name == "output")
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args(argv)
    if not args.test and any(path is None for path in (args.a0, args.a3, args.mf_gate)):
        parser.error("All three sealed campaigns are required unless --test is selected")
    output = args.output.resolve()
    if output.exists():
        parser.error("Output must be new; original measurements are never overwritten")
    python = PROJECT / ".venv/Scripts/python.exe"
    if not python.is_file():
        parser.error("The existing project virtual environment is required")
    command = [str(python), "-I", str(HERE / "holdout_official_audit_worker.py"), "--output", str(output)]
    if args.test:
        command.append("--test")
    else:
        for flag, path in (("--a0", args.a0), ("--a3", args.a3), ("--mf-gate", args.mf_gate)):
            command.extend([flag, str(path.resolve())])
    with tempfile.TemporaryDirectory(prefix="h60a-") as temporary:
        scratch = Path(temporary)
        return subprocess.run(command, cwd=scratch, env=_environment.child_environment(scratch), check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
