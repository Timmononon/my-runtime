import argparse
import json

import httpx
import pytest

from aster.api import create_app
from aster.customization.cli import add_parsers, handle_command
from aster.service import RunCoordinator

TOKEN = "customization-test-token-at-least-thirty-two-characters"


@pytest.mark.asyncio
async def test_api_memory_and_skill_mutations_use_policy_and_durable_ledger(tmp_path):
    coordinator = RunCoordinator(tmp_path / "data", tools=[])
    app = create_app(coordinator, token=TOKEN)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost",
        headers={"Authorization": "Bearer " + TOKEN}
    ) as client:
        sid = coordinator.create_session(tmp_path, agent_id="writer")["id"]
        other = coordinator.create_session(tmp_path, agent_id="other")["id"]
        assert (await client.get("/customization/skills", headers={"Authorization": ""})).status_code == 401
        assert len((await client.get("/customization/skills")).json()["catalog"]) == 8
        arguments = {"entity": "User", "relation": "prefers", "value": "plain text",
                     "sources": [{"kind": "user_supplied", "label": "explicit operator note", "quote": "plain text"}]}
        endpoint = f"/customization/sessions/{sid}/memory_record"
        assert (await client.post(endpoint, json={"arguments": arguments})).status_code == 403
        assert coordinator.store.list_runs(sid) == []
        response = (await client.post(endpoint, json={"arguments": arguments, "approved": True})).json()
        assert not response["is_error"], response
        fact = response["data"]["memory"]
        assert fact["evidence"][0]["verified"] is False
        run = coordinator.store.list_runs(sid)[0]
        assert coordinator.store.tool_executions(run["id"])[0]["state"] == "checkpointed"
        own = (await client.post(f"/customization/sessions/{sid}/memory_recall", json={"arguments": {}})).json()
        assert own["data"]["memory"]["facts"][0]["id"] == fact["id"]
        isolated = (await client.get(f"/customization/sessions/{other}/memory")).json()
        assert isolated["no_data"]
        preview = (await client.post(f"/customization/sessions/{sid}/skill_package_preview",
            json={"arguments": {"source": "bundled:pdf-reader"}})).json()["data"]["package"]
        installed = (await client.post(f"/customization/sessions/{sid}/skill_package_install", json={
            "arguments": {"plan_id": preview["plan_id"], "expected_digest": preview["digest"]}, "approved": True})).json()
        assert not installed["is_error"], installed
        loaded = await coordinator.execute_session_tool(sid, "use_skill", {"name": "pdf-reader", "reference": "templates/reading-notes.md"})
        assert not loaded.is_error and "Visually inspected pages" in loaded.content
        (coordinator.data_dir / "config.yaml").write_text("permissions:\n  denied_tools: [memory_record]\n", encoding="utf-8")
        denied = (await client.post(endpoint, json={"arguments": arguments, "approved": True})).json()
        assert denied["is_error"]


@pytest.mark.asyncio
async def test_customization_cli_uses_same_coordinator_without_model_calls(tmp_path, capsys):
    coordinator = RunCoordinator(tmp_path / "data", tools=[])
    parser = argparse.ArgumentParser()
    add_parsers(parser.add_subparsers(dest="command", required=True))
    sid = coordinator.create_session(tmp_path)["id"]
    try:
        args = parser.parse_args(["skills", "preview", "bundled:doc-coauthoring"])
        assert await handle_command(args, coordinator) == 0
        preview = json.loads(capsys.readouterr().out)
        args = parser.parse_args(["skills", "install", preview["plan_id"], "--digest", preview["digest"], "--session", sid])
        assert await handle_command(args, coordinator) == 0
        assert json.loads(capsys.readouterr().out)["is_error"] is False
        assert coordinator.store.list_runs(sid)[0]["status"] == "completed"
    finally:
        await coordinator.close()
