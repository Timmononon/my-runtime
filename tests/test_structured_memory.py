import asyncio
import sqlite3

import pytest

from aster.domain import Message, ToolContext
from aster.memory.facts import StructuredMemory
from aster.memory.service import MemoryService
from aster.memory.tools import build_memory_tools
from aster.state import Store


def setup(tmp_path, *, agent="research", project="project-a"):
    store = Store(tmp_path / "runtime.db")
    session = store.create_session(tmp_path, agent_id=agent, project_id=project)
    run, _ = store.create_run(session["id"])
    store.append_message(session["id"], run["id"], Message("user", "Project Larch owner is Mina. Later owner is Jun."))
    markdown = MemoryService(tmp_path / "memory", agent_id=agent, project_id=project)
    return store, session, run, StructuredMemory(store, markdown)


def source(quote="Project Larch owner is Mina."):
    return [{"kind": "current_user", "quote": quote}]


def test_actual_evidence_scope_quotes_and_external_distinction(tmp_path):
    store, session, run, facts = setup(tmp_path)
    try:
        value = facts.record(scope="project", entity="Larch", relation="owner", value="Mina", sources=source(),
                             session_id=session["id"], run_id=run["id"])
        assert value["evidence"][0]["verified"]
        assert value["evidence"][0]["message_id"]
        with pytest.raises(ValueError, match="quote"):
            facts.record(scope="project", entity="Larch", relation="owner", value="Fake", sources=source("fabrication"), session_id=session["id"], run_id=run["id"])
        with pytest.raises(ValueError, match="scope"):
            facts.record(scope="agent", entity="Larch", relation="owner", value="Mina", sources=source(), session_id=session["id"], run_id=run["id"])
        other = StructuredMemory(store, MemoryService(tmp_path / "memory", agent_id="research", project_id="project-b"))
        assert other.recall(scopes=["project"])["no_data"]
        with pytest.raises(ValueError):
            other.get(value["id"], scope="project")
        external = facts.record(scope="global", entity="Public", relation="color", value="cobalt",
                                sources=[{"kind": "external", "label": "operator-supplied excerpt", "quote": "cobalt"}])
        assert external["evidence"][0]["verified"] is False
        assert other.recall(scopes=["global"])["facts"][0]["id"] == external["id"]
    finally:
        store.close()


def test_effective_time_supersede_conflicts_retraction_and_atomic_versions(tmp_path):
    store, session, run, facts = setup(tmp_path)
    try:
        common = {"scope": "project", "session_id": session["id"], "run_id": run["id"]}
        first = facts.record(entity="Larch", relation="owner", value="Mina", sources=source(),
                             observed_at="2026-01-01T00:00:00Z", **common)
        second = facts.supersede(first["id"], expected_version=1, value="Jun", sources=source("Later owner is Jun."),
                                 reason="Owner changed", valid_from="2026-02-01T00:00:00Z", **common)
        assert facts.recall(scopes=["project"], at="2026-01-15T00:00:00Z")["facts"][0]["value"] == "Mina"
        assert facts.recall(scopes=["project"], at="2026-02-01T00:00:00Z")["facts"][0]["value"] == "Jun"
        assert facts.get(first["id"], scope="project")["valid_to"] == "2026-02-01T00:00:00+00:00"
        with pytest.raises(ValueError, match="changed"):
            facts.supersede(first["id"], expected_version=1, value="Bad", sources=source(), reason="stale", **common)
        with pytest.raises(ValueError, match="effective time"):
            facts.supersede(second["id"], expected_version=1, value="Bad", sources=source(), reason="reverse", valid_from="2025-01-01T00:00:00Z", **common)
        duplicate = facts.record(entity="Larch", relation="owner", value="Mina", sources=source(), **common)
        assert duplicate["conflicts"]
        facts.forget(second["id"], scope="project", expected_version=1, reason="Retracted by user")
        assert facts.recall(scopes=["project"], at="2026-03-01T00:00:00Z")["no_data"]
        assert facts.history(second["id"], scope="project")[-1]["action"] == "retract"
        assert facts.history(second["id"], scope="project")[-1]["snapshot"]["evidence"]
    finally:
        store.close()


def test_markdown_evidence_and_consistent_database_backup(tmp_path):
    store, _, _, facts = setup(tmp_path)
    note = facts.markdown.write("global", "PUBLIC", "Larch uses cobalt", source="user request")
    fact = facts.record(scope="global", entity="Larch", relation="color", value="cobalt",
                        sources=[{"kind": "markdown", "scope": "global", "name": "PUBLIC", "version": note["version"], "quote": "Larch uses cobalt"}])
    facts.markdown.write("global", "PUBLIC", "Larch uses blue", source="revision", expected_version=note["version"])
    with pytest.raises(ValueError, match="no longer matches"):
        facts.record(scope="global", entity="Larch", relation="color", value="cobalt",
                     sources=[{"kind": "markdown", "scope": "global", "name": "PUBLIC", "version": note["version"], "quote": "Larch uses cobalt"}])
    backup = sqlite3.connect(tmp_path / "backup.db")
    store.connection.backup(backup)
    backup.close()
    store.close()
    restored = Store(tmp_path / "backup.db")
    try:
        view = StructuredMemory(restored, facts.markdown)
        assert view.get(fact["id"], scope="global")["evidence"][0]["version"] == note["version"]
        assert len(view.history(fact["id"], scope="global")) == 1
    finally:
        restored.close()


def test_historical_conflicts_include_superseded_values_effective_at_that_time(tmp_path):
    store, session, run, facts = setup(tmp_path)
    try:
        common = {"scope": "project", "session_id": session["id"], "run_id": run["id"]}
        first = facts.record(entity="Larch", relation="owner", value="Mina", sources=source(),
                             observed_at="2026-01-01T00:00:00Z", **common)
        competing = facts.record(entity="Larch", relation="owner", value="Jun", sources=source("Later owner is Jun."),
                                 observed_at="2026-01-15T00:00:00Z", **common)
        facts.supersede(first["id"], expected_version=1, value="Jun", sources=source("Later owner is Jun."),
                        reason="Resolved owner later", valid_from="2026-03-01T00:00:00Z", **common)
        history = facts.recall(scopes=["project"], at="2026-02-01T00:00:00Z")
        assert {f["status"] for f in history["facts"]} == {"current", "superseded"}
        assert set(history["conflicts"][0]["fact_ids"]) == {first["id"], competing["id"]}
    finally:
        store.close()


@pytest.mark.asyncio
async def test_tools_write_risk_and_current_user_evidence(tmp_path):
    store, session, run, facts = setup(tmp_path)
    try:
        tools = {t.spec.name: t for t in build_memory_tools()}
        assert tools["memory_record"].spec.risk == tools["memory_update"].spec.risk == tools["memory_forget"].spec.risk == "write"
        ctx = ToolContext(session["id"], run["id"], tmp_path, tmp_path / "artifacts", asyncio.Event(), services={"structured_memory": facts})
        result = await tools["memory_record"].execute({"scope": "project", "entity": "Larch", "relation": "owner", "value": "Mina", "sources": source()}, ctx)
        assert not result.is_error
        assert result.data["memory"]["evidence"][0]["verified"]
        ctx.cancel.set()
        result = await tools["memory_forget"].execute({}, ctx)
        assert result.outcome == "not_executed"
    finally:
        store.close()
