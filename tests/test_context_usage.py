"""The context meter counts active prompt content and remains a read-only view."""
import asyncio
import copy
import json

import httpx
import pytest
from test_compaction_integration import coordinator_at

from aster.api import create_app
from aster.domain import Message, ModelTurn, Usage
from aster.engine import FakeModel
from aster.engine.context import estimate_tokens
from aster.engine.context_usage import context_usage_basis
from aster.engine.loop import RunConfig
from aster.service.context_usage import context_usage
from aster.tools.files import build_file_tools


async def complete(coordinator, sid, text="Continue the offline task"):
    run = await coordinator.submit(sid, text)
    async with asyncio.timeout(5):
        while coordinator.store.get_run(run["id"])["status"] in {"queued", "running"}:
            await asyncio.sleep(.01)
    assert coordinator.store.get_run(run["id"])["status"] == "completed"
    return run


async def test_context_endpoint_is_authenticated_content_free_and_does_not_execute_resources(tmp_path):
    model = FakeModel([ModelTurn("done", usage=Usage(input_tokens=5000, output_tokens=2))])
    coordinator = coordinator_at(tmp_path, lambda _: model, tools=build_file_tools())
    sid = coordinator.create_session(tmp_path)["id"]
    token = "offline-context-meter-token"
    app = create_app(coordinator, token=token)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765") as api:
            assert (await api.get(f"/sessions/{sid}/context-usage")).status_code == 401
            headers = {"Authorization": "Bearer " + token}
            before = await api.get(f"/sessions/{sid}/context-usage", headers=headers)
            assert before.status_code == 200 and before.json()["status"] == "unavailable"
            assert before.json()["used_tokens"] is None and before.json()["percent"] is None
            await complete(coordinator, sid, "PRIVATE_USER_TEXT_DO_NOT_RETURN_IN_COUNTS")
            saved = copy.deepcopy(coordinator.store.get_checkpoint(sid))
            archive = copy.deepcopy(coordinator.store.history_records(sid))
            writes = coordinator.store.connection.total_changes
            response = await api.get(f"/sessions/{sid}/context-usage", headers=headers)
            assert response.status_code == 200
            value = response.json()
            assert value["status"] == "estimated" and value["context_window"] == 96000
            assert value["source"] == "active_context" and value["session_id"] == sid
            assert value["used_tokens"] == sum(value["breakdown"].values())
            basis = context_usage_basis(model.calls[0]["system"], model.calls[0]["tools"], RunConfig(), session_id=sid)
            assert value["breakdown"]["tools_tokens"] == basis["tools_tokens"] > 0
            assert value["breakdown"]["system_tokens"] == basis["system_tokens"]
            assert value["breakdown"]["message_tokens"] == estimate_tokens(coordinator.store.active_history(sid))
            assert "PRIVATE_USER_TEXT" not in response.text and token not in response.text
            assert not any(key in response.text for key in ("parameters", "instructions", "credential_ref", "messages_json"))
            assert coordinator.store.connection.total_changes == writes
            assert coordinator.store.get_checkpoint(sid) == saved and coordinator.store.history_records(sid) == archive
            assert len(model.calls) == 1
            assert "context_usage_v1" in (await api.get("/capabilities", headers=headers)).json()["capabilities"]
            assert (await api.get("/sessions/missing/context-usage", headers=headers)).status_code == 404
    finally:
        await coordinator.close()


async def test_context_counts_follow_compacted_checkpoint_and_tail_without_recounting_archive(tmp_path):
    model = FakeModel([ModelTurn("done")])
    coordinator = coordinator_at(tmp_path, lambda _: model, runtime={"retained_context_rounds": 2, "semantic_compaction": False})
    sid = coordinator.create_session(tmp_path)["id"]
    try:
        await complete(coordinator, sid)
        for number in range(8):
            coordinator.store.append_message(sid, None, Message("user", f"Task {number}: preserve local evidence"))
            coordinator.store.append_message(sid, None, Message("assistant", "Recorded evidence detail " * 400))
        archive = copy.deepcopy(coordinator.store.history_records(sid))
        before = context_usage(coordinator.store, sid)
        assert before["breakdown"]["message_tokens"] == estimate_tokens(coordinator.store.active_history(sid))
        result = await coordinator.manual_compact(sid)
        assert result["changed"]
        after = context_usage(coordinator.store, sid)
        assert after["status"] == "estimated"
        assert after["used_tokens"] < before["used_tokens"]
        assert after["breakdown"]["message_tokens"] < before["breakdown"]["message_tokens"]
        assert after["breakdown"]["message_tokens"] == estimate_tokens(coordinator.store.active_history(sid))
        assert coordinator.store.history_records(sid) == archive and len(model.calls) == 1
    finally:
        await coordinator.close()


@pytest.mark.parametrize("invalid", [{}, {"context_window": 0}, {"system_tokens": -1}, {"tools_tokens": None},
                                    {"calibration_tokens": True}, {"session_id": "another-session"}])
async def test_legacy_invalid_and_inherited_basis_remains_unknown(tmp_path, invalid):
    coordinator = coordinator_at(tmp_path, lambda _: FakeModel([]))
    sid = coordinator.create_session(tmp_path)["id"]
    try:
        basis = context_usage_basis("protected system", [], RunConfig(), session_id=sid)
        basis = {} if invalid == {} else {**basis, **invalid}
        coordinator.store.checkpoint(sid, "no-run", [], metadata={"context_usage": basis})
        value = context_usage(coordinator.store, sid)
        assert value["status"] == "unavailable" and value["used_tokens"] is None and value["percent"] is None
        assert "protected system" not in json.dumps(value)
    finally:
        await coordinator.close()
