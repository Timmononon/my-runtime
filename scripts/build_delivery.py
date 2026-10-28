"""Build from an isolated source snapshot, optionally using fresh staged static.

Does not edit the checkout's production assets or user Runtime directory. Install
build-requirements.lock in a separate venv and pass its Python via --build-python.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from verify_web_wheel import source_inventory


def build(root: Path, output: Path, *, static_root: Path | None = None, build_python=None):
    root, output = root.resolve(strict=True), output.resolve()
    if static_root is not None:
        static_root = static_root.resolve(strict=True)
        if not (static_root / "index.html").is_file():
            raise ValueError("Fresh static build must contain index.html")
    output.mkdir(parents=True, exist_ok=True)
    source_inventory(root, static_root)
    with tempfile.TemporaryDirectory(prefix="aster-wheel-source-") as directory:
        stage = Path(directory) / "project"
        stage.mkdir()
        for name in ("README.md", "pyproject.toml"):
            shutil.copy2(root / name, stage / name)
        def ignored(folder, names):
            skip = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")(folder, names)
            if static_root is not None and Path(folder).resolve() == (root / "src/aster/web").resolve():
                skip.add("static")
            return skip
        shutil.copytree(root / "src" / "aster", stage / "src" / "aster", ignore=ignored)
        if static_root is not None:
            target = stage / "src" / "aster" / "web" / "static"
            # This literal child of a TemporaryDirectory is the only removal.
            if target.resolve().is_relative_to(stage.resolve()) and target.exists():
                shutil.rmtree(target)
            shutil.copytree(static_root, target)
        command = [str(build_python or sys.executable), "-m", "hatchling", "build", "-t", "wheel", "-d", str(output)]
        result = subprocess.run(command, cwd=stage, capture_output=True, text=True, encoding="utf-8",errors="replace",check=False)
        if result.returncode:
            raise RuntimeError("Isolated wheel build failed; inspect the build environment and source configuration")
    wheels = sorted(output.glob("aster_runtime-*.whl"), key=lambda path: path.stat().st_mtime_ns)
    if not wheels:
        raise RuntimeError("Wheel build produced no Aster wheel")
    wheel = wheels[-1]
    result = {"wheel": str(wheel), "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
              "source_snapshot_build": True, "static_root": str(static_root) if static_root else str(root / "src/aster/web/static")}
    print(json.dumps(result))
    return wheel


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, default=Path("artifacts/packages"))
    parser.add_argument("--static-root", type=Path)
    parser.add_argument("--build-python", type=Path)
    args = parser.parse_args()
    build(args.root, args.output, static_root=args.static_root, build_python=args.build_python)
