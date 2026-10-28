"""Installed-package probe launched only inside verify_web_wheel's temporary cwd."""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient

import aster
from aster.api import create_app
from aster.config import load_config
from aster.customization.credentials import CredentialStore
from aster.paths import resolve_data_dir
from aster.service import RunCoordinator


def check():
    target, version = Path(sys.argv[1]).resolve(), sys.argv[2]
    assert Path(aster.__file__).resolve().is_relative_to(target)
    assert aster.__version__ == version
    token = "temporary-wheel-test-token-with-at-least-thirty-two-characters"
    headers = {"Authorization": "Bearer " + token}
    with tempfile.TemporaryDirectory(prefix="aster-wheel-upgrade-") as directory:
        home, cwd = Path(directory) / "new-home", Path(directory) / "project"
        cwd.mkdir()
        (cwd / "AGENTS.md").write_text("INSTALLED_PROJECT_RULE", encoding="utf-8")
        home.mkdir()
        sid,job_id="ses_installed_legacy","auto_installed_legacy"
        raw=json.dumps({"role":"user","content":"Original immutable upgrade history","tool_calls":[],"tool_call_id":None,"provider_items":[],"metadata":{}})
        with closing(sqlite3.connect(home/"runtime.db")) as db,db:
            db.executescript("""
            CREATE TABLE sessions(id TEXT PRIMARY KEY,cwd TEXT NOT NULL,agent_id TEXT NOT NULL,project_id TEXT,title TEXT NOT NULL,created_at REAL NOT NULL,updated_at REAL NOT NULL);
            CREATE TABLE messages(seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT NOT NULL UNIQUE,session_id TEXT NOT NULL REFERENCES sessions(id),run_id TEXT,role TEXT NOT NULL,content TEXT NOT NULL,tool_call_id TEXT,message_json TEXT NOT NULL,created_at REAL NOT NULL);
            CREATE TABLE workbench_projects(id TEXT PRIMARY KEY,name TEXT NOT NULL,cwd TEXT NOT NULL,created_at REAL NOT NULL,updated_at REAL NOT NULL,removed_at REAL);
            CREATE TABLE session_lineage(child_id TEXT PRIMARY KEY,parent_id TEXT NOT NULL,boundary_seq INTEGER NOT NULL,kind TEXT NOT NULL,created_at REAL NOT NULL,closed_at REAL);
            CREATE TABLE automation_meta(version INTEGER NOT NULL);
            INSERT INTO automation_meta VALUES(1);
            CREATE TABLE automation_jobs(id TEXT PRIMARY KEY,kind TEXT NOT NULL,session_id TEXT NOT NULL,prompt TEXT NOT NULL,config_json TEXT NOT NULL,enabled INTEGER NOT NULL,next_fire REAL,last_fire REAL,observation_hash TEXT,result_hash TEXT,created_at REAL NOT NULL,updated_at REAL NOT NULL);
            CREATE TABLE automation_triggers(id TEXT PRIMARY KEY,job_id TEXT NOT NULL,trigger_key TEXT NOT NULL,scheduled_at REAL NOT NULL,state TEXT NOT NULL,run_id TEXT,prompt TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,last_status TEXT,error TEXT,UNIQUE(job_id,trigger_key));
            PRAGMA user_version=1;
            """)
            db.execute("INSERT INTO workbench_projects VALUES(?,?,?,?,?,NULL)",("project-installed","Legacy project",str(cwd),1.,1.))
            db.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?)",(sid,str(cwd),"default","project-installed","Legacy",1.,1.))
            db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?)",(1,"msg_installed",sid,None,"user","Original immutable upgrade history",None,raw,1.))
            config={"cron":"0 0 1 1 *","timezone":"Asia/Shanghai","overlap":"skip","misfire":"coalesce","max_runs_per_day":1,"max_pending":1,"catchup_limit":1,"max_delivery_attempts":3,"grace_seconds":60}
            db.execute("INSERT INTO automation_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(job_id,"schedule",sid,"Disabled legacy fixture",json.dumps(config),0,1900000000.,None,None,None,1.,1.))
            original=[tuple(row) for row in db.execute("SELECT * FROM messages ORDER BY seq")]
            assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='session_lifecycle'").fetchone()
            assert "created_at" not in {row[1] for row in db.execute("PRAGMA table_info(automation_triggers)")}
        CredentialStore(data_dir=home).set("synthetic-seeded-upgrade","synthetic-seeded-upgrade-secret")
        second = RunCoordinator(home, tools=[], credentials=CredentialStore(data_dir=home))
        assert [tuple(row) for row in second.store.connection.execute("SELECT * FROM messages ORDER BY seq")] == original
        assert second.store.connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert second.automation.get_job(job_id)["config"]["session_mode"] == "source"
        assert second.automation.get_job(job_id)["config"]["selection_mode"] == "fixed"
        assert "created_at" in {row[1] for row in second.store.connection.execute("PRAGMA table_info(automation_triggers)")}
        assert "execution_json" in {row[1] for row in second.store.connection.execute("PRAGMA table_info(automation_triggers)")}
        assert second.credentials.get("synthetic-seeded-upgrade",use_environment=False)=="synthetic-seeded-upgrade-secret"
        assert "INSTALLED_PROJECT_RULE" in load_config(home, cwd=cwd).instructions
        assert resolve_data_dir(home) == home.resolve()
        app = create_app(second, token=token)
        with TestClient(app, base_url="http://127.0.0.1:8765") as client:
            page = client.get("/")
            assert page.status_code == 200
            static = target / "aster/web/static"
            assets = sorted(path for path in (static / "assets").iterdir() if path.is_file())
            for asset in assets:
                response = client.get("/assets/" + asset.name)
                assert response.status_code == 200 and response.content == asset.read_bytes()
            assert client.get("/sessions").status_code == 401
            assert client.get("/health").json()["provider_configured"] is False
            assert client.get("/capabilities", headers=headers).json()["capabilities"]
            management = client.get("/management", headers=headers)
            assert management.status_code == 200 and management.json()["agents"][0]["id"] == "default"
            assert client.get(f"/browser-control/sessions/{sid}", headers=headers).status_code == 200
            assert client.get(f"/automations/{job_id}/triggers", headers=headers).json()["triggers"] == []
            assert client.get("/workbench/projects", headers=headers).json()["projects"][0]["id"] == "project-installed"
            assert client.post(f"/sessions/{sid}/archive", headers=headers).status_code == 200
            archived = client.get("/sessions?state=archived", headers=headers).json()["sessions"]
            assert any(session["id"] == sid for session in archived)
            assert client.post(f"/sessions/{sid}/unarchive", headers=headers).status_code == 200
            query = client.post(f"/customization/sessions/{sid}/memory_query", headers=headers,
                                json={"arguments": {"mode": "direct_relation", "relation": "owner", "scopes": ["agent"]}, "approved": False})
            assert query.status_code == 200 and not query.json()["is_error"],query.text
            record = client.post("/management/credentials", headers=headers, json={"expected_revision": management.json()["revision"],
                "account": "synthetic-delivery-only", "value": "synthetic-local-delivery-secret"})
            assert record.status_code == 201 and "synthetic-local-delivery-secret" not in record.text
            assert (home / "credentials.yaml").is_file()
            assert "synthetic-local-delivery-secret" not in client.get("/management", headers=headers).text
            assert "synthetic-local-delivery-secret" not in client.get(f"/sessions/{sid}", headers=headers).text
        with closing(sqlite3.connect(home / "runtime.db")) as db:
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert [tuple(row) for row in db.execute("SELECT * FROM messages WHERE id='msg_installed'")] == original
    print(json.dumps({"installed_static_http": "passed", "all_lazy_asset_http": len(assets),
        "missing_provider_not_faked": True, "installed_management_memory_browser_routes": "passed",
        "installed_legacy_schema_upgrade": "passed", "raw_upgrade_history_preserved": True,
        "installed_project_instructions": "passed", "synthetic_file_credentials_write_only": "passed"}))


if __name__ == "__main__":
    check()
