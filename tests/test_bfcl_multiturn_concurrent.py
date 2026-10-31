"""Offline same-campaign concurrency, using simulated tools and HTTP only."""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

import httpx
import pytest
from test_bfcl_current import source_at
from test_bfcl_v4_live import response
from test_bfcl_v4_tools import filesystem_case

from aster.evaluation.bfcl_current import capture_current_profile

PROJECT = Path(__file__).resolve().parents[1]
BACKEND_PATH = Path(os.environ.get("ASTER_TEST_CONCURRENT_BACKEND", str(
    PROJECT / "src/aster/evaluation/bfcl_multiturn_concurrent.py")))
SPEC = importlib.util.spec_from_file_location("_aster_concurrent_backend_test", BACKEND_PATH)
backend = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = backend
SPEC.loader.exec_module(backend)


def case_at(index):
    case = filesystem_case()
    case["id"] = f"multi_turn_base_{index}"
    case["question"][0][0]["content"] = f"Create memo.txt containing alpha-{index}."
    case["expected"][0][1] = f"echo(content='alpha-{index}', file_name='memo.txt')"
    return case


def setup_campaign(tmp_path, count=5, maximum=None):
    source = source_at(tmp_path)
    profile = capture_current_profile(source, model="deepseek-flash", reasoning_effort="none")
    root = tmp_path / "campaign"
    backend.initialize_campaign([case_at(i) for i in range(count)], root, profile=profile,
        maximum_concurrency=maximum or count, _test_only=True)
    return root, profile


def phases(index, extra=0):
    value = f"alpha-{index}"
    return [
        {"calls": [("touch", {"file_name": "memo.txt"}),
                   ("echo", {"content": value, "file_name": "memo.txt"})]},
        *[{"calls": [("echo", {"content": value if i == extra - 1 else f"progress-{i}",
                                "file_name": "memo.txt"})]} for i in range(extra)],
        {"text": "Created."}, {"calls": [("cat", {"file_name": "memo.txt"})]}, {"text": value},
    ]


def factory_for(index, *, extra=0):
    script = phases(index, extra)
    calls = []

    async def handle(request):
        offset = len(calls)
        calls.append(request)
        return response(request, index=offset, **script[offset])

    return lambda: httpx.MockTransport(handle), calls


def control_at(root):
    return json.loads((root / "execution-control.json").read_bytes())


@pytest.mark.asyncio
async def test_five_tasks_start_together_and_keep_state_and_counters_separate(tmp_path):
    root, profile = setup_campaign(tmp_path)
    arrived, release = set(), asyncio.Event()

    def transport(index):
        script, count = phases(index, extra=index), 0

        async def handle(request):
            nonlocal count
            offset, count = count, count + 1
            if offset == 0:
                arrived.add(index)
                if len(arrived) == 5:
                    release.set()
                await asyncio.wait_for(release.wait(), 15)
            await asyncio.sleep(0)
            return response(request, index=offset, **script[offset])

        return lambda: httpx.MockTransport(handle)

    rows = await asyncio.gather(*(backend.run_one_current_multiturn(f"multi_turn_base_{i}", root,
        _profile=profile, _test_transport_factory=transport(i)) for i in (4, 2, 0, 3, 1)))
    assert arrived == set(range(5))
    assert all(row["verification_result"] == "pass" and row["protocol_result"] == "pass" for row in rows)
    assert len({row["session_id"] for row in rows}) == 5
    for row in rows:
        index = int(row["id"].rsplit("_", 1)[1])
        assert row["http_attempts"] == row["budget"]["http_transport_attempts"] == 4 + index
        assert row["model_api_calls"] == 0 and len(row["runs"]) == 2
        assert row["result"] == "unsupported"  # Mock predictions never become capability scores.
    report = backend.finalize_campaign(root)
    assert report["http_transport_attempts"] == 30 and report["model_api_calls"] == 0
    assert report["fatal"] is None and report["concurrent_tasks"]["finalized"] is True
    assert set(report["concurrent_tasks"]["states"].values()) == {"completed"}
    assert backend.read_task_receipt(root, rows[0]["id"]) == rows[0]


@pytest.mark.asyncio
async def test_duplicate_writer_and_early_finalize_do_not_replay_or_close_active_tasks(tmp_path):
    root, profile = setup_campaign(tmp_path, count=1)
    entered, release = asyncio.Event(), asyncio.Event()
    factory, requests = factory_for(0)
    actual = factory()

    async def hold(request):
        if not entered.is_set():
            entered.set()
            await release.wait()
        return await actual.handle_async_request(request)

    make = lambda: httpx.MockTransport(hold)
    task = asyncio.create_task(backend.run_one_current_multiturn("multi_turn_base_0", root,
        _profile=profile, _test_transport_factory=make))
    await asyncio.wait_for(entered.wait(), 15)
    try:
        with pytest.raises(TimeoutError):
            await backend.run_one_current_multiturn("multi_turn_base_0", root,
                _profile=profile, _test_transport_factory=make)
        with pytest.raises(ValueError, match="Active tasks"):
            backend.finalize_campaign(root)
        assert control_at(root)["concurrent_tasks"]["finalized"] is False
        assert len(list((root / "results/receipts").glob("*.json"))) == 0
    finally:
        release.set()
    row = await task
    assert row["verification_result"] == "pass" and len(requests) == 4

    def prohibited():
        raise AssertionError("A completed task must only read its receipt")

    assert await backend.run_one_current_multiturn("multi_turn_base_0", root,
        _profile=profile, _test_transport_factory=prohibited) == row
    assert len(requests) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("incomplete", [False, True])
async def test_global_unknown_stop_survives_another_workers_later_commit(tmp_path, incomplete):
    root, profile = setup_campaign(tmp_path, count=3)
    peer_entered, release_peer = asyncio.Event(), asyncio.Event()

    async def broken(request):
        await peer_entered.wait()
        raise httpx.ReadError("SIMULATED_SECRET_MUST_NOT_APPEAR_IN_RESULTS", request=request)

    async def delayed(request):
        peer_entered.set()
        await release_peer.wait()
        if incomplete:
            event = {"type": "response.incomplete", "response": {"status": "incomplete", "output": [
                {"type": "function_call", "call_id": "partial", "name": "touch", "arguments": '{"file_name":'}]}}
            return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"},
                text="event: response.incomplete\ndata: " + json.dumps(event) + "\n\n")
        return response(request, **phases(1)[0])

    a = asyncio.create_task(backend.run_one_current_multiturn("multi_turn_base_0", root,
        _profile=profile, _test_transport_factory=lambda: httpx.MockTransport(broken)))
    b = asyncio.create_task(backend.run_one_current_multiturn("multi_turn_base_1", root,
        _profile=profile, _test_transport_factory=lambda: httpx.MockTransport(delayed)))
    try:
        first = await asyncio.wait_for(a, 15)
    finally:
        release_peer.set()
    second = await asyncio.wait_for(b, 15)
    assert first["status"] == "case_error" and first["http_attempts"] == 1
    assert second["status"] == "case_error" and second["http_attempts"] == 1
    if incomplete:
        assert second["task_local_provider_incomplete"] is True and not second["tool_ledger"]
    else:
        assert second["termination_kind"] == "campaign_halted"
    assert control_at(root)["fatal"] == "unknown_provider_request_outcome"

    def prohibited():
        raise AssertionError("No transport may be created after a global stop")

    last = await backend.run_one_current_multiturn("multi_turn_base_2", root,
        _profile=profile, _test_transport_factory=prohibited)
    assert last["status"] == "not_run" and last["http_attempts"] == 0
    report = backend.finalize_campaign(root)
    assert report["http_transport_attempts"] == 2
    assert report["fatal"] == "unknown_provider_request_outcome"
    assert "SIMULATED_SECRET" not in json.dumps(report)


@pytest.mark.asyncio
async def test_concurrency_limit_refuses_only_unadmitted_tasks_and_keeps_them_pending(tmp_path):
    root, profile = setup_campaign(tmp_path, count=2, maximum=1)
    entered, release = asyncio.Event(), asyncio.Event()
    factory, _ = factory_for(0)
    actual = factory()

    async def hold(request):
        if not entered.is_set():
            entered.set()
            await release.wait()
        return await actual.handle_async_request(request)

    task = asyncio.create_task(backend.run_one_current_multiturn("multi_turn_base_0", root,
        _profile=profile, _test_transport_factory=lambda: httpx.MockTransport(hold)))
    await asyncio.wait_for(entered.wait(), 15)
    try:
        other_factory, other_calls = factory_for(1)
        with pytest.raises(ValueError, match="worker limit"):
            await backend.run_one_current_multiturn("multi_turn_base_1", root,
                _profile=profile, _test_transport_factory=other_factory)
        assert other_calls == []
        assert control_at(root)["concurrent_tasks"]["states"]["multi_turn_base_1"] == "pending"
    finally:
        release.set()
    await task
    row = await backend.run_one_current_multiturn("multi_turn_base_1", root,
        _profile=profile, _test_transport_factory=other_factory)
    assert row["verification_result"] == "pass" and len(other_calls) == 4


@pytest.mark.asyncio
async def test_cancelled_task_preserves_unknown_intent_and_stops_following_work(tmp_path):
    root, profile = setup_campaign(tmp_path, count=2)
    entered = asyncio.Event()

    async def hold(request):
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(backend.run_one_current_multiturn("multi_turn_base_0", root,
        _profile=profile, _test_transport_factory=lambda: httpx.MockTransport(hold)))
    await asyncio.wait_for(entered.wait(), 15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert control_at(root)["fatal"] == "interrupted_task_outcome"
    factory, calls = factory_for(0)
    with pytest.raises(ValueError, match="unknown prior"):
        await backend.run_one_current_multiturn("multi_turn_base_0", root,
            _profile=profile, _test_transport_factory=factory)
    assert calls == []
    report = backend.finalize_campaign(root)
    assert report["http_transport_attempts"] == 1
    assert [row["result"] for row in report["results"]] == ["case_error", "not_run"]


@pytest.mark.asyncio
async def test_unknown_task_or_backend_mode_is_rejected_before_creating_task_locks(tmp_path):
    root, profile = setup_campaign(tmp_path, count=1)
    factory, calls = factory_for(0)
    with pytest.raises(ValueError, match="outside"):
        await backend.run_one_current_multiturn("multi_turn_base_999", root,
            _profile=profile, _test_transport_factory=factory)
    assert not (root / "results/task-locks").exists() and calls == []
    serial_root = tmp_path / "serial-only"
    backend.serial.initialize_campaign([case_at(0)], serial_root, profile=profile, _test_only=True)
    with pytest.raises(ValueError, match="policy"):
        await backend.run_one_current_multiturn("multi_turn_base_0", serial_root,
            _profile=profile, _test_transport_factory=factory)
    assert not (serial_root / "results/task-locks").exists()


@pytest.mark.asyncio
async def test_data_conflict_keeps_null_quality_and_never_creates_transport(tmp_path):
    source = source_at(tmp_path)
    profile = capture_current_profile(source, model="deepseek-flash", reasoning_effort="none")
    case = case_at(0)
    case["expected"][0][0] = "touch(file_name=12)"
    root = tmp_path / "campaign"
    backend.initialize_campaign([case], root, profile=profile, _test_only=True)

    def prohibited():
        raise AssertionError("A data conflict cannot create a transport")

    row = await backend.run_one_current_multiturn(case["id"], root,
        _profile=profile, _test_transport_factory=prohibited)
    assert row["status"] == "blocked_dataset_conflict" and row["http_attempts"] == 0
    assert row["model_quality"] == "unsupported" and row["termination_kind"] == "dataset_conflict"


@pytest.mark.asyncio
async def test_official_state_failure_does_not_stop_independent_tasks(tmp_path):
    root, profile = setup_campaign(tmp_path, count=2)
    failed = await backend.run_one_current_multiturn("multi_turn_base_0", root, _profile=profile,
        _test_transport_factory=lambda: httpx.MockTransport(lambda request: response(request, text="No action.")))
    assert failed["verification_result"] == "fail" and failed["protocol_result"] == "pass"
    assert "status" not in failed and len(failed["runs"]) == 2
    factory, calls = factory_for(1)
    good = await backend.run_one_current_multiturn("multi_turn_base_1", root,
        _profile=profile, _test_transport_factory=factory)
    assert good["verification_result"] == "pass" and len(calls) == 4
    assert backend.finalize_campaign(root)["fatal"] is None


def test_paid_mode_requires_concurrency_in_plan_before_initialization(tmp_path):
    root = tmp_path / "campaign"
    root.mkdir()
    (root / "campaign-plan.json").write_text("{}", encoding="utf-8")
    profile = capture_current_profile(source_at(tmp_path), model="deepseek-flash", reasoning_effort="none")
    with pytest.raises(ValueError, match="pinned"):
        backend.initialize_campaign([case_at(0)], root, profile=profile, maximum_concurrency=5)
    assert not (root / "execution-control.json").exists()
    for maximum in (0, -1, True, 1.5):
        with pytest.raises(ValueError, match="positive integer"):
            backend.initialize_campaign([case_at(0)], tmp_path / "invalid", profile=profile,
                maximum_concurrency=maximum, _test_only=True)


def test_real_admission_contract_creates_only_an_empty_ledger(tmp_path):
    profile = capture_current_profile(source_at(tmp_path), model="deepseek-flash", reasoning_effort="none")
    root = tmp_path / "campaign"
    root.mkdir()
    case = case_at(0)
    plan = {"backend_mode": backend.MODE, "maximum_concurrency": 5,
        "groups": [{"entry_ids": [case["id"]]}], "authorized_unlimited_api": True,
        "api_unlimited": True, "authorized_api_call_limit": None, "max_output_tokens": 8192,
        "inherited_runtime_safety": backend.serial.INHERITED_RUNTIME_SAFETY,
        "case_deadline_seconds": backend.serial.CASE_DEADLINE_SECONDS,
        "case_tool_safety_limit": backend.serial.CASE_TOOL_SAFETY_LIMIT}
    (root / "campaign-plan.json").write_text(json.dumps(plan), encoding="utf-8")
    manifest = {"files": [{"file": BACKEND_PATH.resolve().relative_to(PROJECT).as_posix(),
        "sha256": hashlib.sha256(BACKEND_PATH.read_bytes()).hexdigest()}]}
    (root / "source-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    control = backend.initialize_campaign([case], root, profile=profile, maximum_concurrency=5)
    assert control["test_only"] is False and control["concurrent_tasks"]["maximum"] == 5
    assert json.loads((root / "results/http-budget.json").read_bytes())["calls"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", [
    "test_complete_task_can_use_more_than_twenty_actual_requests_with_thinking_off",
    "test_official_twenty_steps_applies_per_user_turn_not_whole_task",
    "test_incomplete_http200_is_one_attempt_no_partial_tool_then_next_task_runs",
])
async def test_existing_protocol_scenarios_also_hold_for_concurrent_backend(tmp_path, monkeypatch, scenario):
    import test_bfcl_multiturn_unmetered as scenarios

    for name in ("initialize_campaign", "run_one_current_multiturn", "read_task_receipt", "finalize_campaign"):
        monkeypatch.setattr(scenarios, name, getattr(backend, name))
    await getattr(scenarios, scenario)(tmp_path)
