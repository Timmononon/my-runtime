"""Windows diagnostics, owned-service lifecycle and verifiable portable backups.

No function enables autostart unless install_autostart is explicitly called.
Database restore is offline; complete bundles restore only to a new directory.
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.metadata
import importlib.util
import json
import math
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path, PurePosixPath

import psutil

from aster.platform.filesystem import FileLock, atomic_write, digest
from aster.state.store import SCHEMA_VERSION


def _json_write(path: Path, value: dict):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))


def _filesystem_path(path: Path) -> Path:
    """Use extended Windows paths for staging I/O without changing persisted paths."""
    if os.name != "nt":
        return path
    raw = str(path.absolute())
    if raw.startswith("\\\\?\\"):
        return path
    return Path("\\\\?\\UNC\\" + raw[2:] if raw.startswith("\\\\") else "\\\\?\\" + raw)


def _read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object in {path.name}")  # noqa: TRY004 - malformed persisted data
    return value


class ServiceLease:
    def __init__(self, data_dir: Path, *, host="127.0.0.1", port=8765):
        self.data_dir = Path(data_dir).resolve()
        self.host, self.port = host, port
        self.instance_id = os.environ.get("ASTER_SERVICE_INSTANCE_ID") or uuid.uuid4().hex
        self.lock = FileLock(self.data_dir / ".service.lock", timeout=0.1)
        self.held = False

    def acquire(self):
        self.lock.acquire()
        self.held = True
        try:
            process = psutil.Process()
            metadata = {"pid": process.pid, "create_time": process.create_time(), "instance_id": self.instance_id,
                        "executable": process.exe(), "host": self.host, "port": self.port,
                        "kind": "daemon" if self.host is not None else "foreground"}
            _json_write(self.data_dir / "service.json", metadata)
            (self.data_dir / "service.stop.json").unlink(missing_ok=True)
            return self
        except BaseException:
            self.release()
            raise

    def stop_requested(self) -> bool:
        try:
            return _read_json(self.data_dir / "service.stop.json").get("instance_id") == self.instance_id
        except (OSError, ValueError):
            return False

    def release(self):
        if not self.held:
            return
        try:
            try:
                if _read_json(self.data_dir / "service.json").get("instance_id") == self.instance_id:
                    (self.data_dir / "service.json").unlink(missing_ok=True)
                    (self.data_dir / "service.stop.json").unlink(missing_ok=True)
            except (OSError, ValueError):
                pass  # stale/malformed metadata is never authority to terminate a process
        finally:
            self.held = False
            self.lock.release()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *args):
        self.release()


def _same_process(record: dict) -> psutil.Process | None:
    try:
        process = psutil.Process(int(record["pid"]))
        if abs(process.create_time() - float(record["create_time"])) > 0.01:
            return None
        if os.path.normcase(process.exe()) != os.path.normcase(record["executable"]):
            return None
        return process
    except (KeyError, ValueError, psutil.Error, OSError):
        return None


def service_status(data_dir: Path) -> dict:
    data_dir = Path(data_dir).resolve()
    try:
        record = _read_json(data_dir / "service.json")
    except (OSError, ValueError):
        record = {}
    try:
        with FileLock(data_dir / ".service.lock", timeout=0):
            locked = False
    except TimeoutError:
        locked = True
    alive = _same_process(record) is not None
    return {**record, "running": bool(locked and alive), "locked": locked,
            "state": "running" if locked and alive else "starting_or_locked" if locked else "stopped"}


def _terminate_owned_tree(process: psutil.Process):
    try:
        if not process.is_running():
            return
        children = process.children(recursive=True)
        for child in reversed(children):
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                continue
        process.terminate()
        _, alive = psutil.wait_procs([*children, process], timeout=5)
        for remaining in alive:
            try:
                remaining.kill()
            except psutil.NoSuchProcess:
                continue
        _, alive = psutil.wait_procs(alive, timeout=5)
        if alive:
            raise TimeoutError("Owned service processes remain after termination")
    except psutil.NoSuchProcess:
        return


def start_service(data_dir: Path, argv: list[str], *, host="127.0.0.1", port=8765, timeout=30) -> dict:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Service helpers only start a loopback listener")
    if not argv or any(not isinstance(arg, str) or "\0" in arg for arg in argv):
        raise ValueError("Expected explicit argv")
    data_dir = Path(data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    with FileLock(data_dir / ".service-start.lock", timeout=2):
        status = service_status(data_dir)
        if status["locked"]:
            if status["running"] and status.get("kind") == "daemon":
                return {**status, "already_running": True}
            raise RuntimeError("Data directory is already owned by another runtime")
        instance_id = uuid.uuid4().hex
        environment = os.environ.copy()
        environment["ASTER_SERVICE_INSTANCE_ID"] = instance_id
        options = {"creationflags": 0x08000000 | 0x00000200} if os.name == "nt" else {"start_new_session": True}
        with (data_dir / "service.stdout.log").open("ab") as stdout, (data_dir / "service.stderr.log").open("ab") as stderr:
            child = subprocess.Popen(argv, cwd=data_dir, env=environment, stdin=subprocess.DEVNULL,
                                     stdout=stdout, stderr=stderr, **options)
        process = psutil.Process(child.pid)
        deadline = time.monotonic() + timeout
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        address = f"[{host}]" if ":" in host else host
        try:
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    raise RuntimeError(f"Service exited with code {child.returncode}; see service.stderr.log")
                status = service_status(data_dir)
                if status.get("instance_id") == instance_id and status["running"]:
                    try:
                        with opener.open(f"http://{address}:{port}/health", timeout=0.5) as response:
                            payload = json.loads(response.read(65536))
                        if payload.get("status") == "ok" and payload.get("instance_id") == instance_id:
                            return {**status, "healthy": True}
                    except (OSError, ValueError):
                        pass  # bounded readiness polling; no credentials or response bodies are logged
                time.sleep(0.05)
            raise TimeoutError("The spawned service did not confirm its instance_id on /health")
        except BaseException:
            _terminate_owned_tree(process)
            child.wait(timeout=10)
            raise


def stop_service(data_dir: Path, *, timeout=15, force=True) -> dict:
    data_dir = Path(data_dir).resolve()
    status = service_status(data_dir)
    if not status["running"]:
        return {"stopped": not status["locked"], "already_stopped": True}
    if status.get("kind") != "daemon":
        raise RuntimeError("A foreground runtime owns this directory; cancel it in its terminal")
    process = _same_process(status)
    if process is None:
        if not service_status(data_dir)["locked"]:
            return {"stopped": True, "forced": False, "instance_id": status["instance_id"]}
        raise RuntimeError("Service identity changed; refusing to signal it")
    _json_write(data_dir / "service.stop.json", {"instance_id": status["instance_id"]})
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            exited = not process.is_running() or process.status() == psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            exited = True
        if exited:
            return {"stopped": True, "forced": False, "instance_id": status["instance_id"]}
        time.sleep(0.05)
    current = service_status(data_dir)
    if current.get("instance_id") != status["instance_id"] and current["running"]:
        raise RuntimeError("A different service instance started; refusing to signal it")
    if not force:
        return {"stopped": False, "forced": False, "instance_id": status["instance_id"],
                "reason": "graceful_stop_timeout"}
    _terminate_owned_tree(process)
    return {"stopped": True, "forced": True, "instance_id": status["instance_id"]}


def doctor(data_dir: Path) -> dict:
    checks = {"python": {"available": sys.version_info >= (3, 12), "version": sys.version.split()[0]},
              "powershell7": {"available": bool(shutil.which("pwsh"))},
              "powershell51": {"available": bool(shutil.which("powershell"))},
              "api_key_environment": {"configured": bool(os.environ.get("DEEPSEEK_API_KEY"))}}
    for package, module in (("pypdf", "pypdf"), ("python-docx", "docx"), ("openpyxl", "openpyxl"),
                            ("python-pptx", "pptx"), ("playwright", "playwright"),
                            ("pypdfium2", "pypdfium2"), ("pillow", "PIL")):
        available = importlib.util.find_spec(module) is not None
        checks[package] = {"available": available,
                           "version": importlib.metadata.version(package) if available else None}
    try:
        with contextlib.closing(sqlite3.connect(":memory:")) as db:
            db.execute("CREATE VIRTUAL TABLE test USING fts5(text,tokenize='trigram')")
        checks["sqlite_fts5_trigram"] = {"available": True, "version": sqlite3.sqlite_version}
    except sqlite3.Error:
        checks["sqlite_fts5_trigram"] = {"available": False, "version": sqlite3.sqlite_version}
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            checks["chromium"] = {"available": Path(playwright.chromium.executable_path).is_file()}
    except (ImportError, RuntimeError, OSError):
        checks["chromium"] = {"available": False}
    checks["wsl"] = {"available": bool(shutil.which("wsl")), "distributions": []}
    if checks["wsl"]["available"]:
        try:
            result = subprocess.run(["wsl", "--list", "--quiet"], capture_output=True, timeout=5, check=False,
                                    creationflags=0x08000000 if os.name == "nt" else 0)
            encoding = "utf-16-le" if b"\0" in result.stdout else "utf-8"
            checks["wsl"]["distributions"] = result.stdout.decode(encoding, errors="replace").strip().splitlines()
        except (OSError, subprocess.TimeoutExpired):
            checks["wsl"]["probe_failed"] = True
    credential_file = Path(data_dir).resolve() / "credentials.yaml"
    linked = credential_file.is_symlink() or (hasattr(credential_file, "is_junction") and credential_file.is_junction())
    checks["credential_store"] = {"available": not linked, "backend": "local_file", "path": str(credential_file),
                                  "exists": credential_file.is_file(), "credentials_read": False}
    return {"data_dir": str(Path(data_dir).resolve()), "checks": checks,
            "credentials_read": False, "network_requests": False}


def _connect_readonly(path: Path):
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    db.execute("PRAGMA trusted_schema=OFF")
    return db


def validate_database(path: Path) -> dict:
    with contextlib.closing(_connect_readonly(path)) as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version not in range(1, SCHEMA_VERSION + 1):
            raise ValueError(f"Backup schema {version} is outside supported schemas 1..{SCHEMA_VERSION}")
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Database integrity check failed")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"sessions", "runs", "messages", "events", "tool_executions", "checkpoints", "pending_requests"}
        if version >= 2:
            required |= {"session_lineage", "mailbox_lifecycle", "automation_routes", "file_observations"}
        if not required <= tables:
            raise ValueError("Database is missing runtime tables")
    return {"schema_version": version, "sha256": digest(path), "size": path.stat().st_size}


def _upgrade_restored_database(path: Path) -> None:
    """Upgrade only the staged copy. Immutable source backups remain byte-identical."""
    from aster.state import Store
    if validate_database(path)["schema_version"] < SCHEMA_VERSION:
        store = Store(path)
        store.close()
        with contextlib.closing(sqlite3.connect(path)) as db:
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            db.execute("PRAGMA journal_mode=DELETE")
    if validate_database(path)["schema_version"] != SCHEMA_VERSION:
        raise ValueError("Staged restore did not migrate to the current schema")


def backup_database(data_dir: Path, destination: Path) -> Path:
    source = Path(data_dir).resolve() / "runtime.db"
    destination = Path(destination).resolve()
    if destination == source:
        raise ValueError("Backup destination cannot be the live database")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".aster-db-backup-", suffix=".db", dir=destination.parent)
    os.close(fd)
    try:
        with contextlib.closing(_connect_readonly(source)) as original, contextlib.closing(sqlite3.connect(temporary)) as snapshot:
            original.backup(snapshot)
            snapshot.execute("PRAGMA journal_mode=DELETE")
        validate_database(Path(temporary))
        os.replace(temporary, destination)
        return destination
    finally:
        Path(temporary).unlink(missing_ok=True)


def restore_database(data_dir: Path, backup_path: Path) -> dict:
    data_dir, backup_path = Path(data_dir).resolve(), Path(backup_path).resolve()
    validate_database(backup_path)
    with FileLock(data_dir / ".service.lock", timeout=0):
        target = data_dir / "runtime.db"
        if target == backup_path:
            raise ValueError("Cannot restore a database onto itself")
        data_dir.mkdir(parents=True, exist_ok=True)
        rollback = None
        if target.exists():
            rollback = data_dir / "backups" / f"before-restore-{uuid.uuid4().hex}.db"
            backup_database(data_dir, rollback)
            with contextlib.closing(sqlite3.connect(target, timeout=1)) as old:
                if old.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] != 0:
                    raise RuntimeError("Database has active readers/writers; restore refused")
                old.execute("PRAGMA journal_mode=DELETE")
        fd, temporary = tempfile.mkstemp(prefix=".aster-restore-", suffix=".db", dir=data_dir)
        os.close(fd)
        try:
            with contextlib.closing(_connect_readonly(backup_path)) as source, contextlib.closing(sqlite3.connect(temporary)) as copied:
                source.backup(copied)
                copied.execute("PRAGMA journal_mode=DELETE")
            _upgrade_restored_database(Path(temporary))
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(target) + suffix)
                if sidecar.exists():
                    sidecar.unlink()
            os.replace(temporary, target)
            return {"restored": str(target), "rollback_database": str(rollback) if rollback else None,
                    "scope": "database_only"}
        finally:
            Path(temporary).unlink(missing_ok=True)


def _registered_artifacts(database: Path) -> dict[str, dict]:
    result = {}

    def register(value):
        if not isinstance(value, dict) or not isinstance(value.get("artifact_id"), str) or not isinstance(value.get("path"), str):
            raise ValueError("Malformed authoritative artifact record")  # noqa: TRY004 - persisted record validation.
        existing = result.get(value["artifact_id"])
        if existing and Path(existing["path"]) != Path(value["path"]):
            raise ValueError("Artifact ID has conflicting registered paths")
        result[value["artifact_id"]] = value

    def slots(value):
        for record in value.get("artifacts", []):
            register(record)
        if value.get("output_artifact"):
            register(value["output_artifact"])

    def message(value):
        slots(value.get("metadata", {}))

    with contextlib.closing(_connect_readonly(database)) as db:
        for row in db.execute("SELECT message_json FROM messages"):
            message(json.loads(row[0]))
        for row in db.execute("SELECT result_json FROM tool_executions WHERE result_json IS NOT NULL"):
            slots(json.loads(row[0]))
        for row in db.execute("SELECT payload_json FROM events WHERE type='artifact.registered'"):
            slots(json.loads(row[0]))
        for row in db.execute("SELECT messages_json FROM checkpoints"):
            for item in json.loads(row[0]):
                message(item)
    return result


def registered_artifacts(database: Path) -> dict[str, dict]:
    """Return only explicit runtime artifact slots, never look-alike application data."""
    return _registered_artifacts(database)


def resolve_artifact(data_dir: Path, *, artifact_id: str | None = None, path: str | None = None) -> Path:
    data_dir = Path(data_dir).resolve()
    if not artifact_id:
        raise ValueError("Artifact ID is required")
    registered = _registered_artifacts(data_dir / "runtime.db").get(artifact_id)
    if registered is None:
        raise KeyError("Unknown artifact ID")
    if path is not None and Path(path) != Path(registered["path"]):
        raise PermissionError("Artifact path does not match its registered ID")
    mapping_file = data_dir / "restore-artifacts.json"
    mappings = _read_json(mapping_file).get("artifact_map", {}) if mapping_file.exists() else {}
    mapped = mappings.get(artifact_id)
    if mapped and Path(mapped["original_path"]) != Path(registered["path"]):
        raise PermissionError("Restored artifact mapping disagrees with the original record")
    target = Path(mapped["path"] if mapped else registered["path"]).resolve(strict=True)
    if not target.is_relative_to((data_dir / "artifacts").resolve()):
        raise PermissionError("Artifact is outside the runtime artifact directory")
    if not target.is_file():
        raise ValueError("Artifact is not a regular file")
    return target


def prune_orphan_artifacts(data_dir: Path, min_age_seconds: float, *, dry_run=True) -> dict:
    """Offline, explicit artifact GC; registered history and restore mappings are roots."""
    if not math.isfinite(min_age_seconds) or min_age_seconds < 0:
        raise ValueError("min_age_seconds must be finite and non-negative")
    data_dir = Path(data_dir).resolve()
    with FileLock(data_dir / ".service.lock", timeout=0):
        validate_database(data_dir / "runtime.db")
        root = data_dir / "artifacts"
        records = _registered_artifacts(data_dir / "runtime.db")
        protected = {Path(record["path"]).resolve() for record in records.values()}
        mapping_file = data_dir / "restore-artifacts.json"
        if mapping_file.exists():
            mappings = _read_json(mapping_file).get("artifact_map")
            if not isinstance(mappings, dict):
                raise ValueError("Malformed artifact mapping; cleanup refused")
            for mapping in mappings.values():
                if not isinstance(mapping, dict) or not isinstance(mapping.get("path"), str):
                    raise ValueError("Malformed artifact mapping; cleanup refused")  # noqa: TRY004 - corrupt persisted data
                protected.add(Path(mapping["path"]).resolve())
        candidates = []
        snapshots = {}
        now = time.time()
        if root.exists():
            if root.is_symlink() or root.is_junction():
                raise ValueError("Linked artifact directories cannot be pruned")
            for current, directories, files in os.walk(root, followlinks=False):
                for name in [*directories, *files]:
                    target = Path(current, name)
                    if target.is_symlink() or target.is_junction():
                        raise ValueError("Artifact tree contains a link; cleanup refused")
                    if not target.resolve().is_relative_to(root):
                        raise ValueError("Artifact path escapes its owned directory")
                for name in files:
                    target = Path(current, name)
                    if not target.is_file() or name.endswith(".lock") or target.resolve() in protected:
                        continue
                    status = target.stat()
                    if now - status.st_mtime < min_age_seconds:
                        continue
                    relative = target.relative_to(data_dir).as_posix()
                    candidates.append({"path": relative, "bytes": status.st_size})
                    snapshots[relative] = (status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns)
        report = {"operation": "prune_orphan_artifacts", "dry_run": bool(dry_run), "created_at": now,
                  "min_age_seconds": min_age_seconds, "candidates": candidates, "deleted": [],
                  "skipped": [], "errors": [], "state": "planned"}
        audit = data_dir / "logs" / "maintenance" / f"artifact-prune-{uuid.uuid4().hex}.json"
        report["audit_path"] = str(audit)
        _json_write(audit, report)
        if not dry_run:
            for candidate in candidates:
                relative = candidate["path"]
                target = data_dir / relative
                try:
                    # External editors do not take our lease. Keep a changed file.
                    status = target.stat()
                    if (target.is_symlink() or target.is_junction() or
                            not target.resolve().is_relative_to(root) or
                            (status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns) != snapshots[relative]):
                        report["skipped"].append(relative)
                    else:
                        target.unlink()
                        report["deleted"].append(relative)
                except OSError as error:
                    report["errors"].append({"path": relative, "error": type(error).__name__})
                _json_write(audit, report)
        report["state"] = "completed" if not report["errors"] else "completed_with_errors"
        _json_write(audit, report)
        return report


BUNDLE_DIRECTORIES = ("agents", "projects", "skills", "memory", "artifacts")
BUNDLE_FILES = ("config.yaml", "credentials.yaml", "commands.yaml", "pricing.yaml", "prices.yaml",
                "INSTRUCTIONS.md", "MEMORY.md", "HEARTBEAT.md")


def backup_bundle(data_dir: Path, destination: Path) -> Path:
    data_dir, destination = Path(data_dir).resolve(), Path(destination).resolve()
    if destination.is_relative_to(data_dir) and not destination.is_relative_to(data_dir / "backups"):
        raise ValueError("Inside data_dir, backup archives must be placed in backups/")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".aster-bundle-", dir=destination.parent) as scratch_name:
        scratch = Path(scratch_name)
        snapshot = backup_database(data_dir, scratch / "runtime.db")
        records = _registered_artifacts(snapshot)
        artifacts = {}
        for identity, metadata in records.items():
            actual = resolve_artifact(data_dir, artifact_id=identity, path=metadata["path"])
            artifacts[identity] = {"relative_path": actual.relative_to(data_dir).as_posix(),
                                   "original_path": metadata["path"]}
        sources = {"runtime.db": snapshot}
        for filename in BUNDLE_FILES:
            candidate = data_dir / filename
            if candidate.exists():
                if candidate.is_symlink() or (hasattr(candidate, "is_junction") and candidate.is_junction()):
                    raise ValueError("Bundle refuses linked configuration files")
                sources[filename] = candidate
        for directory in BUNDLE_DIRECTORIES:
            root = data_dir / directory
            if not root.exists():
                continue
            if root.is_symlink() or (hasattr(root, "is_junction") and root.is_junction()):
                raise ValueError("Bundle refuses linked runtime directories")
            for current, subdirs, files in os.walk(root, followlinks=False):
                for name in [*subdirs, *files]:
                    candidate = Path(current, name)
                    if candidate.is_symlink() or (hasattr(candidate, "is_junction") and candidate.is_junction()):
                        raise ValueError("Bundle refuses symlinks/junctions; copy runtime assets into owned directories")
                for name in files:
                    candidate = Path(current, name)
                    if name.endswith(".lock"):
                        continue
                    sources[candidate.relative_to(data_dir).as_posix()] = candidate
        manifest = {"format_version": 1, "schema_version": validate_database(snapshot)["schema_version"], "source_data_dir": str(data_dir),
                    "created_at": time.time(), "files": {}, "artifacts": artifacts,
                    "included": ["runtime.db", *BUNDLE_DIRECTORIES, *BUNDLE_FILES],
                    "excluded": ["user workspaces", "browser_profiles", "OS credentials", "local API token (.api-token)", "logs", "backups"]}
        package = scratch / "backup.zip"
        with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for relative, source in sorted(sources.items()):
                before = digest(source)
                archive.write(source, relative)
                if digest(source) != before:
                    raise RuntimeError(f"File changed during backup: {relative}; retry")
                manifest["files"][relative] = {"sha256": before, "size": source.stat().st_size}
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        os.replace(package, destination)
    return destination


def restore_bundle(backup_path: Path, destination: Path, *, max_bytes=5_000_000_000) -> dict:
    """Validate in a sibling staging directory, then atomically publish a new data_dir."""
    backup_path, destination = Path(backup_path).resolve(), Path(destination).resolve()
    if destination.exists():
        raise FileExistsError("Complete restore requires a new data directory; old state is never overwritten")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".aster-bundle-restore-", dir=destination.parent) as scratch_name:
        stage = Path(scratch_name) / "data"
        stage.mkdir()
        with zipfile.ZipFile(backup_path) as archive:
            if archive.getinfo("manifest.json").file_size > 10_000_000:
                raise ValueError("Backup manifest exceeds budget")
            manifest = json.loads(archive.read("manifest.json"))
            if manifest.get("format_version") != 1 or manifest.get("schema_version") not in range(1, SCHEMA_VERSION + 1):
                raise ValueError("Unsupported bundle format or database schema")
            entries = archive.infolist()
            if len({entry.filename for entry in entries}) != len(entries):
                raise ValueError("Duplicate archive member")
            expected = set(manifest["files"]) | {"manifest.json"}
            if {entry.filename for entry in entries} != expected:
                raise ValueError("Archive members disagree with manifest")
            if sum(entry.file_size for entry in entries) > max_bytes:
                raise ValueError("Backup exceeds configured restore byte budget")
            normalized = set()
            for relative, evidence in manifest["files"].items():
                parts = PurePosixPath(relative)
                if parts.is_absolute() or ".." in parts.parts or "\\" in relative or ":" in relative:
                    raise ValueError("Unsafe archive path")
                if not parts.parts or (parts.parts[0] not in BUNDLE_DIRECTORIES and
                                       str(parts) not in {"runtime.db", *BUNDLE_FILES}):
                    raise ValueError("Archive contains data outside the declared runtime scope")
                key = str(parts).casefold()
                if key in normalized or any(part.rstrip(" .") != part for part in parts.parts):
                    raise ValueError("Ambiguous Windows archive path")
                normalized.add(key)
                target = stage.joinpath(*parts.parts)
                if not target.resolve().is_relative_to(stage.resolve()):
                    raise ValueError("Archive escapes staging directory")
                native_target = _filesystem_path(target)
                native_target.parent.mkdir(parents=True, exist_ok=True)
                entry = archive.getinfo(relative)
                if entry.file_size != evidence["size"]:
                    raise ValueError("Archive size disagrees with manifest")
                with archive.open(entry) as source, native_target.open("xb") as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
                if digest(native_target) != evidence["sha256"]:
                    raise ValueError(f"Backup checksum mismatch: {relative}")
        if validate_database(stage / "runtime.db")["schema_version"] != manifest["schema_version"]:
            raise ValueError("Bundle manifest and database schema disagree")
        _upgrade_restored_database(stage / "runtime.db")
        registered = _registered_artifacts(stage / "runtime.db")
        if set(registered) != set(manifest.get("artifacts", {})):
            raise ValueError("Bundle artifact registry disagrees with database")
        artifact_map = {}
        for identity, source in manifest["artifacts"].items():
            relative = PurePosixPath(source["relative_path"])
            if not relative.parts or relative.parts[0] != "artifacts" or ".." in relative.parts:
                raise ValueError("Artifact mapping is outside artifacts/")
            if source["original_path"] != registered[identity]["path"] or str(relative) not in manifest["files"]:
                raise ValueError("Artifact mapping does not match registered evidence")
            if not _filesystem_path(stage.joinpath(*relative.parts)).is_file():
                raise ValueError("Registered artifact is absent from bundle")
            artifact_map[identity] = {"original_path": source["original_path"],
                                      "path": str(destination.joinpath(*relative.parts))}
        _json_write(stage / "restore-artifacts.json", {"format_version": 1, "artifact_map": artifact_map,
                    "source_data_dir": manifest["source_data_dir"], "restored_at": time.time()})
        _json_write(stage / "restore-manifest.json", manifest)
        os.rename(stage, destination)
    return {"restored": str(destination), "artifacts": len(artifact_map), "old_directory_unchanged": True,
            "configuration_action": "Start with --data-dir pointing at this new directory; review workspace paths"}


def _task_name(data_dir: Path):
    return "Aster-" + hashlib.sha256(str(Path(data_dir).resolve()).casefold().encode()).hexdigest()[:16]


def autostart_definition(data_dir: Path, argv: list[str]) -> dict:
    if not argv:
        raise ValueError("Autostart requires explicit argv")
    namespace = "http://schemas.microsoft.com/windows/2004/02/mit/task"
    ET.register_namespace("", namespace)
    task = ET.Element(f"{{{namespace}}}Task", {"version": "1.2"})
    triggers = ET.SubElement(task, "Triggers")
    trigger = ET.SubElement(triggers, "LogonTrigger")
    ET.SubElement(trigger, "Enabled").text = "true"
    user_id = psutil.Process().username()
    ET.SubElement(trigger, "UserId").text = user_id
    principals = ET.SubElement(task, "Principals")
    principal = ET.SubElement(principals, "Principal", {"id": "Author"})
    ET.SubElement(principal, "UserId").text = user_id
    ET.SubElement(principal, "LogonType").text = "InteractiveToken"
    ET.SubElement(principal, "RunLevel").text = "LeastPrivilege"
    settings = ET.SubElement(task, "Settings")
    ET.SubElement(settings, "MultipleInstancesPolicy").text = "IgnoreNew"
    ET.SubElement(settings, "DisallowStartIfOnBatteries").text = "false"
    ET.SubElement(settings, "StopIfGoingOnBatteries").text = "false"
    ET.SubElement(settings, "ExecutionTimeLimit").text = "PT0S"
    actions = ET.SubElement(task, "Actions", {"Context": "Author"})
    action = ET.SubElement(actions, "Exec")
    executable = Path(argv[0])
    # pythonw avoids a visible terminal at logon; normal explicit daemon start
    # separately uses CREATE_NO_WINDOW. Refuse to assume pythonw exists.
    pythonw = executable.with_name("pythonw.exe")
    ET.SubElement(action, "Command").text = str(pythonw if os.name == "nt" and pythonw.exists() else executable)
    ET.SubElement(action, "Arguments").text = subprocess.list2cmdline(argv[1:])
    ET.SubElement(action, "WorkingDirectory").text = str(Path(data_dir).resolve())
    return {"task_name": _task_name(data_dir), "xml": ET.tostring(task, encoding="unicode"),
            "data_dir": str(Path(data_dir).resolve()), "scope": "current-user logon"}


def install_autostart(data_dir: Path, argv: list[str], *, dry_run=False) -> dict:
    definition = autostart_definition(data_dir, argv)
    if dry_run:
        return {**definition, "installed": False, "dry_run": True}
    if os.name != "nt":
        raise OSError("Task Scheduler registration requires Windows")
    data_dir = Path(data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="aster-autostart-") as directory:
        path = Path(directory) / "task.xml"
        path.write_text(definition["xml"], encoding="utf-16")
        subprocess.run(["schtasks.exe", "/Create", "/TN", definition["task_name"], "/XML", str(path), "/F"],
                       check=True, capture_output=True, timeout=15, creationflags=0x08000000)
    return {"task_name": definition["task_name"], "installed": True}


def remove_autostart(data_dir: Path, *, dry_run=False) -> dict:
    name = _task_name(data_dir)
    if dry_run:
        return {"task_name": name, "removed": False, "dry_run": True}
    if os.name != "nt":
        raise OSError("Task Scheduler registration requires Windows")
    subprocess.run(["schtasks.exe", "/Delete", "/TN", name, "/F"], check=True,
                   capture_output=True, timeout=15, creationflags=0x08000000)
    return {"task_name": name, "removed": True}


# Full portable bundles are the user-facing default. Explicit *_database calls
# remain available for same-directory repair and version rollback.
backup = backup_bundle
restore = restore_bundle
