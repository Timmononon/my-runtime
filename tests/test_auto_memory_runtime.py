"""Real coordinator/SQLite capture flows; all model traffic is offline."""
import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest
import yaml

from aster.domain import ModelTurn, Usage
from aster.engine import FakeModel
from aster.memory.facts import StructuredMemory
from aster.memory.service import MemoryService
from aster.service import RunCoordinator
from aster.state import SessionBusy


class Credentials:
    storage = "vault"

    def __init__(self):
        self.values = {"jev": "offline-jev-credential", "deepseek": "offline-deepseek-credential"}

    def get(self, name="deepseek", **kwargs):
        return self.values.get(name)

    def resolve(self, reference):
        return self.values[reference.split("/")[-1]]


def configuration(root, **updates):
    value = {"auto_memory": {"enabled": True}, **updates}
    (root / "config.yaml").write_text(yaml.safe_dump(value), encoding="utf-8")
    return value


@asynccontextmanager
async def runtime(tmp_path, *, probability=0.95, gate_handler=None, extraction=None, config=None, main_factory=None):
    configuration(tmp_path, **(config or {}))
    coordinator = RunCoordinator(tmp_path, tools=[], credentials=Credentials(),
        model_factory=main_factory or (lambda _: FakeModel([ModelTurn("我会按你的偏好处理。")])) )
    calls, extract_calls = [], []

    async def gate(request):
        calls.append(json.loads(request.content))
        if gate_handler:
            response = await gate_handler(coordinator, calls[-1])
            if response is not None:
                return response
        return httpx.Response(200, json={"model": "jev-1.14.0", "answers": {"remember": {"type": "noul", "noul": probability}},
                                        "usage": {"input_tokens": 100, "output_tokens": 6}})

    def extract(messages, tools):
        state = json.loads(messages[0].content)
        extract_calls.append({"state": state, "tools": tools})
        if extraction:
            return extraction(state)
        source = state["current_user_messages"][0]
        return ModelTurn(json.dumps({"facts": [{"entity": "用户", "relation": "操作系统", "value": "Windows",
            "message_id": source["message_id"], "quote": source["text"]}]}, ensure_ascii=False),
            usage=Usage(input_tokens=140, output_tokens=40))

    coordinator.auto_memory_client_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(gate))
    coordinator.auto_memory_model_factory = lambda config, pin: FakeModel([extract])
    try:
        yield coordinator, calls, extract_calls
    finally:
        await coordinator.close()


async def submit_and_drain(coordinator, session, text="我一直使用 Windows，怎么安装这个？", **kwargs):
    run = await coordinator.submit(session["id"], text, **kwargs)
    await coordinator._workers[session["id"]]
    return coordinator.store.get_run(run["id"])


def facts(coordinator, session):
    return StructuredMemory(coordinator.store, MemoryService(coordinator.data_dir / "memory",
        agent_id=session["agent_id"], project_id=session["project_id"]))


def result(coordinator, run):
    return next(event["payload"] for event in reversed(coordinator.store.events(run_id=run["id"]))
                if event["type"] == "memory.capture")


async def test_question_is_scanned_and_source_linked_fact_saved_without_transcript_noise(tmp_path):
    async with runtime(tmp_path) as (coordinator, calls, extracts):
        session = coordinator.create_session(tmp_path)
        run = await submit_and_drain(coordinator, session)
        assert run["status"] == "completed"
        assert len(calls) == len(extracts) == 1
        assert "怎么安装" in calls[0]["state"]["current_user_messages"][0]["text"]
        saved = facts(coordinator, session).recall()["facts"]
        assert len(saved) == 1 and saved[0]["value"] == "Windows"
        assert saved[0]["scope"] == "agent"
        assert saved[0]["evidence"][0]["verified"]
        assert extracts[0]["tools"] == []
        assert [message.role for message in coordinator.store.history(session["id"])] == ["user", "assistant"]
        assert [message.role for message in coordinator.store.active_history(session["id"])] == ["user", "assistant"]
        assert result(coordinator, run)["saved"] == 1
        receipts = coordinator.store.usage(run["id"])
        assert {item.get("raw", {}).get("purpose") for item in receipts} >= {"memory_auto_gate", "memory_auto_extract"}
        assert coordinator.store.run_budget(run["id"])["model_requests"] == 3
        assert not coordinator.store.has_unresolved(session["id"])
        assert not coordinator.store.pending_requests()
        event_text = json.dumps(coordinator.store.events(run_id=run["id"]), ensure_ascii=False)
        assert "offline-jev-credential" not in event_text
        captures = [event for event in coordinator.store.events(run_id=run["id"]) if event["type"].startswith("memory.capture")]
        assert "怎么安装" not in json.dumps(captures, ensure_ascii=False)


async def test_gate_false_does_not_extract_or_write(tmp_path):
    async with runtime(tmp_path, probability=0.1) as (coordinator, calls, extracts):
        session = coordinator.create_session(tmp_path)
        run = await submit_and_drain(coordinator, session, "这个报错怎么处理？")
        assert len(calls) == 1 and not extracts
        assert facts(coordinator, session).recall()["no_data"]
        assert result(coordinator, run)["reason"] == "below_threshold"


@pytest.mark.parametrize("config", [{"auto_memory": {"enabled": False}}, {"auto_memory": {"enabled": "invalid"}}])
async def test_disabled_or_invalid_optional_config_makes_no_requests(tmp_path, config):
    async with runtime(tmp_path, config=config) as (coordinator, calls, extracts):
        session = coordinator.create_session(tmp_path)
        run = await submit_and_drain(coordinator, session)
        assert run["status"] == "completed"
        assert not calls and not extracts


async def test_project_scope_and_exact_duplicate_suppression(tmp_path):
    async with runtime(tmp_path) as (coordinator, calls, _):
        project = coordinator.workbench.projects.create("测试项目", str(tmp_path))
        session = coordinator.create_session(tmp_path, project_id=project["id"])
        await submit_and_drain(coordinator, session)
        second = await submit_and_drain(coordinator, session)
        assert len(calls) == 2
        saved = facts(coordinator, session).recall(scopes=["project"])["facts"]
        assert len(saved) == 1 and saved[0]["project_id"] == project["id"]
        assert result(coordinator, second)["duplicates"] == 1
        other = coordinator.create_session(tmp_path)
        assert facts(coordinator, other).recall()["no_data"]


async def test_retracted_fact_is_not_resurrected_and_conflicts_are_not_overwritten(tmp_path):
    async with runtime(tmp_path) as (coordinator, _, _):
        session = coordinator.create_session(tmp_path)
        await submit_and_drain(coordinator, session)
        memory = facts(coordinator, session)
        first = memory.recall()["facts"][0]
        memory.forget(first["id"], expected_version=1, reason="用户撤回")
        second = await submit_and_drain(coordinator, session)
        assert result(coordinator, second)["duplicates"] == 1
        assert memory.recall()["no_data"]
        memory.record(entity="用户", relation="操作系统", value="Linux", sources=[
            {"kind": "user_supplied", "quote": "Linux", "label": "explicit offline test"}])
        # A different candidate value must not silently supersede current facts.
        coordinator.auto_memory_model_factory = lambda config, pin: FakeModel([lambda messages, tools: ModelTurn(json.dumps({"facts": [
            {"entity": "用户", "relation": "操作系统", "value": "macOS", "message_id": json.loads(messages[0].content)["current_user_messages"][0]["message_id"],
             "quote": "现在我用 macOS。"}]}))])
        third = await submit_and_drain(coordinator, session, "现在我用 macOS。")
        assert result(coordinator, third)["conflicts"] == 1
        assert memory.recall()["facts"][0]["value"] == "Linux"


@pytest.mark.parametrize("permissions", [{"denied_tools": ["memory_auto_gate"]}, {"ask_tools": ["memory_auto_gate"]},
    {"allowed_tools": ["memory_record"]}, {"denied_tools": ["memory_record"]}])
async def test_narrow_permissions_never_bypassed_and_never_hidden_approval(tmp_path, permissions):
    async with runtime(tmp_path, config={"permissions": permissions}) as (coordinator, calls, _):
        session = coordinator.create_session(tmp_path)
        run = await submit_and_drain(coordinator, session)
        assert run["status"] == "completed"
        assert facts(coordinator, session).recall()["no_data"]
        assert not coordinator.store.pending_requests()
        assert result(coordinator, run)["reason"] == "permission"
        if "memory_record" not in permissions.get("denied_tools", []):
            assert not calls


async def test_current_permission_change_during_gate_stops_extraction(tmp_path):
    async def gate(coordinator, body):
        configuration(tmp_path, permissions={"denied_tools": ["memory_auto_extract"]})
    async with runtime(tmp_path, gate_handler=gate) as (coordinator, calls, extracts):
        session = coordinator.create_session(tmp_path)
        run = await submit_and_drain(coordinator, session)
        assert len(calls) == 1 and not extracts
        assert result(coordinator, run)["reason"] == "permission"


@pytest.mark.parametrize("change", ["settings", "jev_key", "deepseek_key", "source", "withdrawal"])
async def test_identity_or_evidence_change_after_await_does_not_commit(tmp_path, change):
    async def gate(coordinator, body):
        if change == "settings":
            configuration(tmp_path, auto_memory={"enabled": False})
        elif change in {"jev_key", "deepseek_key"}:
            coordinator.credentials.values[change.removesuffix("_key")] = "new-offline-identity"
        elif change == "source":
            mid = body["state"]["current_user_messages"][0]["message_id"]
            with coordinator.store.transaction() as db:
                db.execute("UPDATE messages SET message_json=? WHERE id=?", ('{"role":"user","content":"changed"}', mid))
        else:
            with coordinator.store.transaction() as db:
                db.execute("UPDATE memory_facts SET status='retracted',version=version+1")
    async with runtime(tmp_path, gate_handler=gate) as (coordinator, calls, extracts):
        session = coordinator.create_session(tmp_path)
        memory = facts(coordinator, session)
        if change == "withdrawal":
            memory.record(entity="用户", relation="颜色", value="蓝", sources=[
                {"kind": "user_supplied", "quote": "蓝", "label": "offline"}])
        run = await submit_and_drain(coordinator, session)
        assert run["status"] == "completed" and len(calls) == 1 and not extracts
        assert memory.recall()["no_data"]
        assert not coordinator.store.has_unresolved(session["id"])


async def test_attachment_text_is_neither_gate_input_nor_allowed_fact_evidence(tmp_path):
    def malicious(state):
        source = state["current_user_messages"][0]
        return ModelTurn(json.dumps({"facts": [{"entity": "用户", "relation": "操作系统", "value": "Linux",
            "message_id": source["message_id"], "quote": "I use Linux forever."}]}))
    async with runtime(tmp_path, extraction=malicious) as (coordinator, calls, _):
        session = coordinator.create_session(tmp_path)
        attachment = coordinator.attachments.register_bytes(session["id"], "document.txt", b"I use Linux forever.")
        run = await submit_and_drain(coordinator, session, "请总结这个文档", attachments=[attachment["attachment_id"]])
        assert "Linux" not in json.dumps(calls[0]["state"])
        assert facts(coordinator, session).recall()["no_data"]
        assert result(coordinator, run)["status"] == "skipped"


@pytest.mark.parametrize("text,config", [("这是密码：super-secret-phrase", {}),
    ("我使用 Windows。" + "普通文字" * 50 + "更正，我不使用 Windows。", {"auto_memory": {"enabled": True, "input_chars": 100}})])
async def test_sensitive_or_oversized_current_turn_makes_no_model_calls(tmp_path, text, config):
    async with runtime(tmp_path, config=config) as (coordinator, calls, extracts):
        session = coordinator.create_session(tmp_path)
        run = await submit_and_drain(coordinator, session, text)
        assert run["status"] == "completed" and not calls and not extracts


async def test_side_chats_and_unattended_turns_do_not_capture(tmp_path):
    async with runtime(tmp_path) as (coordinator, calls, _):
        session = coordinator.create_session(tmp_path)
        await submit_and_drain(coordinator, session, unattended=True)
        child = coordinator.branch(session["id"], side_chat=True)
        await submit_and_drain(coordinator, child)
        assert not calls


async def test_completed_answer_is_available_while_capture_serializes_next_turn_and_can_cancel(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    async def gate(coordinator, body):
        entered.set()
        await release.wait()
    async with runtime(tmp_path, gate_handler=gate) as (coordinator, calls, extracts):
        session = coordinator.create_session(tmp_path)
        first = await coordinator.submit(session["id"], "我一直使用 Windows。")
        await asyncio.wait_for(entered.wait(), 3)
        outcome = await coordinator.wait(first["id"], timeout=1)
        assert outcome.status == "completed" and outcome.text
        with pytest.raises(SessionBusy):
            await coordinator.change_session_lifecycle(session["id"], "archive")
        second = await coordinator.submit(session["id"], "第二条")
        assert coordinator.store.get_run(second["id"])["status"] == "queued"
        assert coordinator.cancel(first["id"])
        configuration(tmp_path, auto_memory={"enabled": False})
        await asyncio.wait_for(coordinator._workers[session["id"]], 3)
        assert coordinator.store.get_run(second["id"])["status"] == "completed"
        assert len(calls) == 1 and not extracts
        assert not coordinator.store.has_unresolved(session["id"])


@pytest.mark.parametrize("failure", ["timeout", "server", "invalid_json"])
async def test_remote_failure_does_not_fail_answer_or_strand_dispatch(tmp_path, failure):
    async def gate(coordinator, body):
        if failure == "timeout":
            raise httpx.ReadTimeout("sensitive provider detail")
        if failure == "server":
            return httpx.Response(503, text="sensitive provider detail")
        return httpx.Response(200, text="not JSON sensitive provider detail")
    async with runtime(tmp_path, gate_handler=gate) as (coordinator, calls, extracts):
        session = coordinator.create_session(tmp_path)
        run = await submit_and_drain(coordinator, session)
        assert run["status"] == "completed" and len(calls) == 1 and not extracts
        assert result(coordinator, run)["status"] == "skipped"
        assert not coordinator.store.has_unresolved(session["id"])
        assert "sensitive provider detail" not in json.dumps(coordinator.store.events(run_id=run["id"]))
        unknown_usage = [item for item in coordinator.store.usage(run["id"]) if item.get("raw", {}).get("purpose") == "memory_auto_gate"]
        assert unknown_usage[0]["raw"]["status"] == "outcome_unknown"


async def test_run_request_budget_applies_to_optional_capture(tmp_path):
    async with runtime(tmp_path, config={"runtime": {"max_model_requests": 1}}) as (coordinator, calls, _):
        session = coordinator.create_session(tmp_path)
        run = await submit_and_drain(coordinator, session)
        assert run["status"] == "completed" and not calls
        assert result(coordinator, run)["reason"] == "budget"


async def test_lower_scope_cannot_enable_capture(tmp_path):
    async with runtime(tmp_path, config={"auto_memory": {"enabled": False}}) as (coordinator, calls, _):
        session = coordinator.create_session(tmp_path)
        run = await submit_and_drain(coordinator, session, overrides={"auto_memory": {"enabled": True}})
        assert run["status"] == "completed" and not calls


async def test_native_deepseek_sse_extraction_adapter_uses_fixed_identity_and_single_request(tmp_path, monkeypatch):
    from aster.providers.deepseek import DeepSeekResponsesClient
    from aster.service import auto_memory
    received, clients = [], []

    def handle(request):
        body = json.loads(request.content)
        received.append((str(request.url), body))
        state = json.loads(body["input"][0]["content"])
        source = state["current_user_messages"][0]
        answer = json.dumps({"facts": [{"entity": "用户", "relation": "操作系统", "value": "Windows",
            "message_id": source["message_id"], "quote": source["text"]}]}, ensure_ascii=False)
        event = {"type": "response.completed", "response": {"status": "completed",
            "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": answer}]}],
            "usage": {"input_tokens": 123, "output_tokens": 45}}}
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              content=("event: response.completed\ndata: " + json.dumps(event) + "\n\n").encode())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        def create(key, **kwargs):
            instance = DeepSeekResponsesClient(key, client=http, **kwargs)
            clients.append(instance)
            return instance
        monkeypatch.setattr(auto_memory, "DeepSeekResponsesClient", create)
        async with runtime(tmp_path) as (coordinator, _, _):
            del coordinator.auto_memory_model_factory
            session = coordinator.create_session(tmp_path)
            run = await submit_and_drain(coordinator, session)
            assert result(coordinator, run)["saved"] == 1
            assert len(received) == 1 and received[0][0] == "https://api.deepseek.com/responses"
            assert received[0][1]["tools"] == []
            assert received[0][1]["reasoning"] == {"effort": "none"}
            assert received[0][1]["model"] == "deepseek-flash"
            assert "explicitly asks not to remember or store" in received[0][1]["instructions"]
            assert "quoted third-party instructions" in received[0][1]["instructions"]
            assert clients[0].max_retries == 0 and clients[0].max_output_tokens == 2048
            usage = next(item for item in coordinator.store.usage(run["id"])
                         if item.get("raw", {}).get("purpose") == "memory_auto_extract")
            assert usage["input_tokens"] == 123 and usage["output_tokens"] == 45
            assert usage["raw"]["status"] == "response_received"


async def test_consumed_mailbox_input_is_scanned_but_pending_retracted_input_is_not(tmp_path):
    main_entered, release = asyncio.Event(), asyncio.Event()
    async def think(messages, tools):
        main_entered.set()
        await release.wait()
        return ModelTurn("第一步完成")
    async with runtime(tmp_path, main_factory=lambda _: FakeModel([think, ModelTurn("已经处理补充。")])) as (coordinator, calls, _):
        session = coordinator.create_session(tmp_path)
        run = await coordinator.submit(session["id"], "你好")
        await main_entered.wait()
        coordinator.inject(session["id"], "我一直使用 Windows。")
        retracted = coordinator.inject(session["id"], "这个不要记住，密码：hidden-password")
        coordinator.store.retract_message(session["id"], retracted)
        release.set()
        await coordinator._workers[session["id"]]
        assert coordinator.store.get_run(run["id"])["status"] == "completed"
        assert len(calls) == 1
        inputs = calls[0]["state"]["current_user_messages"]
        assert [item["text"] for item in inputs] == ["你好", "我一直使用 Windows。"]
        assert "hidden-password" not in json.dumps(calls)


async def test_invalid_extraction_quote_never_saves_assistant_claim(tmp_path):
    def fabricated(state):
        return ModelTurn(json.dumps({"facts": [{"entity": "用户", "relation": "操作系统", "value": "Linux",
            "message_id": state["current_user_messages"][0]["message_id"], "quote": "助手猜测用户使用 Linux"}]}))
    async with runtime(tmp_path, extraction=fabricated) as (coordinator, _, extracts):
        session = coordinator.create_session(tmp_path)
        run = await submit_and_drain(coordinator, session)
        assert len(extracts) == 1 and facts(coordinator, session).recall()["no_data"]
        assert run["status"] == "completed" and result(coordinator, run)["saved"] == 0


async def test_slow_cancelled_cleanup_cannot_outlive_session_or_store(tmp_path):
    from aster.state import Store
    entered, release, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
    class SlowCleanup(FakeModel):
        async def aclose(self):
            entered.set()
            try:
                while not release.is_set():
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        continue
            finally:
                stopped.set()
    async with runtime(tmp_path) as (coordinator, _, _):
        coordinator.auto_memory_model_factory = lambda config, pin: SlowCleanup([ModelTurn('{"facts":[]}')])
        session = coordinator.create_session(tmp_path)
        run = await coordinator.submit(session["id"], "我一直使用 Windows。")
        await asyncio.wait_for(entered.wait(), 3)
        checkpoint = coordinator.store.get_checkpoint(session["id"])
        closing = asyncio.create_task(coordinator.close())
        try:
            await asyncio.sleep(2.2)  # Exercise the dispatcher's two-second cancellation grace.
            assert not closing.done()
            assert not stopped.is_set()
            assert coordinator.store.connection.execute("SELECT 1").fetchone()[0] == 1
        finally:
            release.set()
            await asyncio.wait_for(closing, 3)
        assert stopped.is_set()
    reopened = Store(tmp_path / "runtime.db")
    try:
        assert not reopened.has_unresolved(session["id"])
        assert reopened.get_run(run["id"])["status"] == "completed"
        assert reopened.get_checkpoint(session["id"])["archive_through"] == checkpoint["archive_through"]
    finally:
        reopened.close()


async def test_restart_settles_only_own_post_completion_pure_evaluation_without_replay(tmp_path):
    from aster.domain import ToolCall
    async with runtime(tmp_path, probability=0.1) as (coordinator, calls, _):
        session = coordinator.create_session(tmp_path)
        run = await submit_and_drain(coordinator, session)
        checkpoint = coordinator.store.get_checkpoint(session["id"])
        with coordinator.store.transaction() as db:
            db.execute("UPDATE tool_executions SET state='dispatching',result_json=NULL WHERE run_id=?", (run["id"],))
            # Crash before provider response: no token/cost attribution invented.
            db.execute("UPDATE usage SET usage_json=? WHERE run_id=? AND usage_json LIKE '%memory_auto_gate%'",
                (json.dumps({"input_tokens": None, "output_tokens": None,
                             "raw": {"purpose": "memory_auto_gate", "status": "outcome_unknown"}}), run["id"]))
        assert coordinator.store.has_unresolved(session["id"])
        assert len(calls) == 1
    configuration(tmp_path, auto_memory={"enabled": False})
    coordinator = RunCoordinator(tmp_path, tools=[], credentials=Credentials(),
                                  model_factory=lambda _: FakeModel([ModelTurn("正常继续")]))
    try:
        # A separate session contains a material unknown; startup must not
        # decide it merely because its tool has the same name as our helper.
        other = coordinator.create_session(tmp_path)
        other_run, _ = coordinator.store.create_run(other["id"])
        entry = coordinator.store.prepare_tool(other["id"], other_run["id"],
            ToolCall("capture_memory_auto_gate", "memory_auto_gate", {}), risk="external", replay_safe=False)
        coordinator.store.mark_dispatching(entry["id"])
        coordinator.store.finish_run(other_run["id"], "completed")
        marker = coordinator.store.emit_event(other["id"], other_run["id"], "memory.capture.started", {})
        # Tied wall-clock timestamps must not prove post-marker ownership.
        with coordinator.store.transaction() as db:
            timestamp = db.execute("SELECT created_at FROM events WHERE event_id=?", (marker["event_id"],)).fetchone()[0]
            db.execute("UPDATE tool_executions SET created_at=? WHERE id=?", (timestamp, entry["id"]))
        await coordinator.start()
        assert not coordinator.store.has_unresolved(session["id"])
        assert coordinator.store.has_unresolved(other["id"])
        assert coordinator.store.get_checkpoint(session["id"])["archive_through"] == checkpoint["archive_through"]
        assert not coordinator._workers  # No completed run is automatically scanned again.
        unknown = next(item for item in coordinator.store.usage(run["id"]) if item.get("raw", {}).get("purpose") == "memory_auto_gate")
        assert unknown["raw"]["status"] == "outcome_unknown" and unknown["input_tokens"] is None
        next_run = await submit_and_drain(coordinator, session)
        assert next_run["status"] == "completed"
    finally:
        await coordinator.close()


async def test_local_cleanup_never_settles_preexisting_same_named_unknown_action(tmp_path):
    from aster.domain import ToolCall, ToolResult
    async with runtime(tmp_path) as (coordinator, calls, _):
        session = coordinator.create_session(tmp_path)
        run = await coordinator.submit(session["id"], "我一直使用 Windows。")
        entries = []
        async def completion_sink(kind, payload):
            if kind == "run.finished":
                entry = coordinator.store.prepare_tool(session["id"], run["id"],
                    ToolCall("capture_memory_auto_gate", "memory_auto_gate", {}), risk="external", replay_safe=False)
                coordinator.store.mark_dispatching(entry["id"])
                coordinator.store.commit_tool(entry["id"], ToolResult("Preexisting unknown action", outcome="unknown"))
                entries.append(entry["id"])
        coordinator.add_sink(run["id"], completion_sink)
        await coordinator._workers[session["id"]]
        assert entries and not calls
        assert coordinator.store.tool_execution(entries[0])["state"] == "outcome_unknown"
        assert coordinator.store.has_unresolved(session["id"])
        assert result(coordinator, run)["reason"] == "permission"
