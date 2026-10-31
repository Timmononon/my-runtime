"""Run deterministic DeepEval over the three sealed sixty-task holdout runs."""
from __future__ import annotations

import argparse
import importlib.util
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[2]
_SPEC = importlib.util.spec_from_file_location("aster_holdout_clean_environment", HERE / "run_offline.py")
_environment = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_environment)
child_environment = _environment.child_environment


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--a0", type=Path)
    parser.add_argument("--a3", type=Path)
    parser.add_argument("--mf-gate", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--test", action="store_true")
    args = parser.parse_args(argv)
    if not args.test and any(value is None for value in (args.a0, args.a3, args.mf_gate)):
        parser.error("All three sealed campaigns are required unless --test is selected")
    output = args.output.resolve()
    if output.exists():
        parser.error("Output must be new; saved evidence is never overwritten")
    python = PROJECT / ".eval-tools/deepeval-venv/Scripts/python.exe"
    if not python.is_file():
        parser.error("The pinned project-local DeepEval environment is required")
    command = [str(python), "-I", str(HERE / "holdout_comparison_worker.py"), "--output", str(output)]
    if args.test:
        command.append("--test")
    else:
        for flag, path in (("--a0", args.a0), ("--a3", args.a3), ("--mf-gate", args.mf_gate)):
            command.extend([flag, str(path.resolve())])
    with tempfile.TemporaryDirectory(prefix="a-h60-") as temporary:
        scratch = Path(temporary)
        return subprocess.run(command, cwd=scratch, env=child_environment(scratch), check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
