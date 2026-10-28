"""Exact source/resource wheel verification and disposable installed-runtime QA."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from email.parser import Parser
from pathlib import Path, PurePosixPath


class DeliveryError(ValueError):
    """Generic artifact errors never include file content or credential values."""


def _require(condition, reason):
    if not condition:
        raise DeliveryError(reason)


def forbidden(name):
    parts = PurePosixPath(name).parts
    return ("\\" in name or ".." in parts or name.startswith("/") or
            any(part in {".aster", "node_modules", "tests", "__pycache__", "browser_profiles", "backups"} for part in parts)
            or PurePosixPath(name).name in {"credentials.yaml", ".api-token", ".env", "runtime.db", "web_fixture.py"}
            or name.endswith((".pyc", ".pyo", ".map", ".db", ".db-wal", ".db-shm", ".log")))


def source_inventory(root: Path, static_root: Path | None = None):
    """Compare every package file, not a hand-picked list of recently added files."""
    tree = root / "src" / "aster"
    result = {}
    sources = [(tree, "aster", bool(static_root))]
    if static_root:
        sources.append((static_root, "aster/web/static", False))
    for folder, prefix, skip_static in sources:
        for path in sorted(folder.rglob("*")):
            relative = path.relative_to(folder)
            if "__pycache__" in relative.parts or path.suffix in {".pyc", ".pyo"}:
                continue
            if skip_static and relative.parts[:2] == ("web", "static"):
                continue
            _require(not path.is_symlink() and not (hasattr(path, "is_junction") and path.is_junction()),
                     "Package sources contain a link")
            if path.is_file():
                name = prefix + "/" + relative.as_posix()
                _require(not forbidden(name), "Package sources contain Runtime state or development artifacts")
                result[name] = path.read_bytes()
    _require("aster/web/static/index.html" in result, "Fresh built workbench is missing")
    return result


def verify_contents(wheel: Path, root: Path, *, static_root: Path | None = None):
    expected = source_inventory(root, static_root)
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    frontend = json.loads((root / "frontend/package.json").read_text(encoding="utf-8"))
    lock = json.loads((root / "frontend/package-lock.json").read_text(encoding="utf-8"))
    version = project["version"]
    _require(frontend["version"] == lock["version"] == lock["packages"][""]["version"] == version,
             "Python and frontend package versions differ")
    _require(f'__version__ = "{version}"'.encode() in expected["aster/__init__.py"], "Python source version differs")
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        _require(len(names) == len(set(names)), "Wheel contains duplicate entries")
        _require(not any(forbidden(name) for name in names), "Wheel contains forbidden state or development artifacts")
        package = {name for name in names if name.startswith("aster/") and not name.endswith("/")}
        _require(package == set(expected), "Wheel package inventory differs from current source and static build")
        for name, content in expected.items():
            _require(archive.getinfo(name).file_size == len(content) and archive.read(name) == content,
                     "Wheel package bytes differ from current source or static build")
        metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        _require(len(metadata_names) == 1, "Wheel metadata is ambiguous")
        metadata = Parser().parsestr(archive.read(metadata_names[0]).decode("utf-8"))
        _require(metadata["Name"] == project["name"] and metadata["Version"] == version, "Wheel identity differs")
        _require(metadata["Requires-Python"] == project["requires-python"], "Wheel Python compatibility differs")
        _require(metadata.get_payload().strip() == (root / "README.md").read_text(encoding="utf-8").strip(),
                 "Wheel README metadata differs from current README")
        prefix = metadata_names[0].rsplit("/", 1)[0] + "/"
        _require(all(name.startswith(("aster/",prefix)) for name in names), "Wheel has unexpected top-level files")
        entry = archive.read(prefix + "entry_points.txt").decode("utf-8")
        _require("aster = aster.cli:main" in entry, "Wheel CLI entrypoint differs")
        assets = [name for name in package if name.startswith("aster/web/static/assets/")]
        _require(any(name.endswith(".js") for name in assets) and any(name.endswith(".css") for name in assets),
                 "Wheel static asset types are incomplete")
        fixture_markers = (b"offline-workbench-fixture", b"ASTER WORKBENCH VERIFIED", b"qa-selected-skill", b"synthetic-qa-provider-key")
        _require(not any(marker in expected[name] for name in assets for marker in fixture_markers), "Wheel frontend contains fixture content")
    return {"version": version, "package_files": len(expected),
            "python_files": sum(name.endswith(".py") for name in expected),
            "bundled_resource_files": sum(name.startswith("aster/packages/bundled/") for name in expected),
            "static_files": len(assets) + 1,
            "package_manifest_sha256": hashlib.sha256(json.dumps({name: hashlib.sha256(data).hexdigest()
                for name, data in sorted(expected.items())}, separators=(",", ":")).encode()).hexdigest()}


def isolated_environment(target, temporary):
    env = {key: value for key, value in os.environ.items() if key not in {
        "DEEPSEEK_API_KEY", "ASTER_API_TOKEN", "ASTER_DATA_DIR", "ASTER_HOME", "PYTHONPATH", "PYTHONHOME",
        "ASTER_WEB_STATIC_ROOT", "ASTER_WEB_AUDIT_BROWSER_ENABLED"}}
    env.update(PYTHONPATH=str(target), PYTHONNOUSERSITE="1", PYTHONIOENCODING="utf-8",
               ASTER_HOME=str(temporary / "unselected-default-home"))
    return env


def verify(wheel: Path, report: Path, *, root=None, static_root=None, install_python=None):
    root = Path(root or Path(__file__).resolve().parents[1]).resolve(strict=True)
    wheel = wheel.resolve(strict=True)
    static_root = static_root.resolve(strict=True) if static_root else None
    findings = verify_contents(wheel, root, static_root=static_root)
    interpreter = str(Path(install_python).resolve(strict=True)) if install_python else sys.executable
    with tempfile.TemporaryDirectory(prefix="aster-installed-wheel-") as directory:
        temporary, target = Path(directory), Path(directory) / "installed package"
        result = subprocess.run([interpreter, "-m", "pip", "install", "--no-deps", "--no-compile",
            "--disable-pip-version-check", "--target", str(target), str(wheel)], capture_output=True, text=True, encoding="utf-8",errors="replace",check=False)
        _require(result.returncode == 0, "Disposable wheel installation failed")
        env = isolated_environment(target, temporary)
        probe = Path(__file__).with_name("installed_delivery_probe.py")
        result = subprocess.run([interpreter, str(probe), str(target), findings["version"]], cwd=temporary,
                                env=env, capture_output=True, text=True, encoding="utf-8",errors="replace",check=False)
        _require(result.returncode == 0, "Installed Runtime scenario probe failed")
        findings.update(json.loads(result.stdout))
        cli = subprocess.run([interpreter, "-m", "aster", "--help"], cwd=temporary, env=env,
                             capture_output=True, text=True, encoding="utf-8",errors="replace",check=False)
        _require(cli.returncode == 0 and "web" in cli.stdout, "Installed CLI entrypoint failed")
    findings.update(wheel=wheel.name, size_bytes=wheel.stat().st_size, sha256=hashlib.sha256(wheel.read_bytes()).hexdigest(),
                    source_assets_match=True, all_package_sources_match=True, fixture_not_packaged=True,
                    installed_cli_web=True, final_readme_metadata_matches=True,
                    readme_sha256=hashlib.sha256((root / "README.md").read_bytes()).hexdigest(),
                    probe_interpreter=interpreter, explicit_install_interpreter=bool(install_python), paid_calls=0)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(findings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(findings, ensure_ascii=False))
    return findings


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--report", type=Path, default=Path("artifacts/validation/web-wheel-check.json"))
    parser.add_argument("--root", type=Path)
    parser.add_argument("--static-root", type=Path)
    parser.add_argument("--install-python", type=Path, help="Python in a fresh dependency venv installed from requirements.lock")
    args = parser.parse_args()
    verify(args.wheel, args.report, root=args.root, static_root=args.static_root, install_python=args.install_python)
