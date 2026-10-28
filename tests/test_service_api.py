import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest

from aster.api import create_app
from aster.domain import ModelTurn, ToolCall, ToolResult, ToolSpec
from aster.engine import FakeModel
from aster.service import RunCoordinator

TOKEN = "test-local-token-that-is-at-least-thirty-two-characters"


class EmptyCredentials:
    def get(self, *args, **kwargs):
        return None

    def resolve(self, reference):
        raise ValueError("No test credentials")


class TestTool:
    __test__ = False

    def __init__(self, risk="read"):
        self.spec = ToolSpec("test_tool", "Controlled local test", {"type": "object", "properties": {}, "additionalProperties": False}, risk=risk)
        self.executions = 0

    async def execute(self, arguments, context):
        self.executions += 1
        return ToolResult("tool output")


@asynccontextmanager
async def api(tmp_path, *, model=None, tools=None, credentials=None):
    coordinator = RunCoordinator(tmp_path, model_factory=(lambda settings: model) if model else None,
                                  tools=tools or [], credentials=credentials or EmptyCredentials())
    app = create_app(coordinator, token=TOKEN)
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost",
        headers={"Authorization": "Bearer " + TOKEN}
    ) as client:
        yield client, coordinator


async def test_health_without_model_key_and_no_fake_fallback(tmp_path):
    async with api(tmp_path) as (client, coordinator):
        response = await client.get("/health", headers={"Authorization": ""})
        assert response.status_code == 200
        assert response.json()["provider_configured"] is False
        session = (await client.post("/sessions", json={"cwd": str(tmp_path)})).json()
        failed = await client.post(f"/sessions/{session['id']}/runs", json={"text": "hello"})
        assert failed.status_code == 503
        assert failed.json()["error"] == "configuration"
        assert coordinator.store.list_runs() == []


async def test_token_origin_host_and_extra_fields_enforced(tmp_path):
    async with api(tmp_path) as (client, _):
        assert (await client.get("/sessions", headers={"Authorization": "Bearer wrong"})).status_code == 401
        assert (await client.get("/sessions", headers={"Origin": "https://attacker.invalid"})).status_code == 403
        assert (await client.get("/health", headers={"Host": "attacker.invalid"})).status_code == 403
        assert (await client.post("/sessions", json={"api_key": "not-accepted"})).status_code == 422


async def test_api_run_sse_replay_and_idempotent_request(tmp_path):
    model = FakeModel([ModelTurn("Hello local API")])
    async with api(tmp_path, model=model) as (client, coordinator):
        sid = (await client.post("/sessions", json={"cwd": str(tmp_path)})).json()["id"]
        request = {"text": "hello", "request_id": "same-request"}
        first = await client.post(f"/sessions/{sid}/runs", json=request)
        second = await client.post(f"/sessions/{sid}/runs", json=request)
        assert first.status_code == 202
        rid = first.json()["id"]
        assert second.json()["id"] == rid
        outcome = await coordinator.wait(rid, timeout=3)
        assert outcome.status == "completed"
        assert len(model.calls) == 1
        response = await client.get(f"/runs/{rid}/events?follow=false")
        records = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        assert records and any(record["type"] == "model.text.delta" for record in records)
        cursor = records[len(records) // 2]["event_id"]
        replay = await client.get(f"/runs/{rid}/events?follow=false", headers={"Last-Event-ID": str(cursor)})
        later = [json.loads(line[6:]) for line in replay.text.splitlines() if line.startswith("data: ")]
        assert all(record["event_id"] > cursor for record in later)


async def test_session_queue_order_and_queued_text_not_in_active_history(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    seen = []

    async def first(messages, tools):
        seen.append(messages[-1].content)
        entered.set()
        await release.wait()
        return ModelTurn("first done")

    async def second(messages, tools):
        seen.append(messages[-1].content)
        return ModelTurn("second done")

    models = [FakeModel([first]), FakeModel([second])]
    coordinator = RunCoordinator(tmp_path, tools=[], model_factory=lambda settings: models.pop(0))
    await coordinator.start()
    try:
        sid = coordinator.create_session(tmp_path)["id"]
        a = await coordinator.submit(sid, "first")
        await entered.wait()
        b = await coordinator.submit(sid, "second")
        assert coordinator.store.get_run(b["id"])["status"] == "queued"
        assert [message.content for message in coordinator.store.history(sid)] == ["first"]
        release.set()
        assert (await coordinator.wait(a["id"], timeout=3)).status == "completed"
        assert (await coordinator.wait(b["id"], timeout=3)).status == "completed"
        assert seen == ["first", "second"]
    finally:
        release.set()
        await coordinator.close()


async def test_queued_request_survives_restart_without_duplicate_run(tmp_path):
    first = RunCoordinator(tmp_path, tools=[], model_factory=lambda _: FakeModel([ModelTurn("should not start")]))
    sid = first.create_session(tmp_path)["id"]
    queued = await first.submit(sid, "durable request", request_id="receipt-1")
    # close sets its stop flag before the worker gets an event-loop turn.
    await first.close()
    model = FakeModel([ModelTurn("recovered queue")])
    second = RunCoordinator(tmp_path, tools=[], model_factory=lambda _: model)
    try:
        await second.start()
        result = await second.wait(queued["id"], timeout=3)
        duplicate = await second.submit(sid, "durable request", request_id="receipt-1")
        assert result.status == "completed"
        assert duplicate["id"] == queued["id"]
        assert len(model.calls) == 1
    finally:
        await second.close()


async def test_api_approval_and_late_repeat_cannot_execute_again(tmp_path):
    tool = TestTool(risk="write")
    model = FakeModel([ModelTurn(tool_calls=[ToolCall("write-1", "test_tool", {})]), ModelTurn("done")])
    async with api(tmp_path, model=model, tools=[tool]) as (client, coordinator):
        sid = (await client.post("/sessions", json={"cwd": str(tmp_path)})).json()["id"]
        rid = (await client.post(f"/sessions/{sid}/runs", json={"text": "write"})).json()["id"]
        async with asyncio.timeout(3):
            while not coordinator.store.pending_requests(rid):
                await asyncio.sleep(0.01)
        request_id = coordinator.store.pending_requests(rid)[0]["id"]
        approved = await client.post(f"/approvals/{request_id}/decision", json={"approved": True})
        assert approved.json()["accepted"]
        assert (await coordinator.wait(rid, timeout=3)).status == "completed"
        assert tool.executions == 1
        assert not (await client.post(f"/approvals/{request_id}/decision", json={"approved": True})).json()["accepted"]


async def test_api_cancel_and_midturn_message(tmp_path):
    entered = asyncio.Event()

    async def slow(messages, tools):
        entered.set()
        await asyncio.Event().wait()

    async with api(tmp_path, model=FakeModel([slow])) as (client, coordinator):
        sid = (await client.post("/sessions", json={"cwd": str(tmp_path)})).json()["id"]
        rid = (await client.post(f"/sessions/{sid}/runs", json={"text": "wait"})).json()["id"]
        await entered.wait()
        injected = await client.post(f"/sessions/{sid}/messages", json={"text": "additional instruction", "request_id": "followup"})
        duplicate = await client.post(f"/sessions/{sid}/messages", json={"text": "additional instruction", "request_id": "followup"})
        assert injected.json()["message_id"] == duplicate.json()["message_id"]
        assert (await client.post(f"/runs/{rid}/cancel")).json()["accepted"]
        assert (await coordinator.wait(rid, timeout=3)).status == "cancelled"


async def test_agent_configuration_and_skill_scopes_are_per_run(tmp_path):
    import yaml
    (tmp_path / "agents" / "a").mkdir(parents=True)
    (tmp_path / "agents" / "b").mkdir(parents=True)
    (tmp_path / "agents" / "a" / "config.yaml").write_text(yaml.safe_dump({"model": "agent-a"}))
    (tmp_path / "agents" / "b" / "config.yaml").write_text(yaml.safe_dump({"model": "agent-b"}))
    snapshots = []

    def models(settings):
        snapshots.append(settings)
        return FakeModel([ModelTurn("done")])

    coordinator = RunCoordinator(tmp_path, tools=[], model_factory=models)
    try:
        a = coordinator.create_session(tmp_path, agent_id="a")
        b = coordinator.create_session(tmp_path, agent_id="b")
        ra = await coordinator.submit(a["id"], "A")
        rb = await coordinator.submit(b["id"], "B")
        await coordinator.wait(ra["id"], timeout=3)
        await coordinator.wait(rb["id"], timeout=3)
        assert {settings["model"] for settings in snapshots} == {"agent-a", "agent-b"}
    finally:
        await coordinator.close()


async def test_registered_artifact_only_and_path_boundary(tmp_path):
    from aster.domain import Message
    async with api(tmp_path) as (client, coordinator):
        sid = coordinator.create_session(tmp_path)["id"]
        run, _ = coordinator.store.create_run(sid)
        directory = tmp_path / "artifacts" / sid / run["id"]
        directory.mkdir(parents=True)
        output = directory / "result.txt"
        output.write_text("actual artifact")
        coordinator.store.append_message(sid, run["id"], Message("tool", "saved", tool_call_id="call-a",
            metadata={"artifacts": [{"artifact_id": "allowed", "path": str(output), "mime_type": "text/plain"}]}))
        coordinator.store.append_message(sid, run["id"], Message("tool", "invalid", tool_call_id="call-b",
            metadata={"artifacts": [{"artifact_id": "escape", "path": str(tmp_path / "runtime.db") }]}))
        response = await client.get("/artifacts/allowed")
        assert response.status_code == 200 and response.text == "actual artifact"
        assert "attachment" in response.headers["content-disposition"]
        assert (await client.get("/artifacts/unknown")).status_code == 404
        # Permission failures must be an explicit HTTP denial, never file contents.
        response = await client.get("/artifacts/escape")
        assert response.status_code in {403, 404}


async def test_artifact_slots_preserve_user_tool_event_and_fork_checkpoint_without_data_pollution(tmp_path):
    from aster.domain import Message
    from aster.maintenance import registered_artifacts
    from aster.memory.facts import StructuredMemory
    from aster.memory.service import MemoryService

    async with api(tmp_path) as (client, coordinator):
        store = coordinator.store
        sid = coordinator.create_session(tmp_path)["id"]
        run, _ = store.create_run(sid)
        directory = tmp_path / "artifacts" / "slot-fixture"
        directory.mkdir(parents=True)

        def record(name):
            path = directory / (name + ".txt")
            path.write_text(name, encoding="utf-8")
            return {"artifact_id": name, "path": str(path), "mime_type": "text/plain"}

        user, tool, event, checkpoint, spill = [record(name) for name in ("user", "tool", "event", "checkpoint", "spill")]
        forged = {"artifact_id": "user", "path": str(tmp_path / "runtime.db")}
        unregistered = record("unregistered")
        store.append_message(sid, run["id"], Message("user", "attachment", metadata={"artifacts": [user]}))
        store.append_message(sid, run["id"], Message("tool", "ordinary nested data", tool_call_id="data",
            metadata={"data": {"artifacts": [forged, unregistered]}, "output_artifact": spill}))
        intent = store.prepare_tool(sid, run["id"], ToolCall("ledger", "read", {}), risk="read", replay_safe=True)
        store.mark_dispatching(intent["id"])
        store.commit_tool(intent["id"], ToolResult("saved", artifacts=[tool], data={"artifacts": [forged, unregistered]}))
        store.emit_event(sid, run["id"], "artifact.registered", {"artifacts": [event]})
        store.emit_event(sid, run["id"], "ordinary.event", {"artifacts": [forged, unregistered]})
        store.append_message(sid, run["id"], Message("assistant", "done"))
        store.checkpoint(sid, run["id"], [Message("assistant", "checkpoint-only evidence", metadata={"artifacts": [checkpoint]})])
        store.finish_run(run["id"], "completed")
        facts = StructuredMemory(store, MemoryService(tmp_path / "memory", agent_id="default"))
        facts.record(entity="ordinary data", relation="example", value={"artifacts": [forged, unregistered]},
                     sources=[{"kind": "user_supplied", "label": "fixture", "quote": "ordinary object"}])
        child = store.fork_session(sid)
        # The original checkpoint is replaced; the copied branch checkpoint is now
        # the sole authoritative slot preserving this artifact registration.
        store.checkpoint(sid, run["id"], [Message("assistant", "new checkpoint")])
        assert store.get_checkpoint(child["id"])["messages"][0]["metadata"]["artifacts"] == [checkpoint]
        registered = registered_artifacts(tmp_path / "runtime.db")
        assert set(registered) == {"user", "tool", "event", "checkpoint", "spill"}
        assert registered["user"]["path"] == user["path"]
        for name in registered:
            response = await client.get("/artifacts/" + name)
            assert response.status_code == 200 and response.text == name
        assert (await client.get("/artifacts/unregistered")).status_code == 404


async def test_api_question_roundtrip_and_unattended_refuses_wait(tmp_path):
    models = [FakeModel([ModelTurn(tool_calls=[ToolCall("q1", "ask_user_question", {"question": "Which output format?"})]), ModelTurn("done")]),
              FakeModel([ModelTurn(tool_calls=[ToolCall("q2", "ask_user_question", {"question": "No user is here"})]), ModelTurn("cannot ask")])]
    coordinator = RunCoordinator(tmp_path, tools=[], model_factory=lambda _: models.pop(0))
    app = create_app(coordinator, token=TOKEN)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost",
            headers={"Authorization": "Bearer " + TOKEN}) as client:
        sid = coordinator.create_session(tmp_path)["id"]
        run = await coordinator.submit(sid, "ask")
        async with asyncio.timeout(3):
            while not coordinator.store.pending_requests(run["id"]):
                await asyncio.sleep(0.01)
        pid = coordinator.store.pending_requests(run["id"])[0]["id"]
        assert (await client.post(f"/questions/{pid}/answer", json={"answer": "CSV"})).json()["accepted"]
        assert (await coordinator.wait(run["id"], timeout=3)).status == "completed"
        other = await coordinator.submit(sid, "background", unattended=True)
        assert (await coordinator.wait(other["id"], timeout=3)).status == "completed"
        assert not coordinator.store.pending_requests(other["id"])
        assert any("No question channel" in message.content for message in coordinator.store.history(sid))


async def test_explicit_global_auto_approve_write_and_project_cannot_escalate(tmp_path):
    import yaml
    tool = TestTool(risk="write")
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"permissions": {"auto_approve_tools": ["test_tool"]}}))
    (tmp_path / "projects" / "restricted").mkdir(parents=True)
    (tmp_path / "projects" / "restricted" / "config.yaml").write_text(yaml.safe_dump({"permissions": {"auto_approve_tools": []}}))
    (tmp_path / "projects" / "escalation").mkdir(parents=True)
    (tmp_path / "projects" / "escalation" / "config.yaml").write_text(yaml.safe_dump({"permissions": {"auto_approve_tools": ["test_tool", "not_global"]}}))

    def factory(_):
        return FakeModel([ModelTurn(tool_calls=[ToolCall("w", "test_tool", {})]), ModelTurn("done")])

    coordinator = RunCoordinator(tmp_path, tools=[tool], model_factory=factory)
    try:
        a = coordinator.create_session(tmp_path)
        b = coordinator.create_session(tmp_path, project_id="restricted")
        first = await coordinator.submit(a["id"], "write", unattended=True)
        second = await coordinator.submit(b["id"], "deny", unattended=True)
        await coordinator.wait(first["id"], timeout=3)
        await coordinator.wait(second["id"], timeout=3)
        assert tool.executions == 1
        assert not coordinator.store.pending_requests()
        c = coordinator.create_session(tmp_path, project_id="escalation")
        snapshot_run = await coordinator.submit(c["id"], "allowed global only", unattended=True)
        await coordinator.wait(snapshot_run["id"], timeout=3)
        saved = coordinator.store.run_request(snapshot_run["id"])["snapshot"]["permissions"]["auto_approve_tools"]
        assert saved == ["test_tool"]
    finally:
        await coordinator.close()


async def test_startup_recovery_is_opt_in_unattended_and_attempt_is_durable(tmp_path):
    import yaml
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"recovery": {"enabled": True, "max_attempts": 2}}))
    first = RunCoordinator(tmp_path, tools=[], model_factory=lambda _: FakeModel([]))
    sid = first.create_session(tmp_path)["id"]
    queued = await first.submit(sid, "recover me")
    first.store.set_run_status(queued["id"], "running")  # Simulate the persisted pre-LLM boundary.
    await first.close()
    tool = TestTool(risk="write")
    model = FakeModel([ModelTurn(tool_calls=[ToolCall("w", "test_tool", {})]), ModelTurn("done without unsafe write")])
    second = RunCoordinator(tmp_path, tools=[tool], model_factory=lambda _: model)
    try:
        await second.start()
        result = await second.wait(queued["id"], timeout=3)
        assert result.status == "completed"
        assert not tool.executions
        assert second.store.get_run(queued["id"])["resume_attempts"] == 1
        assert not second.store.pending_requests()
    finally:
        await second.close()


async def test_startup_recovery_skips_uncertain_effect_and_default_disabled(tmp_path):
    from aster.domain import ToolCall
    from aster.engine import RunConfig
    first = RunCoordinator(tmp_path, tools=[], model_factory=lambda _: FakeModel([]))
    sid = first.create_session(tmp_path)["id"]
    run, _ = first.store.create_run(sid, config=RunConfig().__dict__)
    execution = first.store.prepare_tool(sid, run["id"], ToolCall("unknown", "submit", {}), risk="external", replay_safe=False)
    first.store.mark_dispatching(execution["id"])
    await first.close()
    model = FakeModel([ModelTurn("must not run")])
    second = RunCoordinator(tmp_path, tools=[], model_factory=lambda _: model)
    try:
        await second.start()
        await asyncio.sleep(0.03)
        assert second._auto_task is None
        assert not model.calls
        assert second.store.get_run(run["id"])["status"] == "running"
    finally:
        await second.close()


async def test_metadata_preparation_clamps_context_without_network(tmp_path, monkeypatch):
    from aster.providers.deepseek import ModelCapabilities
    calls = []

    class Credentials(EmptyCredentials):
        def get(self, *args, **kwargs):
            return "mock-key-never-sent"

    class Provider:
        def __init__(self, key, **kwargs):
            calls.append(kwargs)
            self.model = kwargs["model"]
            self.max_output_tokens = kwargs["max_output_tokens"]

        async def refresh_capabilities(self, *, cancel=None):
            calls.append("capabilities")
            return {self.model: ModelCapabilities(self.model, 40000, 6000, ("text", "image"), ("high",), "test", {})}

        async def generate(self, messages, tools, **kwargs):
            calls.append("generate")
            return ModelTurn("mock metadata verified")

        async def aclose(self):
            calls.append("closed")

    monkeypatch.setattr("aster.service.coordinator.DeepSeekResponsesClient", Provider)
    coordinator = RunCoordinator(tmp_path, tools=[], credentials=Credentials())
    try:
        sid = coordinator.create_session(tmp_path)["id"]
        assert calls == []  # Creating service/session performs no provider request.
        run = await coordinator.submit(sid, "check metadata")
        assert (await coordinator.wait(run["id"], timeout=3)).status == "completed"
        assert calls[0]["base_url"] == "https://api.deepseek.com"
        assert calls.index("capabilities") < calls.index("generate")
        assert coordinator.store.get_run(run["id"])["config"]["context_tokens"] == 40000
    finally:
        await coordinator.close()


async def test_mcp_dispatch_failure_is_terminal_and_does_not_call_model(tmp_path):
    model = FakeModel([ModelTurn("must not call")])
    coordinator = RunCoordinator(tmp_path, tools=[], model_factory=lambda _: model)
    try:
        result = await coordinator.execute_tool("missing_tool", {})
        assert result.is_error and result.outcome == "not_executed"
        assert not model.calls
        assert coordinator.store.list_runs()[0]["status"] == "failed"
        assert coordinator._mcp_tasks == {}
    finally:
        await coordinator.close()


async def test_manual_recovery_shares_session_gate_and_global_capacity(tmp_path):
    from aster.state import SessionBusy
    first = RunCoordinator(tmp_path, tools=[], model_factory=lambda _: FakeModel([]))
    sid = first.create_session(tmp_path)["id"]
    old = await first.submit(sid, "old interrupted work")
    first.store.set_run_status(old["id"], "running")
    await first.close()
    entered, release = asyncio.Event(), asyncio.Event()

    async def waiting(messages, tools):
        entered.set()
        await release.wait()
        return ModelTurn("recovered")

    models = [FakeModel([waiting]), FakeModel([ModelTurn("other session")])]
    created = []

    def factory(settings):
        created.append(settings)
        return models.pop(0)

    coordinator = RunCoordinator(tmp_path, tools=[], model_factory=factory, max_parallel_runs=1)
    task = asyncio.create_task(coordinator.recover(old["id"]))
    try:
        await entered.wait()
        with pytest.raises(SessionBusy):
            await coordinator.recover(old["id"])
        other = coordinator.create_session(tmp_path)["id"]
        queued = await coordinator.submit(other, "other")
        await asyncio.sleep(0.03)
        assert len(created) == 1
        release.set()
        assert (await task).status == "completed"
        assert (await coordinator.wait(queued["id"], timeout=3)).status == "completed"
    finally:
        release.set()
        await task
        await coordinator.close()


@pytest.mark.parametrize("condition", ["unknown", "stale", "exhausted"])
async def test_opt_in_auto_recovery_rejects_unsafe_candidates(tmp_path, condition):
    import time

    import yaml

    from aster.engine import RunConfig
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"recovery": {"enabled": True, "max_age_seconds": 60, "max_attempts": 1}}))
    coordinator = RunCoordinator(tmp_path, tools=[], model_factory=lambda _: FakeModel([ModelTurn("must not execute")]))
    sid = coordinator.create_session(tmp_path)["id"]
    run, _ = coordinator.store.create_run(sid, config=RunConfig().__dict__)
    if condition == "unknown":
        entry = coordinator.store.prepare_tool(sid, run["id"], ToolCall("write", "external", {}), risk="external", replay_safe=False)
        coordinator.store.mark_dispatching(entry["id"])
    else:
        with coordinator.store.transaction() as db:
            if condition == "stale":
                db.execute("UPDATE runs SET updated_at=? WHERE id=?", (time.time() - 120, run["id"]))
            else:
                db.execute("UPDATE runs SET resume_attempts=1 WHERE id=?", (run["id"],))
    try:
        await coordinator.start()
        await coordinator._auto_task
        assert coordinator.store.get_run(run["id"])["status"] == "running"
        skipped = [event for event in coordinator.store.events(run_id=run["id"]) if event["type"] == "recovery.skipped"]
        assert skipped
    finally:
        await coordinator.close()


async def test_cancel_recovery_waiting_for_capacity_never_starts_model(tmp_path):
    first = RunCoordinator(tmp_path, tools=[], model_factory=lambda _: FakeModel([]))
    sid = first.create_session(tmp_path)["id"]
    old = await first.submit(sid, "interrupted")
    first.store.set_run_status(old["id"], "running")
    await first.close()
    model = FakeModel([ModelTurn("must not resume")])
    coordinator = RunCoordinator(tmp_path, tools=[], model_factory=lambda _: model, max_parallel_runs=1)
    await coordinator._capacity.acquire()
    task = asyncio.create_task(coordinator.recover(old["id"]))
    try:
        await asyncio.sleep(0.02)
        assert coordinator.cancel(old["id"])
        coordinator._capacity.release()
        assert (await task).status == "cancelled"
        assert not model.calls
    finally:
        if not task.done():
            coordinator._capacity.release()
            await task
        await coordinator.close()


async def test_close_waits_for_manual_recovery_terminal_save(tmp_path):
    first = RunCoordinator(tmp_path, tools=[], model_factory=lambda _: FakeModel([]))
    sid = first.create_session(tmp_path)["id"]
    old = await first.submit(sid, "interrupted")
    first.store.set_run_status(old["id"], "running")
    await first.close()
    entered = asyncio.Event()

    async def wait_forever(messages, tools):
        entered.set()
        await asyncio.Event().wait()

    coordinator = RunCoordinator(tmp_path, tools=[], model_factory=lambda _: FakeModel([wait_forever]))
    task = asyncio.create_task(coordinator.recover(old["id"]))
    await entered.wait()
    await coordinator.close()
    assert task.done()
    assert (await task).status == "cancelled"
    from aster.state import Store
    reopened = Store(tmp_path / "runtime.db")
    try:
        assert reopened.get_run(old["id"])["status"] == "cancelled"
    finally:
        reopened.close()


async def test_live_sse_drains_completion_committed_during_send(tmp_path):
    coordinator = RunCoordinator(tmp_path, tools=[], model_factory=lambda _: FakeModel([]))
    sid = coordinator.create_session(tmp_path)["id"]
    run, _ = coordinator.store.create_run(sid)
    rid = run["id"]
    coordinator.store.set_plan(sid, rid, {"run_id": rid, "steps": ["finish"]})
    app = create_app(coordinator, token=TOKEN, manage_lifecycle=False)
    frames, finished_during_send = [], False
    incoming = asyncio.Queue()
    incoming.put_nowait({"type": "http.request", "body": b"", "more_body": False})

    async def send(message):
        nonlocal finished_during_send
        if message["type"] == "http.response.body":
            body = message.get("body", b"")
            frames.append(body)
            if b"event: run.created" in body and not finished_during_send:
                finished_during_send = True
                coordinator.store.finish_run(rid, "completed", answer="done")
                # Give the server generator a scheduling point while its old
                # event batch is in flight through the actual ASGI response.
                await asyncio.sleep(0)

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
             "http_version": "1.1", "method": "GET", "scheme": "http",
             "path": f"/runs/{rid}/events", "raw_path": f"/runs/{rid}/events".encode(),
             "query_string": b"", "root_path": "", "server": ("localhost", 80),
             "client": ("127.0.0.1", 2345),
             "headers": [(b"host", b"localhost"), (b"authorization", ("Bearer " + TOKEN).encode())]}
    try:
        await asyncio.wait_for(app(scope, incoming.get, send), timeout=3)
        text = b"".join(frames).decode()
        assert finished_during_send
        assert "event: run.finished" in text
        assert text.rfind("event: plan.updated") < text.index("event: run.finished")
        completion = coordinator.store.completion_cursor(rid)
        assert f"id: {completion}\n" in text
        assert coordinator.store.get_plan(sid)["snapshot"]["lifecycle"] == "completed"
    finally:
        await coordinator.close()


async def test_resource_close_failure_does_not_strand_next_queued_run(tmp_path):
    class BrokenResource:
        async def close(self):
            raise RuntimeError("controlled transport shutdown failure")

    class Coordinator(RunCoordinator):
        async def _build_engine(self, run, payload, **kwargs):
            engine, services, resources = await super()._build_engine(run, payload, **kwargs)
            resources.append(BrokenResource())
            return engine, services, resources

    coordinator = Coordinator(tmp_path, tools=[], model_factory=lambda _: FakeModel([ModelTurn("done")]))
    try:
        sid = coordinator.create_session(tmp_path)["id"]
        first = await coordinator.submit(sid, "first")
        second = await coordinator.submit(sid, "second")
        assert (await coordinator.wait(first["id"], timeout=3)).status == "completed"
        assert (await coordinator.wait(second["id"], timeout=3)).status == "completed"
        await coordinator._workers[sid]
        assert any(event["type"] == "resource.close_failed" for event in coordinator.store.events(run_id=first["id"]))
    finally:
        await coordinator.close()
