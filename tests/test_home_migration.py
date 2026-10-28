"""Offline home relocation with real API/history and explicit credential export."""
from __future__ import annotations

import asyncio
import hashlib
import json
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import yaml

from aster.api import create_app
from aster.config import load_config
from aster.customization.credentials import CredentialStore
from aster.customization.skills import SkillRegistry
from aster.domain import ModelTurn
from aster.engine import FakeModel
from aster.maintenance import backup_bundle, resolve_artifact, restore_bundle
from aster.service import RunCoordinator

TOKEN = "temporary-home-migration-token-at-least-thirty-two-characters"
SECRET = "synthetic-home-migration-key-never-a-real-credential"
REPLACEMENT = "synthetic-home-migration-replacement-never-a-real-credential"


class MemoryVault:
    """Same-account credential identity, without consulting the Windows vault."""

    def __init__(self):
        self.values = {}

    def get_password(self, service, name):
        return self.values.get((service, name))

    def set_password(self, service, name, value):
        self.values[service, name] = value


@asynccontextmanager
async def offline_runtime(home, vault=None):
    coordinator = RunCoordinator(
        home, tools=[], credentials=CredentialStore(backend=vault) if vault else CredentialStore(data_dir=home),
        model_factory=lambda _: FakeModel([ModelTurn("Offline migration answer")]),
    )
    app = create_app(coordinator, token=TOKEN)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765",
        headers={"Authorization": "Bearer " + TOKEN},
    ) as api:
        yield coordinator, api


async def completed_run(api, sid, text, **options):
    response = await api.post(f"/sessions/{sid}/runs", json={"text": text, **options})
    assert response.status_code == 202, response.text
    rid = response.json()["id"]
    for _ in range(100):
        response = await api.get(f"/runs/{rid}")
        assert response.status_code == 200, response.text
        run = response.json()
        if run["status"] not in {"queued", "running"}:
            assert run["status"] == "completed", run
            return run
        await asyncio.sleep(0.01)
    raise AssertionError("Offline migration fixture did not complete")


def file_digests(root):
    # Read-only SQLite connections can create empty WAL/shared-memory files.
    # Compare the database itself and every durable/user file, not those caches.
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*") if path.is_file() and path.name not in {"runtime.db-wal", "runtime.db-shm"}
    }


async def test_home_bundle_preserves_history_scope_assets_and_file_credentials(tmp_path, monkeypatch):
    # All roots are temporary. Neither Path.home nor a real credential backend
    # is used by this migration scenario.
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    old = tmp_path / "old-app-cache" / "Aster Data"
    stage = tmp_path / "restore-stage"
    new = tmp_path / "user-profile" / ".aster"
    workspace = tmp_path / "user-project" / "原工作目录"
    workspace.mkdir(parents=True)
    protected = workspace / "user-owned.txt"
    protected.write_text("User working files are outside the migration bundle.", encoding="utf-8")
    old.mkdir(parents=True)
    (old / ".api-token").write_text(TOKEN, encoding="utf-8")
    (old / "browser_profiles").mkdir()
    (old / "browser_profiles" / "private-cookie").write_text("fixture-private-cookie", encoding="utf-8")
    (old / "INSTRUCTIONS.md").write_text("Global home instructions.", encoding="utf-8")
    (old / "commands.yaml").write_text("commands:\n  summarize: Summarize current project\n", encoding="utf-8")
    (old / "MEMORY.md").write_text("Keep durable memory in the same home.", encoding="utf-8")
    skill_dir = old / "skills" / "home-fixture"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: home-fixture\ndescription: Home relocation fixture\nallowed-tools: []\n---\n"
        "Use the local reference.md.\n", encoding="utf-8",
    )
    (skill_dir / "reference.md").write_text("A packaged reference survives relocation.", encoding="utf-8")
    vault = MemoryVault()
    vault.values[("unrelated-application", "unrelated-account")] = "synthetic-unrelated-vault-record"
    async with offline_runtime(old, vault) as (coordinator, api):
        response = await api.post("/providers", json={
            "name": "Home migration provider", "base_url": "https://provider.example/v1",
            "protocol": "openai_chat", "api_key": SECRET,
            "models": [{"id": "migration-model", "name": "Migration Model",
                        "context_window": 40000, "max_output_tokens": 4000,
                        "input_modalities": ["text"], "effort_levels": ["high"]}],
        })
        assert response.status_code == 201, response.text
        provider = response.json()
        assert provider["configured"] and provider["credential_source"] == "vault"
        profile_row = dict(coordinator.store.connection.execute(
            "SELECT * FROM provider_profiles WHERE id=?", (provider["id"],),
        ).fetchone())
        reference = profile_row["credential_ref"]
        assert vault.values[("aster", reference)] == SECRET
        (old / "config.yaml").write_text(
            f"model: migration-model\napi_key: keyring:aster/{reference}\n"
            "skills: [home-fixture]\nmax_output_tokens: 1024\n", encoding="utf-8",
        )
        response = await api.post("/workbench/projects", json={"name": "Migration project", "cwd": str(workspace)})
        assert response.status_code == 201, response.text
        project = response.json()
        project_root = old / "projects" / project["id"]
        (project_root / "PROJECT.md").write_text("Keep this project scope.", encoding="utf-8")
        (project_root / "config.yaml").write_text(
            "max_output_tokens: 512\npermissions:\n  denied_tools: [process_exec]\n", encoding="utf-8",
        )
        response = await api.post("/sessions", json={"project_id": project["id"], "title": "Home migration history"})
        assert response.status_code == 201, response.text
        session = response.json()
        sid = session["id"]
        response = await api.post(f"/sessions/{sid}/attachments?filename=source.txt", content=b"Registered attachment content",
                                  headers={"Content-Type": "text/plain"})
        assert response.status_code == 201, response.text
        attachment = response.json()
        run_before = await completed_run(api, sid, "Read the preserved attachment", attachments=[attachment["attachment_id"]],
                                         provider_id=provider["id"], model="migration-model", selected_skill="home-fixture")
        before = (await api.get(f"/sessions/{sid}")).json()
        request_before = coordinator.store.run_request(run_before["id"])
        assert before["messages"] and request_before["selected_skill_snapshot"]["path"] == str(skill_dir / "SKILL.md")
        raw_run_before = coordinator.store.get_run(run_before["id"])
        skills_before = (await api.get("/workbench/skills")).json()["skills"]
        pins_before = coordinator.session_selection(sid)
        projects_before = (await api.get("/workbench/projects")).json()["projects"]
        assert len(projects_before) == 1 and projects_before[0]["session_count"] == 1
    vault_before = dict(vault.values)
    original_files = file_digests(old)
    working_files = file_digests(workspace)
    legacy_package = backup_bundle(old, tmp_path / "legacy-vault-migration.zip")
    with zipfile.ZipFile(legacy_package) as archive:
        assert "credentials.yaml" not in archive.namelist()
        assert all(SECRET.encode() not in archive.read(name) for name in archive.namelist())
    result = restore_bundle(legacy_package, stage)
    assert result["artifacts"] == 1 and result["old_directory_unchanged"]
    # Explicit, authorized migration exports just the profile's referenced slot.
    # Starting a file-backed Runtime must never scan or mutate another OS vault.
    legacy_credentials = CredentialStore(backend=vault)
    file_credentials = CredentialStore(data_dir=stage)
    file_credentials.set(reference, legacy_credentials.get(reference, use_environment=False))
    CredentialStore("fixture-other", data_dir=stage).set("keep-account", "synthetic-unrelated-file-record")
    assert vault.values == vault_before
    assert not (old / "credentials.yaml").exists()
    credentials_before = yaml.safe_load((stage / "credentials.yaml").read_text(encoding="utf-8"))
    assert credentials_before["version"] == 1 and credentials_before["services"]["aster"][reference] == SECRET
    package = backup_bundle(stage, tmp_path / "home-migration-with-file-credentials.zip")
    with zipfile.ZipFile(package) as archive:
        names = archive.namelist()
        assert ".api-token" not in names
        assert "credentials.yaml" in names
        assert not any(name.startswith(("browser_profiles/", "workspace/", "logs/", "backups/")) for name in names)
        assert all(SECRET.encode() not in archive.read(name) for name in names if name != "credentials.yaml")
        assert all(TOKEN.encode() not in archive.read(name) for name in names)
        assert SECRET.encode() in archive.read("credentials.yaml")
        assert any(reference.encode() in archive.read(name) for name in names)
    restored = restore_bundle(package, new)
    assert restored["restored"] == str(new) and restored["old_directory_unchanged"]
    assert restored["artifacts"] == 1
    assert file_digests(old) == original_files and file_digests(workspace) == working_files
    assert not (new / ".api-token").exists() and not (new / "browser_profiles").exists()
    resolved = resolve_artifact(new, artifact_id=attachment["attachment_id"], path=attachment["path"])
    assert resolved.is_relative_to(new / "artifacts") and resolved.read_bytes() == b"Registered attachment content"
    assert Path(attachment["path"]).read_bytes() == resolved.read_bytes()
    assert (new / "credentials.yaml").read_bytes() == (stage / "credentials.yaml").read_bytes()
    for relative in ("config.yaml", "commands.yaml", "INSTRUCTIONS.md", "MEMORY.md", f"projects/{project['id']}/config.yaml",
                     f"projects/{project['id']}/PROJECT.md", "skills/home-fixture/SKILL.md", "skills/home-fixture/reference.md"):
        assert (new / relative).read_bytes() == (old / relative).read_bytes()
    registry = SkillRegistry(new / "skills")
    registry.reload()
    assert registry.get("home-fixture").path == new / "skills/home-fixture/SKILL.md"
    assert registry.get("home-fixture").read_reference("reference.md") == "A packaged reference survives relocation."
    settings = load_config(new, project_id=project["id"], cwd=workspace)
    assert settings.data["max_output_tokens"] == 512 and "process_exec" in settings.denied_tools
    assert settings.cwd == workspace and "Keep this project scope." in settings.instructions

    async with offline_runtime(new) as (coordinator, api):
        after = (await api.get(f"/sessions/{sid}")).json()
        assert after == before
        assert coordinator.store.get_run(run_before["id"]) == raw_run_before
        assert coordinator.store.run_request(run_before["id"]) == request_before
        assert (await api.get("/workbench/projects")).json()["projects"] == projects_before
        assert (await api.get("/workbench/skills")).json()["skills"] == skills_before
        assert coordinator.session_selection(sid) == pins_before
        restored_provider = coordinator.providers.get(provider["id"])
        assert {key: value for key, value in restored_provider.items() if key != "credential_source"} == {
            key: value for key, value in provider.items() if key != "credential_source"
        }
        assert restored_provider["configured"] and restored_provider["credential_source"] == "file"
        restored_profile_row = dict(coordinator.store.connection.execute(
            "SELECT * FROM provider_profiles WHERE id=?", (provider["id"],),
        ).fetchone())
        assert restored_profile_row == profile_row
        assert coordinator.providers.resolve_pin(coordinator.providers.pin(provider["id"], "migration-model", "high")) == SECRET
        response = await api.get(f"/sessions/{sid}/attachments")
        assert response.status_code == 200 and response.json() == {"attachments": [attachment]}
        response = await api.get(f"/artifacts/{attachment['attachment_id']}")
        assert response.status_code == 200 and response.content == b"Registered attachment content"
        assert response.headers["content-disposition"].startswith("attachment;")
        # A subsequent run loads the installed skill from the new home. It does
        # not rewrite the old run's pinned configuration or prior transcript.
        following = await completed_run(api, sid, "Continue after home migration")
        assert coordinator.store.run_request(following["id"])["selected_skill_snapshot"]["path"] == str(new / "skills/home-fixture/SKILL.md")
        assert coordinator.store.get_run(run_before["id"]) == raw_run_before
        after_following = (await api.get(f"/sessions/{sid}")).json()
        assert after_following["messages"][:len(before["messages"])] == before["messages"]
        assert after_following["project_id"] == project["id"] and after_following["cwd"] == str(workspace)
        response = await api.get("/providers")
        assert response.status_code == 200
        assert SECRET not in response.text and "credential_ref" not in response.text
        assert SECRET not in json.dumps(after_following) and SECRET not in json.dumps(coordinator.store.run_request(following["id"]))
        response = await api.patch(f"/providers/{provider['id']}", json={"expected_revision": provider["revision"], "api_key": REPLACEMENT})
        assert response.status_code == 200 and response.json()["configured"]
        assert REPLACEMENT not in response.text
        revision = response.json()["revision"]
        replaced = yaml.safe_load((new / "credentials.yaml").read_text(encoding="utf-8"))
        current_reference = coordinator.store.connection.execute(
            "SELECT credential_ref FROM provider_profiles WHERE id=?", (provider["id"],),
        ).fetchone()[0]
        assert current_reference != reference and reference not in replaced["services"]["aster"]
        assert replaced["services"]["aster"][current_reference] == REPLACEMENT
        assert coordinator.providers.resolve_pin(coordinator.providers.pin(provider["id"], "migration-model", "high")) == REPLACEMENT
        response = await api.request("DELETE", f"/providers/{provider['id']}/key", json={"expected_revision": revision})
        assert response.status_code == 200 and not response.json()["configured"]
        remaining = yaml.safe_load((new / "credentials.yaml").read_text(encoding="utf-8"))
        assert current_reference not in remaining["services"].get("aster", {})
        assert remaining["services"]["fixture-other"]["keep-account"] == "synthetic-unrelated-file-record"
        assert SECRET not in (new / "credentials.yaml").read_text(encoding="utf-8")
        assert REPLACEMENT not in (new / "credentials.yaml").read_text(encoding="utf-8")
        assert coordinator.store.get_run(run_before["id"]) == raw_run_before
        assert coordinator.store.run_request(run_before["id"]) == request_before
        assert vault.values == vault_before
    async with offline_runtime(new) as (coordinator, api):
        response = await api.get("/providers")
        assert response.status_code == 200
        disconnected = next(item for item in response.json()["providers"] if item["id"] == provider["id"])
        assert not disconnected["configured"] and disconnected["credential_source"] == "unconfigured"
        assert disconnected["models"] == provider["models"] and disconnected["name"] == provider["name"]
        assert all(value not in response.text for value in (SECRET, REPLACEMENT))
    assert file_digests(old) == original_files and file_digests(workspace) == working_files
    assert vault.values == vault_before
    assert protected.read_text(encoding="utf-8") == "User working files are outside the migration bundle."
