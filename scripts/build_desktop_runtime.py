"""Build Aster's relocatable Windows Python runtime from release wheels.

Run with the project's x64 Windows CPython 3.12 build environment. Downloads
are cached on the builder. The installed desktop does not invoke pip.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import urllib.request
import zipfile
from email.parser import BytesParser
from importlib import metadata
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name, parse_wheel_filename

ROOT = Path(__file__).resolve().parents[1]
PYTHON_VERSION = "3.12.16"
PYTHON_RELEASE = "20261110"
ARCHIVE_NAME = f"cpython-{PYTHON_VERSION}+{PYTHON_RELEASE}-x86_64-pc-windows-msvc-install_only_stripped.tar.gz"
ARCHIVE_URL = f"https://github.com/astral-sh/python-build-standalone/releases/download/{PYTHON_RELEASE}/{ARCHIVE_NAME}"
# Published digest of the install_only_stripped asset in the upstream release.
ARCHIVE_SHA256 = "355ee61b8207b91574491b1c6dc9b0ebb4408fc56bfafe498f3ef439314b52dc"


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def interpreter_archive(cache: Path) -> Path:
    archive = cache / ARCHIVE_NAME
    if archive.is_file() and digest(archive) == ARCHIVE_SHA256:
        return archive
    with tempfile.TemporaryDirectory(dir=cache) as temporary:
        download = Path(temporary) / ARCHIVE_NAME
        with urllib.request.urlopen(ARCHIVE_URL, timeout=90) as source, download.open("wb") as target:
            shutil.copyfileobj(source, target)
        if digest(download) != ARCHIVE_SHA256:
            raise ValueError("CPython distribution does not match its pinned release digest")
        download.replace(archive)
    return archive


def extract_interpreter(archive: Path, work: Path) -> Path:
    def production_file(member: tarfile.TarInfo):
        parts = member.name.split("/")
        if parts[0] != "python" or any(part in (".", "..", "") for part in parts):
            raise ValueError(f"Unexpected archive member: {member.name}")
        if not (member.isfile() or member.isdir()):
            raise ValueError(f"Unexpected archive entry type: {member.name}")
        if len(parts) == 1:
            return True
        if parts[1] in {"Lib", "DLLs", "tcl"}:
            return (parts[1:3] not in (["Lib", "site-packages"], ["Lib", "ensurepip"])
                    and "__pycache__" not in parts and not member.name.endswith(".pyc"))
        return parts[1] in {"LICENSE.txt", "python.exe", "python3.dll", "python312.dll",
                            "vcruntime140.dll", "vcruntime140_1.dll"}

    with tarfile.open(archive, "r:gz") as source:
        source.extractall(work, members=(item for item in source if production_file(item)), filter="data")
    return work / "python"


def pip(*args: str | Path) -> None:
    env = {**os.environ, "PIP_CONFIG_FILE": os.devnull}
    subprocess.run([sys.executable, "-m", "pip", "--isolated", "--disable-pip-version-check", "--no-input",
                    *args], env=env, check=True)


def dependency_wheels(cache: Path, requirements: list[str], lock: Path) -> tuple[Path, dict]:
    # Keep source wheel rebuilds out of this shared cache. Its key changes when
    # the version constraints or the Windows production dependency graph changes.
    key = hashlib.sha256(lock.read_bytes() + json.dumps(sorted(requirements)).encode()).hexdigest()
    wheelhouse = cache / ("cp312-win_amd64-" + key[:20])
    pins = {}
    for line in lock.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            name, version = line.strip().split("==")
            pins[canonicalize_name(name)] = version

    def inspect(directory: Path) -> dict:
        result = {}
        for wheel in sorted(directory.glob("*.whl")):
            name, version, _, _ = parse_wheel_filename(wheel.name)
            if name in result or str(version) != pins.get(name):
                raise ValueError(f"Production wheel must have one exact requirements.lock pin: {wheel.name}")
            result[name] = {"version": str(version), "file": wheel.name, "sha256": digest(wheel)}
        return result

    if not wheelhouse.exists():
        with tempfile.TemporaryDirectory(dir=cache) as temporary:
            download = Path(temporary) / "wheels"
            download.mkdir()
            pip("download", "--index-url", "https://pypi.org/simple", "--only-binary=:all:", "--constraint", lock,
                "--dest", download, *requirements)
            # pip checks the index-provided wheel digest on the initial download.
            (download / "wheels.json").write_text(json.dumps(inspect(download), indent=2), encoding="utf-8")
            try:
                download.rename(wheelhouse)
            except FileExistsError:
                pass  # A second builder populated the identical cache key first.
    packages = inspect(wheelhouse)
    if not packages or packages != json.loads((wheelhouse / "wheels.json").read_text(encoding="utf-8")):
        raise ValueError(f"Cached release wheels have changed: {wheelhouse}")
    return wheelhouse, packages


def install_wheels(runtime: Path, work: Path, wheel: Path, cache: Path) -> tuple[str, dict]:
    name, version, _, _ = parse_wheel_filename(wheel.name)
    if name != "aster-runtime":
        raise ValueError("Expected an aster-runtime release wheel")
    with zipfile.ZipFile(wheel) as package:
        info, = (n for n in package.namelist() if n.endswith(".dist-info/METADATA"))
        wheel_info = BytesParser().parsebytes(package.read(info))
    if canonicalize_name(wheel_info["Name"]) != name or wheel_info["Version"] != str(version):
        raise ValueError("Aster wheel metadata and filename disagree")
    declared = [Requirement(line) for line in wheel_info.get_all("Requires-Dist", [])]
    production = [str(req) for req in declared if not req.marker or req.marker.evaluate({"extra": ""})]
    lock = ROOT / "requirements.lock"
    wheelhouse, packages = dependency_wheels(cache, production, lock)
    local = work / "aster-wheel"
    local.mkdir()
    shutil.copyfile(wheel, local / wheel.name)
    dest = runtime / "Lib" / "site-packages"
    # Resolve Aster by name against only these wheels. Installing a file URL
    # would record the developer's absolute checkout in direct_url.json.
    pip("install", "--no-index", "--only-binary=:all:", "--constraint", lock, "--find-links", local,
        "--find-links", wheelhouse, "--target", dest, "--no-compile", f"aster-runtime=={version}")
    # pip --target puts generated command wrappers and development headers in
    # these two directories. The desktop calls modules with its own python.exe.
    for folder in ("bin", "include"):
        path = dest / folder
        if path.is_dir() and path.resolve() == path and path.parent == dest:
            shutil.rmtree(path)
    for record in dest.glob("*.dist-info/RECORD"):
        with record.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.reader(stream))
        kept = [row for row in rows if not row[0].startswith("../")]
        if len(kept) < len(rows):
            with record.open("w", encoding="utf-8", newline="") as stream:
                csv.writer(stream).writerows(kept)
    installed = {canonicalize_name(d.metadata["Name"]): d.version for d in metadata.distributions(path=[str(dest)])}
    if installed != {name: str(version), **{k: v["version"] for k, v in packages.items()}}:
        raise ValueError("Installed distributions differ from the validated production wheels")
    return str(version), packages


def verify_runtime(runtime: Path, work: Path) -> None:
    packages = runtime / "Lib" / "site-packages"
    # On Windows, even a ._pth-isolated interpreter with 'import site' can
    # execute the user's %APPDATA%/Python/Python312/site-packages/*.pth files.
    # pywin32 is the only production wheel that needs a .pth. Supply its three
    # relative import roots and its two loader DLLs directly instead.
    if {p.name for p in packages.glob("*.pth")} != {"pywin32.pth"}:
        raise ValueError("Review the new Python path hook before bundling this dependency set")
    paths = [".", "Lib", "DLLs", "Lib/site-packages", "Lib/site-packages/win32",
             "Lib/site-packages/win32/lib", "Lib/site-packages/pythonwin"]
    (runtime / "python312._pth").write_text("\n".join(paths) + "\n", encoding="ascii")
    for name in ("pythoncom312.dll", "pywintypes312.dll"):
        shutil.copyfile(packages / "pywin32_system32" / name, runtime / name)
    python = runtime / "python.exe"
    profile = work / "test-user"
    profile.mkdir()
    env = {**os.environ, "PATH": str(Path(os.environ["SystemRoot"]) / "System32"),
           "PYTHONPATH": str(profile / "wrong-pythonpath"), "PYTHONHOME": str(profile / "wrong-pythonhome"),
           "USERPROFILE": str(profile), "HOME": str(profile), "ASTER_DATA_DIR": str(profile / "data")}
    # Seed bytecode without build-machine paths. Source hashes survive timestamp
    # changes in Windows installers and prevent unmanaged files on first launch.
    seed = """
import compileall, py_compile, sys
from pathlib import Path
root = Path(sys.executable).parent
assert compileall.compile_dir(str(root/'Lib'), quiet=1, force=True, stripdir=str(root),
                              invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH)
"""
    probe = """
import importlib.util, io, site, sqlite3, ssl, sys
from pathlib import Path
from psutil import Process
import certifi, docx, openpyxl, pptx
import win32api, win32cred, pywintypes, pythoncom, pypdfium2, greenlet, uvicorn
from PIL import Image
from keyring.backends.Windows import WinVaultKeyring
from aster.api.app import create_app
from playwright._impl._driver import compute_driver_executable
root = Path(sys.executable).parent
assert sys.version.split()[0] == sys.argv[1]
assert sys.flags.isolated and sys.flags.no_site and not site.ENABLE_USER_SITE
assert all(Path(p).resolve().is_relative_to(root) for p in sys.path)
assert importlib.util.find_spec('pytest') is None and importlib.util.find_spec('pip') is None
assert (root/'Lib'/'site-packages'/'aster'/'web'/'static'/'index.html').is_file()
assert Path(compute_driver_executable()[0]).is_file() and Process().exe()
assert ssl.create_default_context(cafile=certifi.where()).get_ca_certs()
assert WinVaultKeyring.priority > 0
for document in (docx.Document(), openpyxl.Workbook(), pptx.Presentation()):
    buffer = io.BytesIO()
    document.save(buffer)
    assert buffer.getvalue().startswith(b'PK')
image = Image.new('RGB', (2, 2))
image.save(io.BytesIO(), format='PNG')
with sqlite3.connect(':memory:') as db:
    db.execute("CREATE VIRTUAL TABLE audit USING fts5(text,tokenize='trigram')")
"""
    subprocess.run([python, "-B", "-W", "ignore::SyntaxWarning", "-c", seed], cwd=profile, env=env, check=True)
    caches = {p: digest(p) for p in runtime.rglob("*.pyc")}
    subprocess.run([python, "-X", "utf8", "-c", probe, PYTHON_VERSION], cwd=profile, env=env, check=True)
    subprocess.run([python, "-X", "utf8", "-m", "aster", "--help"], cwd=profile, env=env,
                   check=True, stdout=subprocess.DEVNULL)
    subprocess.run([python, "-X", "utf8", "-m", "playwright", "--version"], cwd=profile, env=env,
                   check=True, stdout=subprocess.DEVNULL, timeout=30)
    if caches != {p: digest(p) for p in runtime.rglob("*.pyc")}:
        raise ValueError("The installed CLI wrote unexpected bytecode in the application directory")


def build(wheel: Path, output: Path, cache: Path) -> dict:
    if sysconfig.get_platform() != "win-amd64" or sys.version_info[:2] != (3, 12):
        raise RuntimeError("Build this x64 desktop runtime with CPython 3.12 on 64-bit Windows")
    wheel = wheel.resolve(strict=True)
    output = output.absolute()
    if output.resolve() != output or (output.exists() and (not output.is_dir() or any(output.iterdir()))):
        raise ValueError("The output must be a new or empty runtime directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    cache = cache.resolve()
    if cache.is_relative_to(output):
        raise ValueError("Keep the download cache outside the redistributable runtime")
    cache.mkdir(parents=True, exist_ok=True)
    archive = interpreter_archive(cache)
    with tempfile.TemporaryDirectory(prefix="aster-runtime-build-", dir=output.parent) as temporary:
        work = Path(temporary).resolve(strict=True)
        runtime = extract_interpreter(archive, work)
        version, packages = install_wheels(runtime, work, wheel, cache)
        verify_runtime(runtime, work)
        manifest = {"schema_version": 1, "platform": "win-amd64", "python_version": PYTHON_VERSION,
                    "python_archive_sha256": ARCHIVE_SHA256, "python_source": ARCHIVE_URL,
                    "aster_version": version, "wheel_sha256": digest(wheel), "packages": packages}
        (runtime / "runtime-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        if output.is_dir():
            output.rmdir()  # Refuses if a concurrent process put anything here.
        runtime.rename(output)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, default=ROOT / "artifacts/desktop/cache/python")
    args = parser.parse_args()
    manifest = build(args.wheel, args.output, args.cache_dir)
    print(json.dumps({"output": str(args.output.resolve()), "python": PYTHON_VERSION,
                      "aster": manifest["aster_version"], "dependencies": len(manifest["packages"])}))


if __name__ == "__main__":
    main()
