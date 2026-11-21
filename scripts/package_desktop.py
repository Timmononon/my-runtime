"""Build the offline Windows x64 Aster installer from a verified desktop runtime."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

from desktop_notices import write_frontend_notices

ROOT = Path(__file__).resolve().parents[1]
DESKTOP = ROOT / "desktop"
INNO_VERSION = "6.7.3"
INNO_URL = "https://github.com/jrsoftware/issrc/releases/download/is-6_7_3/innosetup-6.7.3.exe"
INNO_SHA = "9c73c3bae7ed48d44112a0f48e66742c00090bdb5bef71d9d3c056c66e97b732"
LANG_URL = ("https://raw.githubusercontent.com/jrsoftware/issrc/is-6_7_3/"
            "Files/Languages/Unofficial/ChineseSimplified.isl")
LANG_SHA = "7d544b9bb1d142cfa11f2e5d3cc8abe2e55f8e066c5124e3772675aa236e1278"
RCEDIT_URL = "https://github.com/electron/rcedit/releases/download/v2.0.0/rcedit-x64.exe"
RCEDIT_SHA = "3e7801db1a5edbec91b49a24a094aad776cb4515488ea5a4ca2289c400eade2a"


def sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def fetch_verified(url: str, target: Path, expected: str) -> Path:
    if target.is_file() and sha256(target) == expected:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as temp:
        partial = Path(temp.name)
    try:
        with urllib.request.urlopen(url, timeout=90) as response, partial.open("wb") as output:
            shutil.copyfileobj(response, output)
        if sha256(partial) != expected:
            raise ValueError(f"Download did not match the pinned SHA-256: {target.name}")
        os.replace(partial, target)
    finally:
        partial.unlink(missing_ok=True)
    return target


def electron_archive(cache: Path) -> Path:
    version = json.loads((DESKTOP / "package.json").read_text())["devDependencies"]["electron"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Pin an exact version of Electron for the Windows installer")
    name = f"electron-v{version}-win32-x64.zip"
    npm_pkg = DESKTOP / "node_modules" / "electron"
    if not (npm_pkg / "checksums.json").is_file() or json.loads(
        (npm_pkg / "package.json").read_text()
    )["version"] != version:
        raise ValueError("Run npm ci --prefix desktop to install the pinned Electron build metadata")
    expected = json.loads((npm_pkg / "checksums.json").read_text())[name]
    npm_cache = Path(os.environ["LOCALAPPDATA"]) / "electron" / "Cache"
    for cached in npm_cache.glob(f"*/{name}"):
        if cached.is_file() and sha256(cached) == expected:
            return cached
    url = f"https://github.com/electron/electron/releases/download/v{version}/{name}"
    return fetch_verified(url, cache / name, expected)


def compiler_tools(cache: Path, work: Path) -> tuple[Path, Path, Path]:
    bootstrap = fetch_verified(INNO_URL, cache / f"innosetup-{INNO_VERSION}.exe", INNO_SHA)
    language = fetch_verified(LANG_URL, cache / f"ChineseSimplified-{INNO_VERSION}.isl", LANG_SHA)
    rcedit = fetch_verified(RCEDIT_URL, cache / "rcedit-x64-2.0.0.exe", RCEDIT_SHA)
    # Portable mode keeps the compiler out of the build user's installed programs and shortcuts.
    compiler = work / "compiler"
    subprocess.run([bootstrap, "/PORTABLE=1", "/CURRENTUSER", "/SP-", "/VERYSILENT", "/NOICONS",
                    "/SUPPRESSMSGBOXES", "/NORESTART", f"/DIR={compiler}"], check=True)
    return compiler / "ISCC.exe", language, rcedit


def validate_runtime(runtime: Path, version: str) -> None:
    for name in ("python.exe", "python3.dll", "python312.dll", "python312._pth", "Lib/os.py",
                 "Lib/site-packages/aster/web/static/index.html", "LICENSE.txt", "runtime-manifest.json"):
        if not (runtime / name).is_file():
            raise ValueError(f"Missing portable runtime file: {name}")
    manifest = json.loads((runtime / "runtime-manifest.json").read_bytes())
    if (manifest.get("schema_version") != 1 or manifest.get("platform") != "win-amd64"
            or manifest.get("aster_version") != version or not manifest.get("packages")
            or not re.fullmatch(r"[0-9a-f]{64}", manifest.get("wheel_sha256", ""))):
        raise ValueError("Expected the distribution produced by scripts/build_desktop_runtime.py")
    forbidden = {".git", ".aster", ".api-token", "api.token", "credentials.yaml", ".env", "pyvenv.cfg",
                 "runtime.db", "runtime.db-wal", "runtime.db-shm", "service.json", "service.stop.json"}
    for dirpath, dirs, files in os.walk(runtime):
        for name in (*dirs, *files):
            entry = Path(dirpath) / name
            if entry.is_symlink() or entry.is_junction() or name.lower() in forbidden:
                raise ValueError(f"Unexpected runtime entry: {entry.relative_to(runtime)}")


def stage_app(app: Path, archive: Path, rcedit: Path, version: str):
    with zipfile.ZipFile(archive) as bundle:
        for item in bundle.infolist():
            parts = PurePosixPath(item.filename).parts
            if not parts or item.is_dir():
                continue
            if parts[0] == "resources":
                continue  # Replace Electron's default demonstration app with Aster.
            if len(parts) > 1 and parts[0] != "locales":
                raise ValueError(f"Unexpected Electron archive directory: {parts[0]}")
            bundle.extract(item, app)
    exe = app / "Aster.exe"
    (app / "electron.exe").rename(exe)
    resources = app / "resources"
    sources = resources / "app"
    (sources / "assets").mkdir(parents=True)
    for name in ("main.cjs", "renderer.cjs", "preload.cjs", "assets/aster.ico"):
        shutil.copy2(DESKTOP / name, sources / name)
    for name in ("launch.py", "feishu_connect.py"):
        shutil.copy2(DESKTOP / name, resources / name)
    (resources / "tools").mkdir()
    shutil.copy2(ROOT / "scripts" / "Install-FeishuCLI.ps1", resources / "tools" / "Install-FeishuCLI.ps1")
    source_pkg = json.loads((DESKTOP / "package.json").read_text())
    pkg = {key: source_pkg[key] for key in ("name", "productName", "main")}
    pkg["version"] = version
    (sources / "package.json").write_text(json.dumps(pkg, indent=2) + "\n", encoding="utf-8")
    write_frontend_notices(ROOT / "frontend", resources / "THIRD_PARTY_FRONTEND.txt")
    subprocess.run([rcedit, exe, "--set-icon", DESKTOP / "assets" / "aster.ico",
                    "--set-file-version", version, "--set-product-version", version,
                    "--set-version-string", "ProductName", "Aster",
                    "--set-version-string", "FileDescription", "Aster",
                    "--set-version-string", "CompanyName", "Aster",
                    "--set-version-string", "InternalName", "Aster",
                    "--set-version-string", "OriginalFilename", "Aster.exe"], check=True)


def build(runtime: Path, output: Path, cache: Path) -> Path:
    if os.name != "nt":
        raise RuntimeError("The installer is built on Windows")
    runtime = runtime.resolve(strict=True)
    output = output.resolve()
    cache = cache.resolve()
    if output.is_relative_to(runtime) or cache.is_relative_to(runtime):
        raise ValueError("Keep the build output and download cache outside the input runtime")
    with (ROOT / "pyproject.toml").open("rb") as source:
        version = tomllib.load(source)["project"]["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Windows setup requires a numeric major.minor.patch version")
    validate_runtime(runtime, version)
    archive = electron_archive(cache)
    output.mkdir(parents=True, exist_ok=True)
    filename = f"Aster-Setup-{version}-win-x64.exe"
    with tempfile.TemporaryDirectory(prefix="aster-setup-", dir=output) as temporary:
        work = Path(temporary)
        compiler, language, rcedit = compiler_tools(cache, work)
        app = work / "app"
        stage_app(app, archive, rcedit, version)
        subprocess.run([compiler, "/Q", f"/O{work}", f"/DAsterVersion={version}",
                        f"/DDesktopSource={DESKTOP}", f"/DElectronApp={app}",
                        f"/DRuntimeSource={runtime}", f"/DChineseMessages={language}", DESKTOP / "setup.iss"],
                       check=True)
        os.replace(work / filename, output / filename)
    return output / filename


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", type=Path, required=True, help="Output of build_desktop_runtime.py")
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    parser.add_argument("--cache", type=Path, default=ROOT / "artifacts/desktop-tools/downloads")
    args = parser.parse_args()
    installer = build(args.runtime, args.output, args.cache)
    print(json.dumps({"installer": str(installer), "size_bytes": installer.stat().st_size,
                      "sha256": sha256(installer)}))
