import copy
import json
from pathlib import Path

import pytest
import yaml

from aster.config import load_config
from aster.customization.skills import SkillRegistry, SkillScope
from aster.domain import Message, ModelTurn, ToolCall, to_dict
from aster.engine import FakeModel
from aster.maintenance import backup_bundle, restore_bundle
from aster.service import RunCoordinator
from aster.state.store import SessionBusy
from aster.tools.files import build_file_tools


class NoCredentials:
    def get(self, *args, **kwargs):
        return None

    def resolve(self, reference):
        raise AssertionError("Tests must not resolve credentials")


def coordinator_at(tmp_path, *, model_factory=None, tools=None):
    return RunCoordinator(tmp_path / "state", credentials=NoCredentials(), tools=[] if tools is None else tools,
                          model_factory=model_factory or (lambda _: FakeModel([ModelTurn("done")])))


async def test_projects_persist_cwd_overrides_new_sessions_and_archive_keeps_scopes(tmp_path):
    first, second = tmp_path / "目录 one", tmp_path / "目录 two"
    first.mkdir()
    second.mkdir()
    (first / "user-owned.txt").write_text("Never delete this")
    coordinator = coordinator_at(tmp_path)
    projects = coordinator.workbench.projects
    try:
        project = projects.create("研究项目", str(first))
        config = coordinator.data_dir / "projects" / project["id"] / "config.yaml"
        config.write_text(yaml.safe_dump({"cwd": str(first), "permissions": {"denied_tools": ["process_exec"]}}), encoding="utf-8")
        old = coordinator.create_session(second, project_id=project["id"])
        assert old["cwd"] == str(first.resolve())
        coordinator.store.append_message(old["id"], None, Message("user", "Keep original project context"))
        projects.update(project["id"], name="新名字", cwd=str(second))
        fresh = coordinator.create_session(first, project_id=project["id"])
        assert fresh["cwd"] == str(second.resolve())
        assert coordinator.store.get_session(old["id"])["cwd"] == str(first.resolve())
        assert yaml.safe_load(config.read_text(encoding="utf-8"))["permissions"] == {"denied_tools": ["process_exec"]}
        original = coordinator.store.history_records(old["id"])
        removed = projects.remove(project["id"])
        assert removed["detached_sessions"] == 0 and removed["archived_sessions"] == 0
        assert removed["preserved_sessions"] == 2
        assert not projects.list()
        assert coordinator.store.get_session(old["id"])["project_id"] == project["id"]
        assert coordinator.store.history_records(old["id"]) == original
        assert (first / "user-owned.txt").read_text() == "Never delete this"
        assert config.exists()
        with pytest.raises(ValueError, match="removed"):
            coordinator.create_session(first, project_id=project["id"])
        snapshot = load_config(coordinator.data_dir, project_id=project["id"], cwd=Path(old["cwd"]))
        assert "process_exec" in snapshot.denied_tools and snapshot.project_id == project["id"]
    finally:
        await coordinator.close()
    # Deleted registration stays absent; config and old scope survive a bundle restore.
    package = backup_bundle(tmp_path / "state", tmp_path / "projects.zip")
    restored = tmp_path / "restored"
    restore_bundle(package, restored)
    new = RunCoordinator(restored, tools=[], credentials=NoCredentials())
    try:
        assert not new.workbench.projects.list()
        with pytest.raises(KeyError):
            new.workbench.projects.get(project["id"], include_removed=True)
        assert new.store.get_session(old["id"])["project_id"] == project["id"]
    finally:
        await new.close()


async def test_project_folder_browser_is_local_bounded_and_does_not_list_files(tmp_path):
    coordinator = coordinator_at(tmp_path)
    root = tmp_path / "folders"
    root.mkdir()
    (root / "private.txt").write_text("Not in chooser")
    (root / ".hidden").mkdir()
    for index in range(205):
        (root / f"folder-{index:03d}").mkdir()
    try:
        result = coordinator.workbench.projects.folders(str(root))
        assert result["path"] == str(root.resolve()) and result["parent"] == str(tmp_path.resolve())
        assert len(result["entries"]) == 200 and result["truncated"]
        assert all(Path(row["path"]).is_dir() and row["name"].startswith("folder-") for row in result["entries"])
        for path in ("relative/path", str(root / "private.txt"), "\\\\server\\share"):
            with pytest.raises((ValueError, OSError)):
                coordinator.workbench.projects.create("Invalid", path)
        assert coordinator.workbench.projects.list() == []
    finally:
        await coordinator.close()


async def test_project_delete_preserves_active_session_without_changing_its_scope(tmp_path):
    coordinator = coordinator_at(tmp_path)
    project = coordinator.workbench.projects.create("Active", str(tmp_path))
    session = coordinator.create_session(project_id=project["id"])
    run, _ = coordinator.store.create_run(session["id"], initial_status="queued")
    try:
        assert coordinator.workbench.projects.remove(project["id"])["permanent"]
        with pytest.raises(KeyError):
            coordinator.workbench.projects.get(project["id"])
        assert coordinator.store.get_session(session["id"])["project_id"] == project["id"]
        coordinator.store.set_run_status(run["id"], "cancelled")
    finally:
        await coordinator.close()


async def test_manual_compact_real_runs_preserves_archive_recent_pairs_and_avoids_model(tmp_path):
    model_calls = []
    serial = 0

    def factory(_settings):
        nonlocal serial
        serial += 1
        model = FakeModel([ModelTurn(tool_calls=[ToolCall(f"read-{serial}", "file_read", {"path": "source.txt"})]),
                           ModelTurn((f"Evidence from iteration {serial}. " + "Measured result. " * 50) * 10)])
        model_calls.append(model)
        return model

    tools = [tool for tool in build_file_tools() if tool.spec.name == "file_read"]
    coordinator = coordinator_at(tmp_path, model_factory=factory, tools=tools)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "source.txt").write_text("SOURCE FACT", encoding="utf-8")
    await coordinator.start()
    try:
        session = coordinator.create_session(workspace)
        for index in range(10):
            run = await coordinator.submit(session["id"], f"Read the source, iteration {index}; preserve source facts.")
            assert (await coordinator.wait(run["id"], timeout=3)).status == "completed"
        archive = copy.deepcopy(coordinator.store.history_records(session["id"]))
        before_calls = sum(len(model.calls) for model in model_calls)
        tail = to_dict(coordinator.store.active_history(session["id"])[-3:])
        result = await coordinator.manual_compact(session["id"])
        assert result["changed"] and result["after_tokens"] < result["before_tokens"]
        assert result["preserved_archive"] and result["archive_messages"] == len(archive)
        assert coordinator.store.history_records(session["id"]) == archive
        active = coordinator.store.active_history(session["id"])
        assert to_dict(active[-3:]) == tail
        pending = set()
        for message in active:
            pending.update(call.id for call in message.tool_calls)
            if message.role == "tool":
                assert message.tool_call_id in pending
                pending.remove(message.tool_call_id)
        assert not pending
        assert sum(len(model.calls) for model in model_calls) == before_calls
        assert "source facts" in active[0].content
        assert coordinator.store.get_checkpoint(session["id"])["metadata"]["manual_compaction"] == result
    finally:
        await coordinator.close()


async def test_manual_compact_rejects_queue_and_writer_and_reports_no_gain(tmp_path):
    coordinator = coordinator_at(tmp_path)
    session = coordinator.create_session(tmp_path)
    try:
        untouched = await coordinator.manual_compact(session["id"])
        assert not untouched["changed"] and untouched["before_tokens"] == untouched["after_tokens"]
        assert coordinator.store.get_checkpoint(session["id"]) is None
        gate = coordinator._session_gates[session["id"]]
        async with gate:
            with pytest.raises(SessionBusy, match="writer"):
                await coordinator.manual_compact(session["id"])
        run, _ = coordinator.store.create_run(session["id"], initial_status="queued")
        with pytest.raises(SessionBusy):
            await coordinator.manual_compact(session["id"])
        coordinator.store.set_run_status(run["id"], "cancelled")
        assert coordinator.store.history(session["id"]) == []
    finally:
        await coordinator.close()


def install_local_skill(data, name="local-analysis", *, allowed="file_read"):
    root = data / "skills" / name
    root.mkdir(parents=True, exist_ok=True)
    source = (f"---\nname: {name}\ndescription: Local custom skill\nallowed-tools: [{allowed}]\n"
              "secrets: {EXAMPLE: 'keyring:test/skill'}\n---\nQA_SELECTED_SKILL: preserve original measurements.\n")
    (root / "SKILL.md").write_text(source, encoding="utf-8")
    (root / "reference.txt").write_text("pinned reference", encoding="utf-8")
    return root


async def test_installed_local_skill_filtered_and_pinned_without_resolving_secrets(tmp_path):
    coordinator = coordinator_at(tmp_path)
    try:
        root = install_local_skill(coordinator.data_dir)
        install_local_skill(coordinator.data_dir, "other")
        catalog = coordinator.workbench.installed_skills()
        assert {item["name"] for item in catalog} == {"local-analysis", "other"}
        agent = coordinator.data_dir / "agents" / "scoped"
        agent.mkdir(parents=True)
        (agent / "config.yaml").write_text("skills: [local-analysis]\n")
        assert [item["name"] for item in coordinator.workbench.installed_skills(agent_id="scoped")] == ["local-analysis"]
        with pytest.raises(ValueError, match="bound"):
            coordinator.workbench.snapshot_selected_skill("other", {"skills": ["local-analysis"]})
        selected = coordinator.workbench.snapshot_selected_skill("local-analysis", {"skills": ["local-analysis"]})
        assert "keyring:test/skill" in json.dumps(selected)
        (root / "reference.txt").write_text("newer content")
        (root / "SKILL.md").write_text("---\nname: local-analysis\n---\nNewly permissive version")
        registry = SkillRegistry(coordinator.data_dir / "skills")
        registry.reload()
        scope = SkillScope(registry, bound_skills=["local-analysis"], secret_resolver=coordinator.credentials.resolve)
        pinned = coordinator.workbench.restore_selected_skill(selected, registry, scope)
        assert pinned.version == selected["version"]
        assert pinned.read_reference("reference.txt") == "pinned reference"
        assert scope.check_tool("file_read") and not scope.check_tool("file_write")
        corrupted = {**selected, "allowed_tools": None}
        with pytest.raises(ValueError, match="metadata"):
            coordinator.workbench.restore_selected_skill(corrupted, registry, scope)
    finally:
        await coordinator.close()
