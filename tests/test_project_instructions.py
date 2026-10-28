import os
import subprocess
import sys

import pytest

from aster.config import load_config
from aster.domain import ModelTurn
from aster.engine import FakeModel
from aster.service import RunCoordinator


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8-sig")


def test_exact_cwd_rule_order_has_no_ancestor_scan_or_permission_override(tmp_path):
    home, cwd = tmp_path / "home", tmp_path / "repository" / "nested"
    write(home / "config.yaml", "permissions:\n  allowed_tools: [file_read]\n  denied_tools: [shell_exec]\n")
    paths = [home / "INSTRUCTIONS.md", home / "agents" / "research" / "AGENT.md",
             home / "projects" / "project1" / "PROJECT.md", home / "projects" / "project1" / "AGENTS.md",
             cwd / "AGENTS.md", cwd / ".aster" / "INSTRUCTIONS.md", cwd / ".aster" / "rules" / "10-one.md",
             cwd / ".aster" / "rules" / "20-two.md", cwd / ".aster" / "instructions.local.md"]
    for index, path in enumerate(paths):
        write(path, f"rule-{index}: {path.name}")
    write(cwd.parent / "AGENTS.md", "forbidden-ancestor-marker")
    write(cwd / ".aster" / "config.yaml", "permissions:\n  allowed_tools: [shell_exec]\n")
    snapshot = load_config(home, cwd=cwd, agent_id="research", project_id="project1")
    assert [snapshot.instructions.index(f"rule-{index}:") for index in range(len(paths))] == sorted(
        snapshot.instructions.index(f"rule-{index}:") for index in range(len(paths)))
    assert "forbidden-ancestor-marker" not in snapshot.instructions
    assert snapshot.allowed_tools == {"file_read"} and snapshot.denied_tools == {"shell_exec"}
    assert snapshot.cwd == cwd.resolve()
    before = snapshot.version
    write(paths[-1], "replacement-local-rule")
    assert "replacement-local-rule" not in snapshot.instructions
    assert load_config(home, cwd=cwd, agent_id="research", project_id="project1").version != before


def test_project_rules_reject_link_escape(tmp_path):
    cwd = tmp_path / "repository"
    cwd.mkdir()
    outside = tmp_path / "outside.md"
    write(outside, "outside-private-marker")
    try:
        os.symlink(outside, cwd / "AGENTS.md")
    except OSError:
        pytest.skip("Host cannot create symlinks")
    with pytest.raises(ValueError, match="inside the working directory"):
        load_config(tmp_path / "home", cwd=cwd)


@pytest.mark.windows
@pytest.mark.skipif(sys.platform != "win32", reason="Windows junction scenario")
def test_project_rules_reject_windows_junction_escape(tmp_path):
    cwd, outside = tmp_path / "repository", tmp_path / "outside"
    cwd.mkdir()
    write(outside / "INSTRUCTIONS.md", "outside-junction-marker")
    subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(cwd / ".aster"), str(outside)],
                   check=True, capture_output=True, text=True)
    with pytest.raises(ValueError, match="inside the working directory"):
        load_config(tmp_path / "home", cwd=cwd)


async def test_queued_run_keeps_loaded_cwd_rules_after_source_edit(tmp_path):
    home, cwd = tmp_path / "home", tmp_path / "repository"
    write(cwd / "AGENTS.md", "Original cwd rule.")
    write(cwd / ".aster" / "instructions.local.md", "Local cwd rule.")
    model = FakeModel([ModelTurn("offline done")])
    runtime = RunCoordinator(home, tools=[], model_factory=lambda _: model, max_parallel_runs=1)
    try:
        sid = runtime.create_session(cwd)["id"]
        await runtime._capacity.acquire()
        run = await runtime.submit(sid, "follow repository rules")
        write(cwd / "AGENTS.md", "Later edited cwd rule.")
        runtime._capacity.release()
        assert (await runtime.wait(run["id"], timeout=3)).status == "completed"
        payload = runtime.store.run_request(run["id"])
        assert "Original cwd rule." in payload["instructions"] and "Local cwd rule." in payload["instructions"]
        assert "Later edited cwd rule." not in model.calls[0]["system"]
    finally:
        if runtime._capacity.locked():
            runtime._capacity.release()
        await runtime.close()
