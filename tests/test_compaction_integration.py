"""Manual helper lifecycle and long-task evidence integration, entirely offline."""
import asyncio
import copy
import hashlib
from pathlib import Path

import httpx
import pytest
import yaml

from aster.api import create_app
from aster.domain import Message, ModelTurn, ToolCall, ToolResult, ToolSpec, Usage, to_dict
from aster.engine import FakeModel
from aster.providers.deepseek import encode_messages
from aster.service import RunCoordinator
from aster.state import SessionBusy, StateError
from aster.state.compaction import compaction_source_digest
from aster.tools.files import build_file_tools

SUMMARY = "<summary>\n## Current task & next steps\nContinue from the archived evidence.\n## User corrections & decisions\nPreserve original files.\n</summary>"


class NoCredentials:
    def get(self, *args, **kwargs):
        return None

    def resolve(self, reference):
        raise AssertionError("Offline tests must not read credentials")


def coordinator_at(tmp_path, model_factory, *, tools=(), runtime=None, max_parallel_runs=4):
    data = tmp_path / "runtime"
    data.mkdir(exist_ok=True)
    (data / "config.yaml").write_text(yaml.safe_dump({"runtime": {
        "compaction_persist_learnings": False, "file_restore_max_files": 0, **(runtime or {}),
    }}), encoding="utf-8")
    return RunCoordinator(data, model_factory=model_factory, credentials=NoCredentials(), tools=list(tools),
                          browser_enabled=False, max_parallel_runs=max_parallel_runs)


def seed_history(coordinator, tmp_path, *, prior_checkpoint=False):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    sid = coordinator.create_session(workspace)["id"]
    coordinator.store.append_message(sid, None, Message("user", "Read the evidence and preserve original files."))
    for index in range(14):
        coordinator.store.append_message(sid, None, Message("assistant", tool_calls=[ToolCall(f"old-{index}", "archived_read", {})]))
        coordinator.store.append_message(sid, None, Message("tool", f"Evidence #{index}\n" + "source fact " * 500,
                                                           tool_call_id=f"old-{index}"))
    coordinator.store.append_message(sid, None, Message("assistant", "Evidence collected; next step is analysis."))
    if prior_checkpoint:
        coordinator.store.checkpoint(sid, "prior", coordinator.store.history(sid), metadata={"sentinel": "keep-until-success"})
    return sid


async def test_manual_semantic_helpers_are_tool_free_metered_and_archive_is_unchanged(tmp_path):
    model = FakeModel([ModelTurn("- The user requires original files to remain unchanged.", usage=Usage(input_tokens=100, output_tokens=20)),
                       ModelTurn(SUMMARY, usage=Usage(input_tokens=200, output_tokens=40))])
    coordinator = coordinator_at(tmp_path, lambda _: model, runtime={"compaction_persist_learnings": True})
    sid = seed_history(coordinator, tmp_path)
    original = copy.deepcopy(coordinator.store.history_records(sid))
    try:
        result = await coordinator.manual_compact(sid, method="semantic")
        assert result["changed"] and result["semantic_applied"] and result["summary_status"] == "succeeded"
        assert result["after_tokens"] < result["before_tokens"]
        assert coordinator.store.history_records(sid) == original
        assert model.calls and all(call["tools"] == [] for call in model.calls)
        run = coordinator.store.get_run(result["run_id"])
        assert run["status"] == "completed"
        assert coordinator.store.run_request(run["id"])["kind"] == "compaction"
        budget = coordinator.store.run_budget(run["id"])
        assert budget["model_requests"] == len(model.calls) == 2
        assert budget["tool_calls"] == 0 and not coordinator.store.tool_executions(run["id"])
        usage = coordinator.store.usage(run["id"])
        assert [row["raw"]["purpose"] for row in usage] == ["compaction_learning", "compaction"]
        assert sum(row["input_tokens"] for row in usage) == 300
        from aster.engine.telemetry import performance
        trace = performance(coordinator.store, run["id"])
        assert trace["phase"] == "done"
        assert {"setup", "compacting", "awaiting_model"} <= set(trace["phase_seconds"])
        checkpoint = coordinator.store.get_checkpoint(sid)
        assert checkpoint["metadata"]["manual_compaction"] == result
        assert not coordinator._manual_tasks and not coordinator._controls and not coordinator._engines
        encode_messages(coordinator.store.active_history(sid))
    finally:
        await coordinator.close()


async def test_manual_semantic_empty_history_does_not_wait_for_capacity_or_create_run(tmp_path):
    def no_model(_):
        pytest.fail("Empty history must not construct a model")

    coordinator = coordinator_at(tmp_path, no_model, max_parallel_runs=1)
    sid = coordinator.create_session(tmp_path)["id"]
    await coordinator._capacity.acquire()
    try:
        result = await asyncio.wait_for(coordinator.manual_compact(sid, method="semantic"), .5)
        assert not result["changed"] and not result["semantic_applied"]
        assert not coordinator.store.list_runs(sid) and coordinator.store.get_checkpoint(sid) is None
    finally:
        coordinator._capacity.release()
        await coordinator.close()


async def test_manual_semantic_does_not_discover_mcp_or_execute_hooks(tmp_path, monkeypatch):
    model = FakeModel([ModelTurn(SUMMARY)])
    coordinator = coordinator_at(tmp_path, lambda _: model)
    configuration = yaml.safe_load((coordinator.data_dir / "config.yaml").read_text())
    configuration.update(mcp_servers=[{"name": "blocked", "command": "never-start-this-process"}],
                         hooks=[{"event": "SessionStart", "executable": "never-start-this-process"}])
    (coordinator.data_dir / "config.yaml").write_text(yaml.safe_dump(configuration), encoding="utf-8")

    async def no_mcp(*args, **kwargs):
        pytest.fail("Context-only helper must never discover MCP tools")

    monkeypatch.setattr(coordinator, "_mcp_client_for", no_mcp)
    sid = seed_history(coordinator, tmp_path)
    try:
        result = await coordinator.manual_compact(sid, method="semantic")
        assert result["semantic_applied"] and len(model.calls) == 1
        assert model.calls[0]["tools"] == []
        assert not coordinator.store.tool_executions(result["run_id"])
        assert not any(event["type"].startswith("hook.") for event in coordinator.store.events(session_id=sid))
    finally:
        await coordinator.close()


async def test_legacy_no_body_api_uses_local_compaction_without_model(tmp_path):
    def no_model(_):
        pytest.fail("Default manual compaction must not construct a model")

    coordinator = coordinator_at(tmp_path, no_model)
    sid = seed_history(coordinator, tmp_path)
    original = copy.deepcopy(coordinator.store.history_records(sid))
    token = "synthetic-compaction-test-bearer-credential"
    app = create_app(coordinator, token=token)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765",
                                     headers={"Authorization": "Bearer " + token}) as client:
            response = await client.post(f"/sessions/{sid}/compact")
            assert response.status_code == 200, response.text
            assert response.json()["method"] == "deterministic" and response.json()["changed"]
            assert response.json()["semantic_applied"] is False
            invalid = await client.post(f"/sessions/{sid}/compact", json={"method": "unknown"})
            assert invalid.status_code == 422
        assert coordinator.store.history_records(sid) == original
        assert not coordinator.store.list_runs(sid)
    finally:
        await coordinator.close()


async def test_manual_semantic_provider_setup_failure_preserves_checkpoint_and_cleans_writer(tmp_path):
    def broken(_):
        raise RuntimeError("Synthetic model initialization failure")

    coordinator = coordinator_at(tmp_path, broken)
    sid = seed_history(coordinator, tmp_path, prior_checkpoint=True)
    before = copy.deepcopy(coordinator.store.get_checkpoint(sid))
    original = copy.deepcopy(coordinator.store.history_records(sid))
    try:
        with pytest.raises(RuntimeError, match="initialization"):
            await coordinator.manual_compact(sid, method="semantic")
        assert coordinator.store.get_checkpoint(sid) == before
        assert coordinator.store.history_records(sid) == original
        assert coordinator.store.list_runs(sid)[0]["status"] == "failed"
        assert not coordinator._session_gates[sid].locked()
        assert not coordinator._manual_tasks and not coordinator._controls
    finally:
        await coordinator.close()


async def test_cancel_manual_semantic_preserves_checkpoint_and_rejects_second_writer(tmp_path):
    entered = asyncio.Event()

    async def blocked(messages, tools):
        assert tools == []
        entered.set()
        await asyncio.Event().wait()

    coordinator = coordinator_at(tmp_path, lambda _: FakeModel([blocked]))
    sid = seed_history(coordinator, tmp_path, prior_checkpoint=True)
    before = copy.deepcopy(coordinator.store.get_checkpoint(sid))
    original = copy.deepcopy(coordinator.store.history_records(sid))
    task = asyncio.create_task(coordinator.manual_compact(sid, method="semantic"))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        with pytest.raises(SessionBusy, match="writer"):
            await coordinator.manual_compact(sid)
        rid = coordinator.store.list_runs(sid)[0]["id"]
        assert coordinator.cancel(rid)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        assert coordinator.store.get_checkpoint(sid) == before
        assert coordinator.store.history_records(sid) == original
        assert coordinator.store.get_run(rid)["status"] == "cancelled"
        assert coordinator.store.run_budget(rid)["model_requests"] == 1
        assert coordinator.store.usage(rid)[0]["raw"]["status"] == "outcome_unknown"
        assert not coordinator._manual_tasks and not coordinator._session_gates[sid].locked()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await coordinator.close()


async def test_manual_semantic_budget_limit_reports_local_fallback_without_extra_calls(tmp_path):
    model = FakeModel([ModelTurn("- Keep source files unchanged.", usage=Usage(input_tokens=100, output_tokens=20))])
    coordinator = coordinator_at(tmp_path, lambda _: model, runtime={"max_model_requests": 1, "compaction_persist_learnings": True})
    sid = seed_history(coordinator, tmp_path)
    try:
        result = await coordinator.manual_compact(sid, method="semantic")
        assert result["changed"] and not result["semantic_applied"]
        assert result["summary_status"] == "failed"
        assert len(model.calls) == coordinator.store.run_budget(result["run_id"])["model_requests"] == 1
    finally:
        await coordinator.close()


async def test_manual_helper_skips_request_larger_than_remaining_tokens(tmp_path):
    model = FakeModel([])
    coordinator = coordinator_at(tmp_path, lambda _: model, runtime={"max_total_tokens": 100})
    sid = seed_history(coordinator, tmp_path)
    try:
        result = await coordinator.manual_compact(sid, method="semantic")
        assert result["changed"] and not result["semantic_applied"]
        assert not model.calls
        assert coordinator.store.run_budget(result["run_id"])["model_requests"] == 0
    finally:
        await coordinator.close()


async def test_manual_semantic_preserves_observation_checkpoint_state(tmp_path):
    model = FakeModel([ModelTurn(SUMMARY)])
    coordinator = coordinator_at(tmp_path, lambda _: model)
    sid = seed_history(coordinator, tmp_path)
    coordinator.store.checkpoint(sid, "prior", coordinator.store.history(sid), metadata={
        "observation_state": {"version": 1, "replacements": {}, "seen": {"prior-read": 777}}, "sentinel": "preserve"})
    try:
        result = await coordinator.manual_compact(sid, method="semantic")
        assert result["semantic_applied"]
        metadata = coordinator.store.get_checkpoint(sid)["metadata"]
        assert metadata["observation_state"]["seen"]["prior-read"] == 777
        assert metadata["sentinel"] == "preserve"
    finally:
        await coordinator.close()


async def test_manual_semantic_obeys_global_capacity_and_releases_after_cancel(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    constructed = []

    async def first(messages, tools):
        entered.set()
        await release.wait()
        return ModelTurn(SUMMARY)

    def factory(_):
        model = FakeModel([first if not constructed else ModelTurn(SUMMARY)])
        constructed.append(model)
        return model

    coordinator = coordinator_at(tmp_path, factory, max_parallel_runs=1)
    first_id, second_id = seed_history(coordinator, tmp_path), seed_history(coordinator, tmp_path)
    first_task = asyncio.create_task(coordinator.manual_compact(first_id, method="semantic"))
    second_task = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        second_task = asyncio.create_task(coordinator.manual_compact(second_id, method="semantic"))
        await asyncio.sleep(.05)
        assert len(constructed) == 1 and not coordinator.store.list_runs(second_id)
        assert len(coordinator._manual_tasks) == 2
        release.set()
        results = await asyncio.wait_for(asyncio.gather(first_task, second_task), 5)
        assert all(result["semantic_applied"] for result in results)
        assert len(constructed) == 2 and coordinator._capacity._value == 1
    finally:
        for task in (first_task, second_task):
            if task:
                task.cancel()
        await asyncio.gather(*[task for task in (first_task, second_task) if task], return_exceptions=True)
        await coordinator.close()


@pytest.mark.parametrize("waiting_for_capacity", [False, True])
async def test_shutdown_drains_manual_compaction_before_closing_store(tmp_path, waiting_for_capacity):
    entered = asyncio.Event()

    async def blocked(messages, tools):
        entered.set()
        await asyncio.Event().wait()

    coordinator = coordinator_at(tmp_path, lambda _: FakeModel([blocked]), max_parallel_runs=1)
    sid = seed_history(coordinator, tmp_path, prior_checkpoint=True)
    if waiting_for_capacity:
        await coordinator._capacity.acquire()
    task = asyncio.create_task(coordinator.manual_compact(sid, method="semantic"))
    try:
        if waiting_for_capacity:
            await asyncio.sleep(.02)
            assert task in coordinator._manual_tasks and not coordinator.store.list_runs(sid)
        else:
            await asyncio.wait_for(entered.wait(), 3)
        await asyncio.wait_for(coordinator.close(), 3)
        assert task.done() and task.cancelled()
        assert not coordinator._manual_tasks and not coordinator._controls
        with pytest.raises(StateError, match="shutting down"):
            await coordinator.manual_compact(sid)
    finally:
        if waiting_for_capacity:
            coordinator._capacity.release()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if not coordinator._closed:
            await coordinator.close()


class LongRead:
    spec = ToolSpec("long_read", "Read synthetic long task evidence", {
        "type": "object", "properties": {"index": {"type": "integer"}}, "required": ["index"], "additionalProperties": False,
    }, replay_safe=True, concurrency_safe=True)

    async def execute(self, args, context):
        return ToolResult(f"evidence index={args['index']}\n" + "measurement " * 2200)


class LongTaskModel:
    model = "offline-long-task"

    def __init__(self, rounds=35):
        self.rounds, self.main_calls, self.summary_calls, self.requests = rounds, 0, 0, []

    async def generate(self, messages, tools, *, system, **kwargs):
        self.requests.append(copy.deepcopy(messages))
        encode_messages(messages)
        if "Compress the quoted conversation" in system:
            assert tools == []
            self.summary_calls += 1
            return ModelTurn(SUMMARY, usage=Usage(input_tokens=1000, output_tokens=100))
        assert "Compare four attachment sources; preserve original files." in "\n".join(message.content for message in messages)
        self.main_calls += 1
        if self.main_calls <= self.rounds:
            return ModelTurn(tool_calls=[ToolCall(f"long-{self.main_calls}", "long_read", {"index": self.main_calls})])
        return ModelTurn("Completed synthetic evidence review.")


@pytest.mark.parametrize("semantic", [False, True])
async def test_four_16k_attachments_survive_default_96k_long_task_compaction(tmp_path, semantic):
    model = LongTaskModel()
    coordinator = coordinator_at(tmp_path, lambda _: model, tools=[LongRead()], runtime={"semantic_compaction": semantic})
    sid = coordinator.create_session(tmp_path)["id"]
    attachments = [coordinator.attachments.register_bytes(sid, f"evidence-{index}.txt", (f"FILE{index} " + "source fact " * 1800)[:16000].encode())
                   for index in range(4)]
    try:
        run = await coordinator.submit(sid, "Compare four attachment sources; preserve original files.",
                                       attachments=[item["attachment_id"] for item in attachments])
        outcome = await coordinator.wait(run["id"], timeout=30)
        assert outcome.status == "completed", outcome.error
        assert model.main_calls == 36
        archive = coordinator.store.history(sid)
        request = next(message for message in archive if message.metadata.get("attachment_ids"))
        assert request.metadata["user_text"] == "Compare four attachment sources; preserve original files."
        assert len(request.metadata["attachment_sources"]) == 4
        assert len(request.content) > 64000
        assert all(hashlib.sha256(Path(item["path"]).read_bytes()).hexdigest() == item["sha256"] for item in attachments)
        compacted = [event for event in coordinator.store.events(session_id=sid) if event["type"] == "context.compacted"]
        assert compacted and all(event["payload"]["after_tokens"] < event["payload"]["before_tokens"] for event in compacted)
        active = coordinator.store.active_history(sid)
        summary = next(message for message in active if message.metadata.get("compaction_summary"))
        assert not any("source fact " * 10 in text for text in summary.metadata["protected_requests"])
        references = "\n".join(summary.metadata["protected_references"])
        assert all(item["attachment_id"] in references and item["sha256"] in references for item in attachments)
        assert model.summary_calls > 0 if semantic else model.summary_calls == 0
        encode_messages(active)
    finally:
        await coordinator.close()


class HugeRead:
    spec = ToolSpec("huge_read", "Read synthetic evidence that exceeds a model result budget", {
        "type": "object", "properties": {}, "additionalProperties": False,
    }, replay_safe=True, concurrency_safe=True)

    async def execute(self, args, context):
        return ToolResult("EXACT-SPILL-EVIDENCE\n" + "original result " * 3000)


async def test_spill_can_be_read_across_runs_only_by_authorized_history_boundary(tmp_path):
    queue = [FakeModel([ModelTurn("Before the spill")]),
             FakeModel([ModelTurn(tool_calls=[ToolCall("spill", "huge_read", {})]), ModelTurn("Spill registered")])]
    read_tool = next(tool for tool in build_file_tools() if tool.spec.name == "file_read")
    coordinator = coordinator_at(tmp_path, lambda _: queue.pop(0), tools=[HugeRead(), read_tool], runtime={"tool_output_chars": 1000})
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sid = coordinator.create_session(workspace)["id"]
    try:
        first = await coordinator.submit(sid, "Start evidence review")
        assert (await coordinator.wait(first["id"], timeout=3)).status == "completed"
        boundary = len(coordinator.store.history(sid))
        second = await coordinator.submit(sid, "Read the large evidence")
        assert (await coordinator.wait(second["id"], timeout=3)).status == "completed"
        source = next(message for message in coordinator.store.history(sid) if message.tool_call_id == "spill")
        spill = source.metadata["output_artifact"]
        original_hash = hashlib.sha256(Path(spill["path"]).read_bytes()).hexdigest()
        authorized = coordinator.branch(sid)["id"]
        earlier = coordinator.branch(sid, message_index=boundary)["id"]
        unrelated = coordinator.create_session(workspace)["id"]
        for index, (target, allowed) in enumerate(((sid, True), (authorized, True), (earlier, False), (unrelated, False))):
            queue.append(FakeModel([ModelTurn(tool_calls=[ToolCall(f"reread-{index}", "file_read", {"path": spill["path"]})]), ModelTurn("Checked")]))
            run = await coordinator.submit(target, "Read the registered evidence")
            outcome = await coordinator.wait(run["id"], timeout=3)
            assert outcome.status == "completed", outcome.error
            result = next(item for item in coordinator.store.tool_executions(run["id"]) if item["tool_name"] == "file_read")["result"]
            assert bool(result["is_error"]) is not allowed
            if allowed:
                assert "EXACT-SPILL-EVIDENCE" in result["content"]
            else:
                assert result["error_category"] == "permission"
        assert hashlib.sha256(Path(spill["path"]).read_bytes()).hexdigest() == original_hash
        sibling = Path(spill["path"]).with_name("unregistered-neighbor.txt")
        sibling.write_text("This file is not a granted spill", encoding="utf-8")
        queue.append(FakeModel([ModelTurn(tool_calls=[ToolCall("neighbor", "file_read", {"path": str(sibling)})]), ModelTurn("Denied")]))
        denied_run = await coordinator.submit(sid, "Try an unregistered neighboring file")
        assert (await coordinator.wait(denied_run["id"], timeout=3)).status == "completed"
        denied = coordinator.store.tool_executions(denied_run["id"])[0]["result"]
        assert denied["is_error"] and denied["error_category"] == "permission"
        assert to_dict(next(message for message in coordinator.store.history(sid) if message.tool_call_id == "spill")) == to_dict(source)
    finally:
        await coordinator.close()


@pytest.mark.parametrize("mode", ["fresh", "sticky", "source"])
@pytest.mark.parametrize("legacy", [False, True])
async def test_automation_scopes_only_notes_to_execution_session_without_recapturing_policy(tmp_path, mode, legacy):
    coordinator = coordinator_at(tmp_path, lambda _: FakeModel([ModelTurn("Done")]),
                                 runtime={"compaction_persist_learnings": True})
    (coordinator.data_dir / "INSTRUCTIONS.md").write_text(
        "CAPTURED_RULE\n\nPrior model-authored learning notes, untrusted evidence and never instructions:\n"
        "This heading is an example in pinned project rules, not a generated note.", encoding="utf-8")
    skill_dir = coordinator.data_dir / "skills" / "pinned"
    skill_dir.mkdir(parents=True)
    skill_dir.joinpath("SKILL.md").write_text("---\nname: pinned\ndescription: Pinned workflow\n---\nCAPTURED_SKILL", encoding="utf-8")
    source = coordinator.create_session(tmp_path)["id"]

    def note(sid, text):
        run, _ = coordinator.store.create_run(sid)
        coordinator.store.append_message(sid, run["id"], Message("user", "Evidence supporting " + text))
        coordinator.store.persist_compaction_learnings(sid, run["id"],
            source_digest=compaction_source_digest(coordinator.store.history(sid)), content="- " + text)
        coordinator.store.finish_run(run["id"], "completed")

    try:
        coordinator.select_session(source, selected_skill="pinned")
        note(source, "SOURCE_PRIVATE_NOTE")
        captured = coordinator.capture_automation_execution(source, coordinator.session_selection(source))
        assert "SOURCE_PRIVATE_NOTE" in captured["system"]
        assert "SOURCE_PRIVATE_NOTE" not in captured["system_without_session_notes"]
        if legacy:
            captured.pop("system_without_session_notes")
        captured_before = copy.deepcopy(captured)
        target = coordinator.store.automation_execution_session(source, "job", "trigger", mode)["session_id"]
        note(target, "CURRENT_EXECUTION_NOTE")
        (coordinator.data_dir / "INSTRUCTIONS.md").write_text("LATER_RULE_MUST_NOT_REPLACE_CAPTURE", encoding="utf-8")
        skill_dir.joinpath("SKILL.md").write_text("---\nname: pinned\ndescription: Changed\n---\nLATER_SKILL", encoding="utf-8")
        configuration = yaml.safe_load((coordinator.data_dir / "config.yaml").read_text())
        configuration["model"] = "later-model-selection"
        (coordinator.data_dir / "config.yaml").write_text(yaml.safe_dump(configuration), encoding="utf-8")
        run = await coordinator.submit(target, "Run the pinned automation", execution_snapshot=captured)
        assert (await coordinator.wait(run["id"], timeout=3)).status == "completed"
        assert "CURRENT_EXECUTION_NOTE" in run["system"]
        assert ("SOURCE_PRIVATE_NOTE" in run["system"]) is (mode == "source")
        assert "CAPTURED_RULE" in run["system"] and "CAPTURED_SKILL" in run["system"]
        assert "This heading is an example in pinned project rules" in run["system"]
        assert "LATER_RULE" not in run["system"] and "LATER_SKILL" not in run["system"]
        assert coordinator.store.run_request(run["id"])["snapshot"]["model"] == captured["snapshot"]["model"]
        assert captured == captured_before
    finally:
        await coordinator.close()
