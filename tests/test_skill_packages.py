import asyncio
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from aster.customization.skills import SkillRegistry, SkillScope, UseSkillTool
from aster.domain import ToolContext
from aster.packages import SkillPackageManager
from aster.packages.manager import tree_digest
from aster.packages.tools import build_package_tools


def package(path, *, name="example", body="first"):
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Local workflow\n---\n{body}\n", encoding="utf-8")
    return path


def test_windows_git_checkout_preserves_bundled_package_digests(tmp_path):
    root = Path(__file__).resolve().parents[1]
    repository = tmp_path / "source"
    bundle_path = Path("src/aster/packages/bundled")
    shutil.copytree(root / bundle_path, repository / bundle_path,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy2(root / ".gitattributes", repository / ".gitattributes")
    (repository / "ordinary.txt").write_bytes(b"LF checkout control\n")
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")

    def git(*arguments, cwd=repository):
        subprocess.run(["git", "-c", "core.autocrlf=false", *arguments], cwd=cwd, env=env,
                       capture_output=True, text=True, check=True)

    git("init", "-b", "main")
    git("add", "--all")
    git("-c", "user.name=offline-test", "-c", "user.email=offline@example.invalid",
        "commit", "-qm", "Record bundle bytes")
    checkout = tmp_path / "checkout"
    git("clone", "--no-checkout", str(repository), str(checkout), cwd=tmp_path)
    git("-c", "core.autocrlf=true", "checkout", "-q", "main", cwd=checkout)

    # Prove the checkout exercised CRLF conversion outside the pinned assets.
    assert (checkout / "ordinary.txt").read_bytes() == b"LF checkout control\r\n"
    catalog = json.loads((checkout / bundle_path / "catalog.json").read_text(encoding="utf-8"))["packages"]
    assert len(catalog) == 8
    for entry in catalog:
        package_root = checkout / bundle_path / entry["name"]
        files = {path.relative_to(package_root).as_posix(): path.read_bytes()
                 for path in package_root.rglob("*") if path.is_file()}
        assert tree_digest(files) == entry["digest"], entry["name"]


@pytest.mark.asyncio
async def test_bundled_install_receipt_use_skill_reference_and_real_helper(tmp_path):
    manager = SkillPackageManager(tmp_path / "data")
    assert len(manager.catalog()) == 8
    for entry in manager.catalog():
        preview = manager.preview("bundled:" + entry["name"])
        receipt = manager.install(preview["plan_id"], expected_digest=preview["digest"])
        assert receipt["digest"] == entry["digest"]
    registry = SkillRegistry(manager.root)
    registry.reload()
    for entry in manager.catalog():
        assert registry.get(entry["name"]).version == entry["digest"]
    scope = SkillScope(registry)
    ctx = ToolContext("s", "r", tmp_path, tmp_path / "out", asyncio.Event(), services={"skill_scope": scope})
    loaded = await UseSkillTool().execute({"name": "webapp-testing", "reference": "references/test-plan.md"}, ctx)
    assert "Expected visible result" in loaded.content
    assert loaded.data["skill_directory"] == str(manager.root / "webapp-testing")
    fixture = tmp_path / "download.txt"
    fixture.write_text("actual fixture", encoding="utf-8")
    process = await asyncio.create_subprocess_exec(sys.executable, str(manager.root / "webapp-testing/scripts/check_download.py"), str(fixture), stdout=subprocess.PIPE)
    stdout, _ = await process.communicate()
    assert process.returncode == 0 and json.loads(stdout)["passed"]


@pytest.mark.asyncio
async def test_theme_and_html_helpers_execute_and_reject_external_assets(tmp_path):
    manager = SkillPackageManager(tmp_path / "data")
    for name in ("theme-factory", "web-artifacts-builder", "doc-coauthoring"):
        preview = manager.preview("bundled:" + name)
        manager.install(preview["plan_id"], expected_digest=preview["digest"])
    async def run_helper(skill, helper, *arguments):
        process = await asyncio.create_subprocess_exec(sys.executable, str(manager.root / skill / "scripts" / helper),
            *map(str, arguments), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = await process.communicate()
        return process.returncode, stdout.decode("utf-8"), stderr.decode("utf-8")
    output = tmp_path / "themes.html"
    code, _, _ = await run_helper("theme-factory", "showcase.py", "--output", output)
    assert code == 0 and output.read_text(encoding="utf-8").count("<section>") == 10
    assert (await run_helper("theme-factory", "showcase.py", "--output", output))[0] != 0
    template = manager.root / "web-artifacts-builder/templates/interactive.html"
    code, stdout, _ = await run_helper("web-artifacts-builder", "check_artifact.py", template)
    assert code == 0 and json.loads(stdout)["passed"]
    bad = tmp_path / "bad.html"
    bad.write_text(template.read_text(encoding="utf-8") + '<script src="https://example.invalid/x.js"></script>', encoding="utf-8")
    code, stdout, _ = await run_helper("web-artifacts-builder", "check_artifact.py", bad)
    assert code == 1 and "nonlocal asset" in stdout
    bad.write_text('<html lang="en"><title>T</title><main><img src="../outside.png"></main></html>', encoding="utf-8")
    code, stdout, _ = await run_helper("web-artifacts-builder", "check_artifact.py", bad)
    assert code == 1 and "out-of-scope" in stdout
    note = manager.root / "doc-coauthoring/templates/document.md"
    code, stdout, _ = await run_helper("doc-coauthoring", "check_sources.py", note)
    assert code == 0 and json.loads(stdout)["source_markers"] == ["[S1]"]
    assert json.loads(stdout)["factual_verification"] == "not_performed"


def test_upgrade_snapshot_source_change_and_uninstall(tmp_path):
    manager = SkillPackageManager(tmp_path / "data")
    source = package(tmp_path / "自建包", name="pdf")  # A user's own same-name package is allowed.
    (source / "reference.md").write_text("old evidence", encoding="utf-8")
    preview = manager.preview(source)
    first = manager.install(preview["plan_id"], expected_digest=preview["digest"])
    registry = SkillRegistry(manager.root)
    registry.reload()
    snapshot = SkillScope(registry).activate("pdf")
    (source / "reference.md").write_text("new evidence", encoding="utf-8")
    changed = manager.preview(source)
    assert changed["changed"] == ["reference.md"]
    second = manager.install(changed["plan_id"], expected_digest=changed["digest"])
    assert first["digest"] != second["digest"]
    assert snapshot.read_reference("reference.md") == "old evidence"
    pending = manager.preview(source)
    (source / "reference.md").write_text("changed after preview", encoding="utf-8")
    with pytest.raises(ValueError, match="Source changed"):
        manager.install(pending["plan_id"], expected_digest=pending["digest"])
    manager.uninstall("pdf", expected_digest=second["digest"])
    assert not (manager.root / "pdf").exists()


def test_failure_between_directory_swaps_restores_old_skill(tmp_path, monkeypatch):
    manager = SkillPackageManager(tmp_path / "data")
    source = package(tmp_path / "source")
    first = manager.preview(source)
    manager.install(first["plan_id"], expected_digest=first["digest"])
    package(source, body="new")
    second = manager.preview(source)
    replace = os.replace
    def fail_publish(src, dst):
        if Path(src).name == "new" and Path(dst).name == "example":
            raise OSError("injected publish failure")
        return replace(src, dst)
    monkeypatch.setattr(os, "replace", fail_publish)
    with pytest.raises(OSError, match="injected"):
        manager.install(second["plan_id"], expected_digest=second["digest"])
    assert manager.installed()[0]["digest"] == first["digest"]
    assert list(manager.transactions.iterdir()) == []


def test_restart_recovers_interrupted_uninstall_without_losing_receipt(tmp_path):
    manager = SkillPackageManager(tmp_path / "data")
    source = package(tmp_path / "source")
    preview = manager.preview(source)
    receipt = manager.install(preview["plan_id"], expected_digest=preview["digest"])
    # Reproduce the exact durable state after rename and before commit, then
    # discard the manager: recovery must depend on disk, not in-memory flags.
    transaction = manager.transactions / "interrupted-removal"
    transaction.mkdir()
    (transaction / "transaction.json").write_text(json.dumps({"name": "example", "action": "uninstall",
        "receipt": {"transaction_id": transaction.name, "name": "example", "digest": receipt["digest"]}}), encoding="utf-8")
    os.replace(manager.root / "example", transaction / "old")
    restarted = SkillPackageManager(tmp_path / "data")
    assert restarted.installed()[0]["digest"] == receipt["digest"]
    assert list(restarted.transactions.iterdir()) == []
    assert (restarted.receipts / (receipt["transaction_id"] + ".json")).is_file()


def test_windows_case_collision_archive_rejected(tmp_path):
    archive = tmp_path / "collision.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("SKILL.md", "---\nname: example\ndescription: Test\n---\n")
        output.writestr("reference.txt", "first")
        output.writestr("REFERENCE.txt", "ambiguous")
    with pytest.raises(ValueError, match="collision"):
        SkillPackageManager(tmp_path / "data").preview(archive)


@pytest.mark.asyncio
async def test_session_package_source_and_cross_session_plan_scope(tmp_path):
    manager = SkillPackageManager(tmp_path / "data")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = package(tmp_path / "outside", name="external")
    (outside / "confidential-merger-notes.md").write_text("private fixture", encoding="utf-8")
    ctx = ToolContext("scoped-session", "r", workspace, workspace / "artifacts", asyncio.Event(),
                      services={"skill_packages": manager, "allowed_roots": [workspace]})
    tools = {tool.spec.name: tool for tool in build_package_tools()}
    denied = await tools["skill_package_preview"].execute({"source": str(outside)}, ctx)
    assert denied.is_error and denied.error_category == "permission" and denied.outcome == "not_executed"
    assert "confidential-merger-notes" not in denied.content
    # A human management preview or another session may produce this plan; its
    # digest is integrity evidence, never authority to widen this session's roots.
    plan = manager.preview(outside)
    denied = await tools["skill_package_install"].execute({"plan_id": plan["plan_id"], "expected_digest": plan["digest"]}, ctx)
    assert denied.is_error and denied.outcome == "not_executed"
    assert not (manager.root / "external").exists()
    package(workspace / "inside", name="inside")
    accepted = await tools["skill_package_preview"].execute({"source": "inside"}, ctx)
    assert not accepted.is_error
    plan = accepted.data["package"]
    result = await tools["skill_package_install"].execute({"plan_id": plan["plan_id"], "expected_digest": plan["digest"]}, ctx)
    assert not result.is_error and (manager.root / "inside/SKILL.md").is_file()


@pytest.mark.parametrize("name", ["../outside", "/absolute", "C:/outside", "a/../outside", "AUX.txt", "x\\evil", "__pycache__/bad.pyc", ".env"])
def test_archive_paths_and_bytecode_rejected(tmp_path, name):
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("SKILL.md", "---\nname: example\ndescription: Test\n---\n")
        output.writestr(name, "payload")
    if name == "x\\evil":
        archive.write_bytes(archive.read_bytes().replace(b"x/evil", b"x\\evil"))
    with pytest.raises(ValueError):
        SkillPackageManager(tmp_path / "data").preview(archive)
    assert not (tmp_path / "outside").exists()


def test_secret_and_symlink_and_case_collision_rejected(tmp_path):
    manager = SkillPackageManager(tmp_path / "data")
    source = package(tmp_path / "source")
    secret = source / "config.txt"
    secret.write_text("ACCESS_TOKEN = literal-sensitive-value\n", encoding="utf-8")
    with pytest.raises(ValueError, match="secret"):
        manager.preview(source)
    secret.write_text("ACCESS_TOKEN = env:MY_TOKEN\n", encoding="utf-8")
    assert manager.preview(source)
    archive = tmp_path / "links.zip"
    with zipfile.ZipFile(archive, "w") as output:
        link = zipfile.ZipInfo("link")
        link.create_system = 3
        link.external_attr = 0o120777 << 16
        output.writestr(link, "outside")
    with pytest.raises(ValueError, match="link"):
        manager.preview(archive)
