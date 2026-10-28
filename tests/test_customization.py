import asyncio
import os
import sys

import pytest

from aster.config import load_config
from aster.customization.commands import CommandRegistry
from aster.customization.credentials import CredentialStore, resolve_environment
from aster.customization.discovery import search_tools
from aster.customization.hooks import HookCommand, HookManager, supervised_hook_executor
from aster.customization.skills import SkillRegistry, SkillScope, UseSkillTool
from aster.domain import ToolContext, ToolSpec
from aster.memory import MemoryService


@pytest.mark.asyncio
async def test_history_search_is_sql_scoped_for_project_and_projectless_sessions(tmp_path):
    from aster.domain import Message
    from aster.memory.service import MemoryService
    from aster.state import Store

    store = Store(tmp_path / "runtime.db")
    try:
        sessions = {}
        for project in (None, "projectA", "projectB"):
            session = store.create_session(tmp_path, agent_id="same-agent", project_id=project)
            sessions[project] = session["id"]
            store.append_message(session["id"], None, Message("user", "scope-marker " + str(project)))
        other = store.create_session(tmp_path, agent_id="other-agent")
        store.append_message(other["id"], None, Message("user", "scope-marker other"))
        for project, session_id in sessions.items():
            memory = MemoryService(tmp_path / "memory", agent_id="same-agent", project_id=project,
                                   history_search=store.search_messages)
            matches = await memory.search_history("scope-marker", limit=1)
            assert [m["session_id"] for m in matches] == [session_id]
    finally:
        store.close()

def context(tmp_path, services=None):
    return ToolContext("s", "r", tmp_path, tmp_path / "artifacts", asyncio.Event(), services=services or {})


def test_config_snapshot_permissions_cannot_expand(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "model: base\npermissions:\n  allowed_tools: [read, write]\n  denied_tools: [delete]\n",
        encoding="utf-8",
    )
    agent = tmp_path / "agents" / "research"
    agent.mkdir(parents=True)
    (agent / "config.yaml").write_text(
        "model: agent\npermissions:\n  allowed_tools: [read, delete]\n", encoding="utf-8"
    )
    (agent / "AGENT.md").write_text("Research rules", encoding="utf-8")
    snapshot = load_config(
        tmp_path, agent_id="research", overrides={"permissions": {"allowed_tools": ["read", "shell"]}}
    )
    assert snapshot.allowed_tools == {"read"}
    assert snapshot.denied_tools == {"delete"}
    assert "Research rules" in snapshot.instructions
    local = snapshot.data
    local["model"] = "mutated"
    assert snapshot.data["model"] == "agent"
    with pytest.raises(ValueError, match="credential"):
        load_config(tmp_path, overrides={"provider": {"api_key": "SECRET"}})


def setup_skill(tmp_path):
    directory = tmp_path / "skills" / "research"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        "---\nname: research\ndescription: Search papers\nallowed-tools: [file_read]\nsecrets:\n  PAPER_KEY: env:PAPER_KEY\n---\nRead reference.md\n",
        encoding="utf-8",
    )
    (directory / "reference.md").write_text("Reference", encoding="utf-8")
    registry = SkillRegistry(directory.parent)
    registry.reload()
    return registry


@pytest.mark.asyncio
async def test_skill_execution_constraint_and_secret_scope(tmp_path):
    registry = setup_skill(tmp_path)
    scope = SkillScope(registry, secret_resolver=lambda ref: "top-secret")
    result = await UseSkillTool().execute({"name": "research"}, context(tmp_path, {"skill_scope": scope}))
    assert not result.is_error
    assert scope.check_tool("file_read") and not scope.check_tool("shell_exec")
    assert scope.environment({"PATH": "keep"}) == {"PATH": "keep"}
    assert scope.environment({}, skill_name="research") == {"PAPER_KEY": "top-secret"}
    assert "top-secret" not in result.content
    assert "PAPER_KEY" not in os.environ
    assert registry.get("research").read_reference("reference.md") == "Reference"
    with pytest.raises((ValueError, OSError)):
        registry.get("research").read_reference("../../outside.txt")


@pytest.mark.asyncio
async def test_hook_denial_timeout_and_recursion(tmp_path):
    async def deny(command, payload, ctx):
        return {"returncode": 0, "stdout": '{"allow":false,"reason":"blocked"}'}

    hooks = HookManager([HookCommand("PreToolUse", "python")], deny)
    decision = await hooks.run("PreToolUse", {}, context(tmp_path))
    assert not decision.allow and decision.reason == "blocked"

    async def slow(*args):
        await asyncio.sleep(1)

    timed = HookManager([HookCommand("PreToolUse", "python", timeout_seconds=0.01)], slow)
    assert not (await timed.run("PreToolUse", {}, context(tmp_path))).allow


@pytest.mark.asyncio
async def test_real_supervised_hook(tmp_path):
    from aster.platform.processes import ProcessSupervisor
    from aster.state.store import Store

    script = tmp_path / "hook.py"
    script.write_text(
        "import json,os\nfrom pathlib import Path\np=json.loads(Path(os.environ['ASTER_HOOK_INPUT']).read_text())\nprint(json.dumps({'allow':p['tool_name']=='read'}))\n",
        encoding="utf-8",
    )
    supervisor = ProcessSupervisor()
    store = Store(tmp_path / "runtime.db")
    session = store.create_session(tmp_path)
    run, _ = store.create_run(session["id"])
    ctx = ToolContext(
        session["id"], run["id"], tmp_path, tmp_path / "artifacts", asyncio.Event(), services={"store": store}
    )
    try:
        hooks = HookManager(
            [HookCommand("PreToolUse", sys.executable, (str(script),))], supervised_hook_executor(supervisor)
        )
        result = await hooks.run("PreToolUse", {"tool_name": "write", "tool_call_id": "one"}, ctx)
        assert not result.allow
        second = await hooks.run("PreToolUse", {"tool_name": "write", "tool_call_id": "one"}, ctx)
        assert not second.allow
        assert len(store.tool_executions(run["id"])) == 1
        assert store.tool_executions(run["id"])[0]["state"] == "committed"
    finally:
        await supervisor.close()
        store.close()


@pytest.mark.asyncio
async def test_memory_scope_version_and_history_filter(tmp_path):
    calls = []

    def history(query, **kwargs):
        calls.append((query, kwargs))
        return []

    a = MemoryService(tmp_path, agent_id="a", project_id="p", history_search=history)
    b = MemoryService(tmp_path, agent_id="b")
    first = a.write("agent", "MEMORY", "已确认结果", source="session:s/message:1")
    assert not b.read()["exists"]
    assert a.search("结果")[0]["scope"] == "agent"
    with pytest.raises(ValueError, match="changed"):
        a.write("agent", "MEMORY", "new", source="s")
    a.write("agent", "MEMORY", "updated", source="s", expected_version=first["version"])
    assert await a.search_history("find") == []
    assert calls[0][1] == {"agent_id": "a", "project_id": "p", "limit": 20}
    with pytest.raises(ValueError):
        a.read("other")


def test_commands_are_text_and_tool_search_deterministic():
    command = CommandRegistry({"review": "Review $target"})
    assert command.expand("review", {"target": "$(danger)"}) == "Review $(danger)"
    specs = [ToolSpec("shell_exec", "Execute a shell command", {}), ToolSpec("file_read", "Read a file", {})]
    assert search_tools(specs, "read")[0]["name"] == "file_read"
    assert search_tools(specs, "select:shell_exec")[0]["parameters"] == {}


def test_credentials_use_backend_and_do_not_write_config(monkeypatch):
    class Backend:
        def __init__(self):
            self.values = {}

        def get_password(self, service, name):
            return self.values.get((service, name))

        def set_password(self, service, name, value):
            self.values[service, name] = value

        def delete_password(self, service, name):
            del self.values[service, name]

    store = CredentialStore(backend=Backend())
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert not store.is_configured()
    store.set("deepseek", "secret")
    assert store.get() == "secret"
    assert store.resolve("keyring:aster/deepseek") == "secret"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "override")
    assert store.get() == "override"
    assert store.delete()
    assert store.get(use_environment=False) is None


def test_sensitive_environment_uses_references_in_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("PRIVATE_TEST_TOKEN", "private-test-secret")
    with pytest.raises(ValueError, match="references"):
        load_config(
            tmp_path,
            overrides={"mcp_servers": [{"name": "test", "env": {"ACCESS_TOKEN": "private-test-secret"}}]},
        )
    config = {
        "mcp_servers": [
            {"name": "test", "env": {"ACCESS_TOKEN": "env:PRIVATE_TEST_TOKEN", "LANG": "en_US.UTF-8"}}
        ]
    }
    snapshot = load_config(tmp_path, overrides=config)
    assert "private-test-secret" not in snapshot._json
    env = snapshot.data["mcp_servers"][0]["env"]
    child = resolve_environment(env)
    assert child["ACCESS_TOKEN"] == "private-test-secret"
    assert env["ACCESS_TOKEN"] == "env:PRIVATE_TEST_TOKEN"
    assert "ACCESS_TOKEN" not in os.environ


def test_project_cannot_change_credential_endpoint(tmp_path):
    with pytest.raises(ValueError, match="only sends credentials"):
        load_config(tmp_path, overrides={"base_url": "https://attacker.invalid"})


def test_auto_approval_grants_only_come_from_global(tmp_path):
    assert (
        load_config(tmp_path, overrides={"permissions": {"auto_approve_tools": ["shell_exec"]}}).data[
            "permissions"
        ]["auto_approve_tools"]
        == []
    )
    (tmp_path / "config.yaml").write_text(
        "permissions:\n  auto_approve_tools: [file_write, process_exec]\n  ask_tools: [process_exec]\n",
        encoding="utf-8",
    )
    snapshot = load_config(
        tmp_path, overrides={"permissions": {"auto_approve_tools": ["file_write", "shell_exec"]}}
    )
    assert snapshot.data["permissions"]["auto_approve_tools"] == ["file_write"]
    assert (
        load_config(tmp_path, overrides={"permissions": {"auto_approve_tools": []}}).data["permissions"][
            "auto_approve_tools"
        ]
        == []
    )


def test_custom_command_errors_are_explicit_and_dollar_escape_is_literal():
    commands = CommandRegistry({"review": "Review ${target}; literal $$HOME"})
    assert commands.expand("review", {"target": "$(no execution)"}) == "Review $(no execution); literal $HOME"
    with pytest.raises(ValueError, match="Missing"):
        commands.expand("review", {})
    with pytest.raises(ValueError, match="Unknown command arguments"):
        commands.expand("review", {"target": "x", "typo": "y"})
    with pytest.raises(TypeError):
        CommandRegistry({"bad": {"script": "not supported"}})
