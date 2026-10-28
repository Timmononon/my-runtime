import json
import os
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
import yaml

from aster.api import create_app
from aster.customization.credentials import CredentialError, CredentialStore
from aster.maintenance import doctor
from aster.packages.manager import SkillPackageManager
from aster.service import RunCoordinator

SECRET = "offline-file-credential-unique-secret"
TOKEN = "file-credential-api-token-at-least-thirty-two-characters"


@pytest.fixture(autouse=True)
def no_default_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTER_HOME", str(tmp_path / "default home"))
    monkeypatch.delenv("ASTER_DATA_DIR", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    def forbidden_vault(*args):
        raise AssertionError("File storage must not consult the actual OS vault")

    monkeypatch.setattr("keyring.get_password", forbidden_vault)
    monkeypatch.setattr("keyring.set_password", forbidden_vault)
    monkeypatch.setattr("keyring.delete_password", forbidden_vault)


def test_missing_file_is_read_only_and_explicit_home_is_isolated(tmp_path):
    selected = tmp_path / "selected home"
    store = CredentialStore(data_dir=selected)
    assert store.storage == "file" and store.get(use_environment=False) is None
    with pytest.raises(CredentialError, match="not configured"):
        store.resolve("keyring:aster/missing")
    assert not selected.exists() and not (tmp_path / "default home").exists()


def test_file_round_trip_preserves_legacy_reference_names_and_other_services(tmp_path):
    store = CredentialStore(data_dir=tmp_path)
    store.set("provider-old-reference", SECRET)
    other = CredentialStore("paper-search", data_dir=tmp_path)
    other.set("authorization", "Bearer offline-skill-token")
    fresh = CredentialStore(data_dir=tmp_path)
    assert fresh.resolve("keyring:aster/provider-old-reference") == SECRET
    assert fresh.resolve("keyring:provider-old-reference") == SECRET
    assert fresh.resolve("keyring:paper-search/authorization") == "Bearer offline-skill-token"
    assert yaml.safe_load((tmp_path / "credentials.yaml").read_text(encoding="utf-8")) == {
        "version": 1, "services": {"aster": {"provider-old-reference": SECRET},
                                  "paper-search": {"authorization": "Bearer offline-skill-token"}}}
    assert not (tmp_path / "config.yaml").exists()
    assert fresh.delete("provider-old-reference")
    assert not fresh.delete("provider-old-reference")
    assert fresh.resolve("keyring:paper-search/authorization") == "Bearer offline-skill-token"
    assert SECRET not in (tmp_path / "credentials.yaml").read_text(encoding="utf-8")
    if os.name != "nt":
        assert (tmp_path / "credentials.yaml").stat().st_mode & 0o777 == 0o600


def test_independent_file_writers_do_not_lose_accounts(tmp_path):
    def save(index):
        CredentialStore(data_dir=tmp_path).set(f"account-{index}", f"offline-value-{index}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(save, range(16)))
    store = CredentialStore(data_dir=tmp_path)
    for index in range(16):
        assert store.get(f"account-{index}") == f"offline-value-{index}"
    assert not list(tmp_path.glob(".credentials.yaml.*.tmp"))


@pytest.mark.parametrize("document", [
    f"version: 1\nservices: [{{secret: '{SECRET}'}}\n",
    f"version: 1\nservices: {{aster: {{provider: '{SECRET}'}}}}\nextra: [",
    f"version: 1\nservices:\n  aster:\n    provider: |\n      {SECRET}\n      second-line\n",
])
def test_malformed_file_errors_never_echo_key_or_parser_context(tmp_path, document):
    (tmp_path / "credentials.yaml").write_text(document, encoding="utf-8")
    store = CredentialStore(data_dir=tmp_path)
    for operation in (lambda: store.get("provider"), lambda: store.set("next", "offline-next"),
                      lambda: store.resolve("keyring:aster/provider")):
        with pytest.raises(CredentialError) as captured:
            operation()
        assert SECRET not in "".join(traceback.format_exception(captured.value))
    assert (tmp_path / "credentials.yaml").read_text(encoding="utf-8") == document


async def test_provider_api_writes_local_file_without_key_in_wire_database_or_config(tmp_path):
    coordinator = RunCoordinator(tmp_path, tools=[])
    app = create_app(coordinator, token=TOKEN)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost",
                                    headers={"Authorization": "Bearer " + TOKEN}) as client:
            response = await client.patch("/providers/deepseek", json={"expected_revision": 1, "api_key": SECRET})
            assert response.status_code == 200 and response.json()["credential_source"] == "file"
            assert SECRET not in response.text
            for endpoint in ("/providers", "/ui/bootstrap", "/health"):
                assert SECRET not in (await client.get(endpoint)).text
            assert SECRET in (tmp_path / "credentials.yaml").read_text(encoding="utf-8")
            assert SECRET not in coordinator.store.path.read_bytes().decode(errors="ignore")
            assert not (tmp_path / "config.yaml").exists()
            reference = coordinator.providers._row("deepseek")["credential_ref"]
            assert coordinator.credentials.resolve("keyring:aster/" + reference) == SECRET
            assert (await client.request("DELETE", "/providers/deepseek/key", json={"expected_revision": 2})).status_code == 200
            assert SECRET not in (tmp_path / "credentials.yaml").read_text(encoding="utf-8")
    finally:
        await coordinator.close()


async def test_corrupt_file_api_error_does_not_echo_key(tmp_path):
    coordinator = RunCoordinator(tmp_path, tools=[])
    (tmp_path / "credentials.yaml").write_text(f"version: 1\nservices: [ '{SECRET}'\n", encoding="utf-8")
    app = create_app(coordinator, token=TOKEN)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost",
                                    headers={"Authorization": "Bearer " + TOKEN}) as client:
            response = await client.patch("/providers/deepseek", json={"expected_revision": 1, "api_key": "offline-replacement"})
            assert response.status_code == 503 and SECRET not in response.text
            assert response.json()["detail"] == "The local credential configuration file is unavailable"
    finally:
        await coordinator.close()


def test_doctor_reports_file_metadata_without_reading_keys(tmp_path, monkeypatch):
    path = tmp_path / "credentials.yaml"
    path.write_text("invalid YAML intentionally does not need parsing", encoding="utf-8")
    original = Path.read_text

    def protected_read(candidate, *args, **kwargs):
        if candidate == path:
            raise AssertionError("Doctor must never read credential content")
        return original(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", protected_read)
    result = doctor(tmp_path)
    assert result["checks"]["credential_store"] == {
        "available": True, "backend": "local_file", "path": str(path.resolve()), "exists": True, "credentials_read": False}


def test_package_preview_refuses_local_credentials_file(tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    (package / "SKILL.md").write_text("---\nname: example\ndescription: offline fixture\n---\nExample", encoding="utf-8")
    (package / "credentials.yaml").write_text(f"version: 1\nservices: {{aster: {{key: {SECRET}}}}}\n", encoding="utf-8")
    manager = SkillPackageManager(tmp_path / "runtime")
    with pytest.raises(ValueError, match="cannot be packaged"):
        manager.preview(package)
    assert SECRET not in json.dumps(list((tmp_path / "runtime" / "skills").rglob("*")), default=str)
