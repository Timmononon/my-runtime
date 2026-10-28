import asyncio
import copy
import dataclasses
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest
import yaml

from aster.api import create_app
from aster.automation.service import AutomationService
from aster.domain import Message, ModelTurn, ToolCall, ToolContext, ToolResult, ToolSpec
from aster.engine import Engine, FakeModel, RunConfig
from aster.engine.dispatcher import Dispatcher
from aster.engine.observations import ObservationManager
from aster.engine.telemetry import performance
from aster.policy import PermissionPolicy
from aster.service import RunCoordinator
from aster.state import ReconciliationRequired, Store
from aster.tools.registry import Registry


def transcript(store, path):
    sid = store.create_session(path)["id"]
    run, _ = store.create_run(sid)
    for message in [Message("user", "source"), Message("assistant", tool_calls=[ToolCall("read", "file_read", {})]),
                    Message("tool", "evidence", tool_call_id="read"), Message("assistant", "done")]:
        store.append_message(sid, run["id"], message)
    store.finish_run(run["id"], "completed")
    return sid, run["id"]


def test_append_only_branch_boundaries_lineage_and_unknown_barrier(tmp_path):
    store = Store(tmp_path / "runtime.db")
    try:
        sid, rid = transcript(store, tmp_path)
        original = store.history_records(sid)
        with pytest.raises(ValueError, match="complete"):
            store.fork_session(sid, message_index=2)
        child = store.fork_session(sid, message_index=3)
        assert len(store.history(child["id"])) == 3
        assert store.history_records(sid) == original
        assert store.list_runs(child["id"]) == []
        assert store.session_ancestors(child["id"]) == [sid]
        assert store.tool_executions(rid) == []
        current, _ = store.create_run(sid)
        item = store.prepare_tool(sid, current["id"], ToolCall("unknown", "write", {}), risk="write", replay_safe=False)
        store.mark_dispatching(item["id"])
        with pytest.raises(ReconciliationRequired):
            store.create_run(child["id"])
        with pytest.raises(ReconciliationRequired):
            store.fork_session(sid, message_index=4)
    finally:
        store.close()


async def test_side_chat_is_hidden_independent_and_audited(tmp_path):
    coordinator = RunCoordinator(tmp_path, tools=[], model_factory=lambda _: FakeModel([ModelTurn("aside result")]))
    try:
        sid, _ = transcript(coordinator.store, tmp_path)
        before = coordinator.store.history_records(sid)
        child = coordinator.branch(sid, side_chat=True)
        run = await coordinator.submit(child["id"], "temporary aside")
        assert (await coordinator.wait(run["id"], timeout=3)).status == "completed"
        coordinator.close_side_chat(child["id"])
        assert coordinator.store.history_records(sid) == before
        assert child["id"] not in {item["id"] for item in coordinator.store.list_sessions()}
        assert child["id"] in {item["id"] for item in coordinator.store.list_sessions(include_side_chats=True)}
        assert coordinator.store.history(child["id"])[-1].content == "aside result"
        with pytest.raises(Exception, match="(?i)closed"):
            await coordinator.submit(child["id"], "must not reopen")
    finally:
        await coordinator.close()


def test_mailbox_retraction_and_consumption_are_distinct_durable_states(tmp_path):
    store = Store(tmp_path / "runtime.db")
    sid = store.create_session(tmp_path)["id"]
    run, _ = store.create_run(sid)
    removed = store.queue_message(sid, "withdrawn", request_id="stable")
    assert store.retract_message(sid, removed)
    assert not store.retract_message(sid, removed)
    assert store.queue_message(sid, "cannot revive", request_id="stable") == removed
    kept = store.queue_message(sid, "consume me")
    assert [item.content for item in store.drain_messages(sid, run["id"])] == ["consume me"]
    assert not store.retract_message(sid, kept)
    store.close()
    store = Store(tmp_path / "runtime.db")
    try:
        assert [item["state"] for item in store.list_mailbox(sid, include_terminal=True)] == ["retracted", "consumed"]
        assert not store.list_mailbox(sid)
    finally:
        store.close()


async def test_steering_interrupts_model_wait_but_run_continues(tmp_path):
    entered = asyncio.Event()

    async def wait(messages, tools):
        entered.set()
        await asyncio.Event().wait()

    model = FakeModel([wait, ModelTurn("changed direction")])
    coordinator = RunCoordinator(tmp_path, tools=[], model_factory=lambda _: model)
    try:
        sid = coordinator.create_session(tmp_path)["id"]
        run = await coordinator.submit(sid, "first direction")
        await entered.wait()
        coordinator.inject(sid, "new direction", steer=True)
        assert (await coordinator.wait(run["id"], timeout=3)).status == "completed"
        assert model.calls[1]["messages"][-1].content == "new direction"
        assert coordinator.store.run_budget(run["id"])["model_requests"] == 2
        report = performance(coordinator.store, run["id"])
        assert report["phase"] == "done" and "awaiting_model" in report["phase_seconds"]
        assert "first direction" not in json.dumps(report)
        terminals = [item for item in report["trace"] if item["type"] == "model.finished"]
        assert len(terminals) == 2 and len({item["request_id"] for item in terminals}) == 2
        assert [item["outcome"] for item in terminals] == ["interrupted", "completed"]
    finally:
        await coordinator.close()


@pytest.mark.parametrize("risk", ["write", "read"])
async def test_steering_respects_inflight_material_write(tmp_path, risk):
    started, release = asyncio.Event(), asyncio.Event()

    class Tool:
        spec = ToolSpec("work", "Controlled work", {"type": "object"}, risk=risk, replay_safe=risk == "read")
        calls = 0

        async def execute(self, args, context):
            self.calls += 1
            started.set()
            if risk == "write":
                await release.wait()
                (tmp_path / "effect.txt").write_text("one effect")
            else:
                await context.cancel.wait()
            return ToolResult("finished")

    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"permissions": {"auto_approve_tools": ["work"]}}))
    tool = Tool()
    model = FakeModel([ModelTurn(tool_calls=[ToolCall("one", "work", {}), ToolCall("two", "work", {})]), ModelTurn("steered done")])
    coordinator = RunCoordinator(tmp_path, tools=[tool], model_factory=lambda _: model)
    try:
        sid = coordinator.create_session(tmp_path)["id"]
        run = await coordinator.submit(sid, "do work")
        await started.wait()
        coordinator.inject(sid, "stop following old plan", steer=True)
        if risk == "write":
            await asyncio.sleep(0.03)
            assert coordinator.store.get_run(run["id"])["status"] == "running"
            release.set()
        assert (await coordinator.wait(run["id"], timeout=3)).status == "completed"
        assert tool.calls == 1
        assert model.calls[-1]["messages"][-1].content == "stop following old plan"
        if risk == "write":
            assert (tmp_path / "effect.txt").read_text() == "one effect"
    finally:
        release.set()
        await coordinator.close()


async def test_post_hook_http_risk_reclassified_before_approval(tmp_path):
    class HTTPTool:
        spec = ToolSpec("http", "HTTP", {"type": "object", "properties": {"method": {"type": "string"}}}, risk="write")
        calls = 0

        def classify(self, args):
            return dataclasses.replace(self.spec, risk="read" if args["method"] == "GET" else "write")

        async def execute(self, args, context):
            self.calls += 1
            return ToolResult("executed")

    class Hook:
        async def run(self, event, payload, context):
            payload["arguments"]["method"] = "POST"
            return SimpleNamespace(allow=True)

    store = Store(tmp_path / "runtime.db")
    try:
        sid = store.create_session(tmp_path)["id"]
        run, _ = store.create_run(sid)
        tool = HTTPTool()
        context = ToolContext(sid, run["id"], tmp_path, tmp_path, asyncio.Event(), services={"hooks": Hook()})
        result = (await Dispatcher(Registry([tool]), store, PermissionPolicy()).execute([ToolCall("h", "http", {"method": "GET"})], context))[0]
        assert result.is_error and tool.calls == 0
        assert not store.tool_executions(run["id"])
    finally:
        store.close()


async def test_file_dedup_requires_encoding_version_range_and_restores_fresh_content(tmp_path):
    store = Store(tmp_path / "runtime.db")
    try:
        sid = store.create_session(tmp_path)["id"]
        run, _ = store.create_run(sid)
        context = ToolContext(sid, run["id"], tmp_path, tmp_path, asyncio.Event())
        observer = ObservationManager(store, context, restore_chars=900)
        path = tmp_path / "source.txt"
        path.write_text("first\nMIDDLE-IDENTIFIER\nlast\n", encoding="utf-8")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        call = ToolCall("read", "file_read", {"path": str(path), "start_line": 2, "max_lines": 1})
        record = observer.record(call, ToolResult("2: MIDDLE-IDENTIFIER", data={"path": str(path), "sha256": digest}))
        observer.observe_visible([Message("tool", "2: MIDDLE-IDENTIFIER", tool_call_id="read", metadata={"file_observation": record})])
        assert (await observer.cached(call)).data["deduplicated"]
        assert await observer.cached(ToolCall("enc", "file_read", {**call.arguments, "encoding": "latin-1"})) is None
        path.write_text("first\nCHANGED-IDENTIFIER\nlast\n", encoding="utf-8")
        assert await observer.cached(call) is None
        restored = await observer.restore([], headroom_chars=900)
        assert len(restored[0].content) <= 900 and "CHANGED-IDENTIFIER" in restored[0].content
        assert "1: first" not in restored[0].content and "3: last" not in restored[0].content
        assert restored[0].metadata["file_restoration"][0]["sha256"] != digest
    finally:
        store.close()


def test_pure_screenshot_sequence_bounds_images_without_changing_archive(tmp_path):
    store = Store(tmp_path / "runtime.db")
    try:
        sid = store.create_session(tmp_path)["id"]
        run, _ = store.create_run(sid)
        context = ToolContext(sid, run["id"], tmp_path, tmp_path, asyncio.Event())
        observer = ObservationManager(store, context, browser_keep=2)
        messages = []
        for index in range(5):
            messages.extend([Message("assistant", tool_calls=[ToolCall(str(index), "browser_screenshot", {})]),
                             Message("tool", "screenshot", tool_call_id=str(index), metadata={"content_parts": [{"type": "input_image", "path": "registered.png"}]})])
        before = copy.deepcopy(messages)
        shaped = observer.bound_browser(messages)
        assert messages == before
        assert sum(bool(message.metadata.get("content_parts")) for message in shaped) == 2
    finally:
        store.close()


def test_schema_one_upgrade_and_legacy_automation_preserve_source_mode(tmp_path):
    store = Store(tmp_path / "runtime.db")
    sid = store.create_session(tmp_path)["id"]
    service = AutomationService(store, object())
    job = service.create_job("schedule", sid, "old", {"cron": "* * * * *"})
    with store.transaction() as db:
        config = job["config"]
        config.pop("session_mode")
        db.execute("UPDATE automation_jobs SET config_json=? WHERE id=?", (json.dumps(config), job["id"]))
        db.execute("PRAGMA user_version=1")
    store.close()
    store = Store(tmp_path / "runtime.db")
    try:
        assert store.get_session(sid)
        restarted = AutomationService(store, object())
        assert restarted.get_job(job["id"])["config"]["session_mode"] == "source"
        assert restarted.create_job("schedule", sid, "new", {"cron": "* * * * *"})["config"]["session_mode"] == "fresh"
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 2
    finally:
        store.close()


async def test_http_attachments_fork_mailbox_and_performance_share_core(tmp_path):
    models = []

    def factory(_):
        model = FakeModel([ModelTurn("read source")])
        models.append(model)
        return model

    coordinator = RunCoordinator(tmp_path, tools=[], model_factory=factory)
    app = create_app(coordinator, token="test-token", manage_lifecycle=False)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://localhost",
                                     headers={"Authorization": "Bearer test-token"}) as client:
            sid = (await client.post("/sessions", json={"cwd": str(tmp_path)})).json()["id"]
            attachment = (await client.post(f"/sessions/{sid}/attachments?filename=source.txt",
                                           content=b"SOURCE_MARKER_719", headers={"Content-Type": "text/plain"})).json()
            run = (await client.post(f"/sessions/{sid}/runs", json={"text": "use attachment", "attachments": [attachment["attachment_id"]]})).json()
            assert (await coordinator.wait(run["id"], timeout=3)).status == "completed"
            assert "SOURCE_MARKER_719" in models[0].calls[0]["messages"][0].content
            child = (await client.post(f"/sessions/{sid}/fork", json={})).json()
            inherited = (await client.get(f"/sessions/{child['id']}/attachments")).json()["attachments"]
            assert [item["attachment_id"] for item in inherited] == [attachment["attachment_id"]]
            later = coordinator.attachments.register_bytes(sid, "later.txt", b"later")
            with pytest.raises(PermissionError):
                coordinator.attachments.resolve(child["id"], later["attachment_id"])
            queued = (await client.post(f"/sessions/{sid}/messages", json={"text": "withdraw"})).json()
            assert (await client.delete(f"/sessions/{sid}/mailbox/{queued['message_id']}")).json()["retracted"]
            assert (await client.get(f"/sessions/{sid}/mailbox")).json()["messages"] == []
            report = (await client.get(f"/runs/{run['id']}/performance")).json()
            assert report["status"] == "completed" and "SOURCE_MARKER" not in json.dumps(report)
    finally:
        await coordinator.close()


async def test_real_file_tool_dedup_and_persistent_version_tracking(tmp_path):
    from aster.tools.files import build_file_tools
    path = tmp_path / "example.txt"
    path.write_text("one\nEXACT_SOURCE_82\nthree\n")
    store = Store(tmp_path / "runtime.db")
    try:
        sid = store.create_session(tmp_path)["id"]
        call = {"path": str(path), "start_line": 2, "max_lines": 1}
        model = FakeModel([ModelTurn(tool_calls=[ToolCall("a", "file_read", call)]),
                           ModelTurn(tool_calls=[ToolCall("b", "file_read", call)]), ModelTurn("done")])
        outcome = await Engine(model, Registry(build_file_tools()), store).run(sid, "Read the same exact range twice")
        assert outcome.status == "completed"
        executions = store.tool_executions(outcome.run_id)
        assert executions[0]["result"]["content"].splitlines()[0] == "2: EXACT_SOURCE_82"
        assert "continue file_read with start_line=3" in executions[0]["result"]["content"]
        assert executions[0]["result"]["data"]["truncated"] is True
        assert executions[0]["result"]["data"]["range_complete"] is True
        assert executions[1]["result"]["data"]["deduplicated"]
        assert executions[1]["result"]["data"]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    finally:
        store.close()
    store = Store(tmp_path / "runtime.db")
    try:
        context = ToolContext(sid, outcome.run_id, tmp_path, tmp_path, asyncio.Event())
        restored = await ObservationManager(store, context).restore([], headroom_chars=5000)
        assert "EXACT_SOURCE_82" in restored[0].content and "1: one" not in restored[0].content
    finally:
        store.close()


@pytest.mark.parametrize("mode", ["fresh", "sticky"])
async def test_automation_execution_sessions_are_isolated_and_routes_survive_restart(tmp_path, mode):
    store = Store(tmp_path / "runtime.db")
    sid = store.create_session(tmp_path)["id"]
    store.append_message(sid, None, Message("user", "interactive secret must not enter automation"))

    class Coordinator:
        async def submit(self, session_id, text, request_id, unattended):
            run, created = store.create_run(session_id, request_id=request_id)
            if created:
                store.append_message(session_id, run["id"], Message("user", text))
                store.finish_run(run["id"], "completed")
            return run

    try:
        service = AutomationService(store, Coordinator(), clock=lambda: 1000)
        job = service.create_job("schedule", sid, "scheduled", {"cron": "* * * * *", "session_mode": mode})
        for index in range(2):
            service._claim(job, f"tick:{index}", 1000 + index * 60, "scheduled", 1000 + index * 60)
            await service._deliver(1000 + index * 60)
            service._results(1001 + index * 60)
            service = AutomationService(store, Coordinator(), clock=lambda: 1100)
        executions = [store.get_run(receipt["run_id"]) for receipt in service.receipts()]
        assert len(executions) == 2
        child_sessions = {run["session_id"] for run in executions}
        assert len(child_sessions) == (2 if mode == "fresh" else 1)
        assert sid not in child_sessions
        assert len(store.history(sid)) == 1
        assert all("interactive secret" not in message.content for child in child_sessions for message in store.history(child))
    finally:
        store.close()


async def test_branch_rejects_reused_historical_call_id_before_appending_invalid_history(tmp_path):
    store = Store(tmp_path / "runtime.db")
    try:
        sid, _ = transcript(store, tmp_path)
        child = store.fork_session(sid)
        model = FakeModel([ModelTurn(tool_calls=[ToolCall("read", "file_read", {"path": "irrelevant"})])])
        outcome = await Engine(model, Registry(), store).run(child["id"], "continue")
        assert outcome.status == "failed" and "reused" in outcome.error
        assert sum(call.id == "read" for message in store.history(child["id"]) for call in message.tool_calls) == 1
        assert not store.tool_executions(outcome.run_id)
    finally:
        store.close()


async def test_closed_side_chat_cancels_capacity_queued_run_without_model(tmp_path):
    model = FakeModel([ModelTurn("must not start")])
    coordinator = RunCoordinator(tmp_path, tools=[], model_factory=lambda _: model, max_parallel_runs=1)
    await coordinator._capacity.acquire()
    try:
        sid, _ = transcript(coordinator.store, tmp_path)
        child = coordinator.branch(sid, side_chat=True)
        run = await coordinator.submit(child["id"], "waiting for capacity")
        await asyncio.sleep(0)
        coordinator.close_side_chat(child["id"])
        assert coordinator.store.get_run(run["id"])["status"] == "cancelled"
        with pytest.raises(Exception, match="(?i)closed"):
            coordinator.inject(child["id"], "must not enqueue")
        coordinator._capacity.release()
        await coordinator._workers[child["id"]]
        assert not model.calls
    finally:
        if coordinator._capacity.locked():
            coordinator._capacity.release()
        await coordinator.close()


async def test_long_history_after_reopen_compacts_then_restores_exact_fresh_range(tmp_path):
    from aster.tools.files import build_file_tools
    path = tmp_path / "source.txt"
    path.write_text("header\nPERSISTED_OLD_MARKER\nfooter\n")
    store = Store(tmp_path / "runtime.db")
    sid = store.create_session(tmp_path)["id"]
    initial = FakeModel([ModelTurn(tool_calls=[ToolCall("read-source", "file_read", {"path": str(path), "start_line": 2, "max_lines": 1})]), ModelTurn("source captured")])
    first = await Engine(initial, Registry(build_file_tools()), store).run(sid, "Keep working with this source range")
    assert first.status == "completed"
    for index in range(16):
        store.append_message(sid, first.run_id, Message("assistant", tool_calls=[ToolCall(f"old-{index}", "file_read", {"path": "other.txt"})]))
        store.append_message(sid, first.run_id, Message("tool", "old evidence " + "z" * 1000, tool_call_id=f"old-{index}"))
    store.append_message(sid, first.run_id, Message("assistant", "history collected"))
    raw_before = store.history_records(sid)
    store.close()
    path.write_text("header\nFRESH_EXACT_MARKER_491\nfooter\n")
    store = Store(tmp_path / "runtime.db")
    try:
        model = FakeModel([ModelTurn("continued with fresh range")])
        config = RunConfig(context_tokens=8000, reserve_tokens=1000, semantic_compaction=False,
                           retained_context_rounds=2, file_restore_chars=1500)
        resumed = await Engine(model, Registry(build_file_tools()), store, config=config).run(sid, "continue accurately")
        assert resumed.status == "completed", resumed.error
        refreshes = [message for message in model.calls[0]["messages"] if message.metadata.get("file_restoration")]
        assert refreshes and "2: FRESH_EXACT_MARKER_491" in refreshes[0].content
        assert "1: header" not in refreshes[0].content and len(refreshes[0].content) <= 1500
        assert hashlib.sha256(path.read_bytes()).hexdigest() in refreshes[0].content
        assert store.history_records(sid)[:len(raw_before)] == raw_before
        assert any(event["type"] == "context.files_restored" for event in store.events(run_id=resumed.run_id))
    finally:
        store.close()


def test_http_credentials_have_exact_trusted_origins_and_lower_layers_cannot_widen(tmp_path):
    from aster.config import load_config
    settings = {"http_allowed_hosts": ["example.test"], "http_credential_origins": {
        "env:SERVICE_TOKEN": ["https://Example.Test", "http://127.0.0.1:8991"]}}
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(settings))
    snapshot = load_config(tmp_path)
    assert snapshot.data["http_credential_origins"]["env:SERVICE_TOKEN"] == ["http://127.0.0.1:8991", "https://example.test:443"]
    restricted = load_config(tmp_path, overrides={"http_allowed_hosts": ["evil.test"], "http_credential_origins": {
        "env:SERVICE_TOKEN": ["https://evil.test", "https://example.test:443"], "env:NEW_TOKEN": ["https://evil.test"]}})
    assert restricted.data["http_allowed_hosts"] == []
    assert restricted.data["http_credential_origins"] == {"env:SERVICE_TOKEN": ["https://example.test:443"]}
    for invalid in [{"raw-key": ["https://example.test"]}, {"env:TOKEN": ["https://*.test"]}, {"env:TOKEN": ["https://example.test/path"]}]:
        with pytest.raises(ValueError):
            load_config(tmp_path, overrides={"http_credential_origins": invalid})
    with pytest.raises(ValueError, match="credential references"):
        load_config(tmp_path, overrides={"credential_value": "still-rejected-secret"})


@pytest.mark.parametrize("changed_mode", ["sticky", "source"])
async def test_automation_crash_receipt_keeps_pinned_session_after_mode_change(tmp_path, changed_mode):
    store = Store(tmp_path / "runtime.db")
    sid = store.create_session(tmp_path)["id"]
    calls = []

    class Coordinator:
        async def submit(self, session_id, text, request_id, unattended):
            run, created = store.create_run(session_id, request_id=request_id)
            if created:
                calls.append(run["id"])
                store.finish_run(run["id"], "completed")
            return run

    try:
        coordinator = Coordinator()
        service = AutomationService(store, coordinator, clock=lambda: 1000)
        job = service.create_job("schedule", sid, "one effect", {"cron": "* * * * *", "session_mode": "fresh"})
        service._claim(job, "first", 1000, "one effect", 1000)
        receipt = service.receipts()[0]
        pinned = store.automation_execution_session(sid, job["id"], receipt["id"], "fresh")
        accepted = await coordinator.submit(pinned["session_id"], receipt["prompt"], receipt["id"], True)
        # Simulate exit before saving run_id on the still-prepared receipt.
        service.update_job(job["id"], config={"session_mode": changed_mode})
        service = AutomationService(store, coordinator, clock=lambda: 1001)
        await service._deliver(1001)
        assert service.receipts()[0]["run_id"] == accepted["id"]
        assert len(calls) == 1
        assert store.automation_execution_session(sid, job["id"], receipt["id"], changed_mode)["session_mode"] == "fresh"
        future = store.automation_execution_session(sid, job["id"], "new-trigger", changed_mode)
        assert future["session_mode"] == changed_mode and future["session_id"] != pinned["session_id"]
    finally:
        store.close()


async def test_steering_during_semantic_summary_continues_with_new_user_message(tmp_path):
    store = Store(tmp_path / "runtime.db")
    sid, _ = transcript(store, tmp_path)
    for index in range(16):
        store.append_message(sid, None, Message("assistant", tool_calls=[ToolCall(f"history-{index}", "read", {})]))
        store.append_message(sid, None, Message("tool", "context " + "x" * 1100, tool_call_id=f"history-{index}"))
    entered = asyncio.Event()
    summary_cancelled = asyncio.Event()
    resumed = asyncio.Event()

    async def wait_summary(messages, tools):
        assert tools == []
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            summary_cancelled.set()

    async def answer_new_direction(messages, tools):
        assert summary_cancelled.is_set()
        assert any(message.content == "new objective during summary" for message in messages)
        resumed.set()
        return ModelTurn("new direction accepted")

    model = FakeModel([wait_summary, answer_new_direction])
    engine = Engine(model, Registry(), store, config=RunConfig(context_tokens=7000, reserve_tokens=1000,
                                                              retained_context_rounds=2))
    task = asyncio.create_task(engine.run(sid, "continue"))
    try:
        # Synchronize on control transitions, rather than imposing a three-second
        # budget on cancellation plus SQLite checkpoints and durable cleanup.
        async with asyncio.timeout(15):
            await entered.wait()
            rid = store.list_runs(sid)[0]["id"]
            store.queue_message(sid, "new objective during summary")
            assert engine.steer(rid)
            await summary_cancelled.wait()
            await resumed.wait()
            result = await task
        assert result.status == "completed", result.error
        assert any(message.content == "new objective during summary" for message in model.calls[-1]["messages"])
        assert store.run_budget(rid)["model_requests"] == 2
        assert store.get_run(rid)["status"] == "completed"
        events = store.events(run_id=rid)
        assert [event["payload"]["purpose"] for event in events if event["type"] == "model.request"] == ["compaction", "main"]
        assert [(event["payload"]["outcome"], event["payload"]["category"])
                for event in events if event["type"] == "model.finished"] == [("interrupted", "steering"), ("completed", None)]
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        store.close()
