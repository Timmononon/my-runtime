import contextlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import textwrap
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import psutil
import pytest

from aster.domain import Message
from aster.maintenance import (
    ServiceLease,
    autostart_definition,
    backup_bundle,
    backup_database,
    doctor,
    install_autostart,
    prune_orphan_artifacts,
    remove_autostart,
    resolve_artifact,
    restore_bundle,
    restore_database,
    service_status,
    start_service,
    stop_service,
    validate_database,
)
from aster.state.store import SCHEMA_VERSION, Store


def runtime_data(path):
    (path / "workspace").mkdir(parents=True, exist_ok=True)
    store = Store(path / "runtime.db")
    session = store.create_session(path / "workspace")
    return store, session["id"]


def test_consistent_wal_snapshot_and_offline_restore(tmp_path):
    data = tmp_path / "data"
    store, session = runtime_data(data)
    store.append_message(session, None, Message("user", "before backup"))
    snapshot = backup_database(data, tmp_path / "snapshot.db")
    store.append_message(session, None, Message("user", "after backup"))
    store.close()
    result = restore_database(data, snapshot)
    assert Path(result["rollback_database"]).is_file()
    restored = Store(data / "runtime.db")
    assert [item.content for item in restored.history(session)] == ["before backup"]
    restored.close()
    rollback = Store(result["rollback_database"])
    assert len(rollback.history(session)) == 2
    rollback.close()


def test_restore_rejects_active_runtime_corruption_and_wrong_schema(tmp_path):
    data = tmp_path / "data"
    store, _ = runtime_data(data)
    store.close()
    snapshot = backup_database(data, tmp_path / "snapshot.db")
    with ServiceLease(data, host=None, port=None), pytest.raises(TimeoutError):
        restore_database(data, snapshot)
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not sqlite")
    before = (data / "runtime.db").read_bytes()
    with pytest.raises(sqlite3.DatabaseError):
        restore_database(data, corrupt)
    assert (data / "runtime.db").read_bytes() == before
    with contextlib.closing(sqlite3.connect(snapshot)) as db:
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    with pytest.raises(ValueError, match="schema"):
        restore_database(data, snapshot)
    assert (data / "runtime.db").read_bytes() == before


def test_failed_database_replace_preserves_old_data(tmp_path, monkeypatch):
    data = tmp_path / "data"
    store, session = runtime_data(data)
    store.append_message(session, None, Message("user", "before"))
    snapshot = backup_database(data, tmp_path / "snapshot.db")
    store.append_message(session, None, Message("user", "keep newer"))
    store.close()
    original = os.replace

    def fail_commit(source, destination):
        if Path(destination) == data / "runtime.db":
            raise PermissionError("simulated occupied database")
        return original(source, destination)

    monkeypatch.setattr("aster.maintenance.os.replace", fail_commit)
    with pytest.raises(PermissionError):
        restore_database(data, snapshot)
    preserved = Store(data / "runtime.db")
    assert len(preserved.history(session)) == 2
    preserved.close()


def test_complete_bundle_restores_artifacts_without_rewriting_history(tmp_path):
    data = tmp_path / "原始 Data"
    store, session = runtime_data(data)
    asset = data / "artifacts" / "session" / "image.png"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"sample asset")
    metadata = {"artifacts": [{"artifact_id": "asset1", "path": str(asset), "mime_type": "image/png"}]}
    store.append_message(session, None, Message("tool", "original path retained", metadata=metadata))
    (data / "config.yaml").write_text("model: deepseek-flash\n", encoding="utf-8")
    (data / "commands.yaml").write_text("commands:\n  summary: 总结当前项目\n", encoding="utf-8")
    (data / "pricing.yaml").write_text("version: 1\nmodels: {}\n", encoding="utf-8")
    (data / "prices.yaml").write_text("version: canonical-v1\ncurrency: USD\nmodels: {}\n", encoding="utf-8")
    (data / ".api-token").write_text("private-local-api-token", encoding="utf-8")
    (data / "agents" / "research").mkdir(parents=True)
    (data / "agents" / "research" / "AGENT.md").write_text("research", encoding="utf-8")
    (data / "browser_profiles").mkdir()
    (data / "browser_profiles" / "secret-cookie").write_text("excluded")
    package = backup_bundle(data, tmp_path / "complete.zip")
    store.close()
    with zipfile.ZipFile(package) as archive:
        assert not any(name.startswith("browser_profiles/") for name in archive.namelist())
        assert ".api-token" not in archive.namelist()
        assert not any(b"private-local-api-token" in archive.read(name) for name in archive.namelist())
    new_data = tmp_path / "恢复 New"
    result = restore_bundle(package, new_data)
    assert result["artifacts"] == 1 and result["old_directory_unchanged"]
    assert asset.read_bytes() == b"sample asset"
    resolved = resolve_artifact(new_data, artifact_id="asset1", path=str(asset))
    assert resolved.is_relative_to(new_data / "artifacts")
    assert resolved.read_bytes() == b"sample asset"
    restored = Store(new_data / "runtime.db")
    assert restored.history(session)[0].metadata["artifacts"][0]["path"] == str(asset)
    restored.close()
    assert (new_data / "agents/research/AGENT.md").read_text() == "research"
    assert (new_data / "commands.yaml").read_bytes() == (data / "commands.yaml").read_bytes()
    assert (new_data / "pricing.yaml").read_bytes() == (data / "pricing.yaml").read_bytes()
    assert (new_data / "prices.yaml").read_bytes() == (data / "prices.yaml").read_bytes()
    assert not (new_data / ".api-token").exists()
    with pytest.raises(KeyError):
        resolve_artifact(new_data, artifact_id="unknown", path=str(asset))
    with pytest.raises(PermissionError):
        resolve_artifact(new_data, artifact_id="asset1", path=str(tmp_path / "different"))
    with pytest.raises(FileExistsError):
        restore_bundle(package, new_data)
    # Re-backing-up a restored database must preserve its original references
    # while exporting the new physical files, rather than requiring old paths.
    second = backup_bundle(new_data, tmp_path / "second.zip")
    third = tmp_path / "third"
    restore_bundle(second, third)
    assert resolve_artifact(third, artifact_id="asset1", path=str(asset)).read_bytes() == b"sample asset"
    assert (third / "commands.yaml").read_bytes() == (data / "commands.yaml").read_bytes()
    assert (third / "pricing.yaml").read_bytes() == (data / "pricing.yaml").read_bytes()
    assert (third / "prices.yaml").read_bytes() == (data / "prices.yaml").read_bytes()


def test_corrupt_bundle_never_publishes_destination(tmp_path):
    data = tmp_path / "data"
    store, _ = runtime_data(data)
    store.close()
    package = backup_bundle(data, tmp_path / "good.zip")
    corrupt = tmp_path / "corrupt.zip"
    with zipfile.ZipFile(package) as original, zipfile.ZipFile(corrupt, "w") as changed:
        for name in original.namelist():
            changed.writestr(name, b"corrupt" if name == "runtime.db" else original.read(name))
    destination = tmp_path / "never-published"
    with pytest.raises(ValueError):
        restore_bundle(corrupt, destination)
    assert not destination.exists()
    assert validate_database(data / "runtime.db")["schema_version"] == SCHEMA_VERSION


def test_orphan_prune_protects_registered_restore_and_recent_files(tmp_path):
    data = tmp_path / "data"
    store, session = runtime_data(data)
    root = data / "artifacts"
    root.mkdir()
    files = {name: root / name for name in ("registered.txt", "mapped.txt", "orphan.txt", "recent.txt", "persistent.lock")}
    for path in files.values():
        path.write_text("keep except old orphan", encoding="utf-8")
        if path.name != "recent.txt":
            os.utime(path, (time.time() - 172800, time.time() - 172800))
    store.append_message(session, None, Message("tool", "result", metadata={"artifacts": [
        {"artifact_id": "registered", "path": str(files["registered.txt"])}]}))
    store.close()
    (data / "restore-artifacts.json").write_text(json.dumps({"artifact_map": {
        "restored": {"path": str(files["mapped.txt"]), "original_path": "D:/old/mapped.txt"}}}), encoding="utf-8")
    preview = prune_orphan_artifacts(data, 86400)
    assert [item["path"] for item in preview["candidates"]] == ["artifacts/orphan.txt"]
    assert not preview["deleted"] and all(path.exists() for path in files.values())
    actual = prune_orphan_artifacts(data, 86400, dry_run=False)
    assert actual["deleted"] == ["artifacts/orphan.txt"] and not actual["errors"]
    assert all(path.exists() for name, path in files.items() if name != "orphan.txt")
    audit = json.loads(Path(actual["audit_path"]).read_text(encoding="utf-8"))
    assert audit["deleted"] == actual["deleted"] and audit["state"] == "completed"


def test_orphan_prune_rejects_active_instance_and_malformed_mapping(tmp_path):
    store, _ = runtime_data(tmp_path)
    store.close()
    with ServiceLease(tmp_path, host=None, port=None), pytest.raises(TimeoutError):
        prune_orphan_artifacts(tmp_path, 0, dry_run=False)
    (tmp_path / "restore-artifacts.json").write_text('{"artifact_map":null}', encoding="utf-8")
    with pytest.raises(ValueError, match="mapping"):
        prune_orphan_artifacts(tmp_path, 0, dry_run=False)
    with pytest.raises(ValueError, match="non-negative"):
        prune_orphan_artifacts(tmp_path, -1)


def test_single_instance_lease_and_nonce_stop(tmp_path):
    lease = ServiceLease(tmp_path, host=None, port=None).acquire()
    try:
        status = service_status(tmp_path)
        assert status["running"] and status["kind"] == "foreground"
        with pytest.raises(TimeoutError):
            ServiceLease(tmp_path).acquire()
        (tmp_path / "service.stop.json").write_text(json.dumps({"instance_id": "wrong"}))
        assert not lease.stop_requested()
        (tmp_path / "service.stop.json").write_text(json.dumps({"instance_id": lease.instance_id}))
        assert lease.stop_requested()
    finally:
        lease.release()
    assert not service_status(tmp_path)["running"]


def test_stop_service_accepts_exit_between_running_and_status(tmp_path, monkeypatch):
    class ExitingProcess:
        def is_running(self):
            return True

        def status(self):
            raise psutil.NoSuchProcess(12345)

    monkeypatch.setattr("aster.maintenance.service_status", lambda _: {
        "running": True, "locked": True, "kind": "daemon", "instance_id": "owned"})
    monkeypatch.setattr("aster.maintenance._same_process", lambda _: ExitingProcess())
    result = stop_service(tmp_path)
    assert result == {"stopped": True, "forced": False, "instance_id": "owned"}


def test_stop_service_accepts_exit_before_identity_check(tmp_path, monkeypatch):
    statuses = iter([{"running": True, "locked": True, "kind": "daemon", "instance_id": "owned"},
                     {"running": False, "locked": False}])
    monkeypatch.setattr("aster.maintenance.service_status", lambda _: next(statuses))
    monkeypatch.setattr("aster.maintenance._same_process", lambda _: None)
    assert stop_service(tmp_path) == {"stopped": True, "forced": False, "instance_id": "owned"}


def dummy_service(tmp_path, *, wrong_identity=False, ignore_stop=False):
    script = tmp_path / "dummy service.py"
    script.write_text(textwrap.dedent(f"""
        import json,sys,time,subprocess
        from pathlib import Path
        from http.server import BaseHTTPRequestHandler,HTTPServer
        from aster.maintenance import ServiceLease
        data=Path(sys.argv[1]); port=int(sys.argv[2])
        with ServiceLease(data,port=port) as lease:
            child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(90)'])
            (data/'child.pid').write_text(str(child.pid))
            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(200); self.end_headers()
                    self.wfile.write(json.dumps({{'status':'ok','instance_id': {'"wrong"' if wrong_identity else 'lease.instance_id'}}}).encode())
                def log_message(self,*args): pass
            server=HTTPServer(('127.0.0.1',port),Handler); server.timeout=0.1
            try:
                while {ignore_stop!s} or not lease.stop_requested(): server.handle_request()
            finally:
                server.server_close(); child.terminate(); child.wait(timeout=5)
    """), encoding="utf-8")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return script, port


@pytest.mark.windows
@pytest.mark.skipif(os.name != "nt", reason="Hidden Windows process lifecycle")
@pytest.mark.parametrize("forced", [False, True])
def test_hidden_service_start_stop_no_orphans(tmp_path, forced):
    data = tmp_path / "中文 Data"
    script, port = dummy_service(tmp_path, ignore_stop=forced)
    status = start_service(data, [sys.executable, str(script), str(data), str(port)], port=port, timeout=8)
    child_pid = int((data / "child.pid").read_text())
    try:
        assert status["healthy"] and service_status(data)["running"]
    finally:
        stopped = stop_service(data, timeout=0.3 if forced else 5)
    assert stopped["stopped"] and stopped["forced"] is forced
    assert not psutil.pid_exists(status["pid"])
    assert not psutil.pid_exists(child_pid)


@pytest.mark.windows
@pytest.mark.skipif(os.name != "nt", reason="Hidden Windows process lifecycle")
def test_graceful_timeout_never_terminates_owned_service(tmp_path, monkeypatch):
    data = tmp_path / "graceful Data"
    script, port = dummy_service(tmp_path, ignore_stop=True)
    status = start_service(data, [sys.executable, str(script), str(data), str(port)], port=port, timeout=8)
    child_pid = int((data / "child.pid").read_text())
    original = __import__("aster.maintenance", fromlist=["_terminate_owned_tree"])._terminate_owned_tree

    def prohibited(_):
        raise AssertionError("force=False must never terminate a process")

    try:
        monkeypatch.setattr("aster.maintenance._terminate_owned_tree", prohibited)
        result = stop_service(data, timeout=0.2, force=False)
        assert result["reason"] == "graceful_stop_timeout" and not result["stopped"] and not result["forced"]
        assert service_status(data)["instance_id"] == status["instance_id"]
        assert psutil.pid_exists(status["pid"]) and psutil.pid_exists(child_pid)
    finally:
        monkeypatch.setattr("aster.maintenance._terminate_owned_tree", original)
        stop_service(data, timeout=0.1)
    assert not psutil.pid_exists(status["pid"]) and not psutil.pid_exists(child_pid)


@pytest.mark.windows
@pytest.mark.skipif(os.name != "nt", reason="Hidden Windows process lifecycle")
def test_wrong_health_instance_rejected_and_reaped(tmp_path):
    data = tmp_path / "data"
    script, port = dummy_service(tmp_path, wrong_identity=True)
    with pytest.raises(TimeoutError):
        start_service(data, [sys.executable, str(script), str(data), str(port)], port=port, timeout=1)
    assert not service_status(data)["running"]
    assert not psutil.pid_exists(int((data / "child.pid").read_text()))


def test_doctor_does_not_read_or_expose_credentials(tmp_path, monkeypatch):
    sentinel = "do-not-print-private-token"
    monkeypatch.setenv("DEEPSEEK_API_KEY", sentinel)

    class Backend:
        priority = 1

        def get_password(self, *args):
            raise AssertionError("doctor must not read vault values")

    monkeypatch.setattr("keyring.get_keyring", lambda: Backend())
    report = doctor(tmp_path)
    assert sentinel not in json.dumps(report)
    assert report["checks"]["api_key_environment"]["configured"]
    assert report["credentials_read"] is False
    assert report["checks"]["sqlite_fts5_trigram"]["available"]


def test_autostart_is_dry_run_only_and_xml_quotes_paths(tmp_path, monkeypatch):
    data = tmp_path / "中文 Data"
    argv = [sys.executable, "-m", "aster", "--data-dir", str(data), "daemon", "serve"]

    def prohibited(*args, **kwargs):
        raise AssertionError("dry-run must not call Task Scheduler")

    monkeypatch.setattr("aster.maintenance.subprocess.run", prohibited)
    report = install_autostart(data, argv, dry_run=True)
    assert report["dry_run"] and not report["installed"]
    tree = ET.fromstring(report["xml"])
    namespace = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    arguments = tree.find("t:Actions/t:Exec/t:Arguments", namespace).text
    assert f'"{data}"' in arguments
    assert tree.find("t:Principals/t:Principal/t:UserId", namespace).text
    assert not remove_autostart(data, dry_run=True)["removed"]
    assert autostart_definition(data, argv)["task_name"] == report["task_name"]


@pytest.mark.windows
@pytest.mark.skipif(os.name != "nt", reason="PowerShell script validation")
def test_powershell_install_dry_run_with_unicode_paths(tmp_path):
    executable = shutil.which("pwsh") or shutil.which("powershell")
    assert executable
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([executable, "-NoProfile", "-NonInteractive", "-File",
                             str(root / "scripts/Install-Aster.ps1"), "-ProjectRoot", str(root),
                             "-VenvPath", str(tmp_path / "中文 虚拟环境"), "-DryRun"],
                            capture_output=True, check=False, timeout=10, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["VirtualEnvironment"] == str(tmp_path / "中文 虚拟环境")
    assert not report["GlobalEnvironmentChanged"] and not report["AutostartEnabled"]
