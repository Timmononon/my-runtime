"""Run pinned DeepEval over sealed dev40 configurations in an isolated offline child."""
from __future__ import annotations

import argparse
import importlib.util
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[2]
_SPEC = importlib.util.spec_from_file_location("aster_existing_deepeval_environment", HERE / "run_offline.py")
_environment = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_environment)
child_environment = _environment.child_environment


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--baseline-label", default="A0")
    parser.add_argument("--candidate-label", default="A3")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--no-junit", action="store_true", help="Skip optional JUnit XML when its host metadata collection stalls")
    args = parser.parse_args(argv)
    if not args.test and args.baseline is None:
        parser.error("--baseline is required unless --test is selected")
    if args.candidate is not None and args.baseline is None:
        parser.error("--candidate requires --baseline")
    output = args.output.resolve()
    if output.exists():
        parser.error("Output must be new; historical evidence is never overwritten")
    python = PROJECT / ".eval-tools/deepeval-venv/Scripts/python.exe"
    if not python.is_file():
        parser.error("The project-local pinned DeepEval environment is not installed")
    command = [str(python), "-I", str(HERE / "comparison_worker.py"), "--output", str(output)]
    if args.test:
        command.append("--test")
        if args.no_junit:
            command.append("--no-junit")
    else:
        command.extend(["--baseline", str(args.baseline.resolve()), "--baseline-label", args.baseline_label])
        if args.candidate is not None:
            command.extend(["--candidate", str(args.candidate.resolve()), "--candidate-label", args.candidate_label])
    with tempfile.TemporaryDirectory(prefix="aster-deepeval-comparison-") as temporary:
        scratch = Path(temporary)
        return subprocess.run(command, cwd=scratch, env=child_environment(scratch), check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
