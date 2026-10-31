"""Owned Windows processes verify real FileLock isolation, with mocked HTTP."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import multiprocessing
import os
from pathlib import Path

import httpx
import pytest
from bfcl_test_prerequisites import require_native
from test_bfcl_multiturn_concurrent import PROJECT, backend, factory_for, phases, setup_campaign
from test_bfcl_v4_live import response

from aster.evaluation.bfcl_current import capture_current_profile

pytestmark = [pytest.mark.bfcl_native, pytest.mark.windows, pytest.mark.skipif(os.name != "nt", reason="Windows process locks")]


def _child_guard():
    path = PROJECT / "artifacts/bfcl/v4-integration-20261029/offline_gate.py"
    spec = importlib.util.spec_from_file_location("concurrent_windows_offline_guard", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    guard = module.OfflineGuard()
    guard.install()
    return guard


def _worker(root_text, source_text, index, barrier, crash):
    guard = _child_guard()
    root = Path(root_text)
    profile = capture_current_profile(source_text, model="deepseek-flash", reasoning_effort="none")
    script, requests = phases(index), []

    async def handle(request):
        offset = len(requests)
        requests.append(True)
        if offset == 0:
            if crash:
                (root / f"owned-worker-crash-{index}.json").write_text(
                    json.dumps({"pid": os.getpid(), "guard": guard.summary()}), encoding="utf-8")
                os._exit(17)  # This owned fixture process only; preserve its unknown HTTP intent.
            await asyncio.to_thread(barrier.wait, 40)
        return response(request, index=offset, **script[offset])

    row = asyncio.run(backend.run_one_current_multiturn(f"multi_turn_base_{index}", root,
        _profile=profile, _test_transport_factory=lambda: httpx.MockTransport(handle)))
    result = {"pid": os.getpid(), "id": row["id"], "http_attempts": row["http_attempts"],
        "verification_result": row["verification_result"], "guard": guard.summary()}
    (root / f"owned-worker-{index}.json").write_text(json.dumps(result), encoding="utf-8")


def _join_owned(processes):
    try:
        for process in processes:
            process.join(timeout=60)
        assert all(not process.is_alive() for process in processes)
    finally:
        # Ownership is the Process handles created by these tests, never a
        # process-name scan or termination of unrelated user workers.
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)


def test_five_windows_processes_execute_one_campaign_without_losing_requests(tmp_path):
    root, profile = setup_campaign(tmp_path, count=5)
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(5)
    processes = [context.Process(target=_worker,
        args=(str(root), str(profile.source), index, barrier, False)) for index in range(5)]
    for process in processes:
        process.start()
    _join_owned(processes)
    assert [process.exitcode for process in processes] == [0] * 5
    workers = [json.loads((root / f"owned-worker-{index}.json").read_bytes()) for index in range(5)]
    assert len({row["pid"] for row in workers}) == 5
    assert all(row["verification_result"] == "pass" and row["http_attempts"] == 4 for row in workers)
    assert all(row["guard"]["offline_guards_passed"] for row in workers)
    assert all(row["guard"]["model_api_calls"] == 0 for row in workers)
    report = backend.finalize_campaign(root)
    assert report["http_transport_attempts"] == 20 and report["model_api_calls"] == 0
    assert len(report["results"]) == 5 and report["fatal"] is None
    assert report["wire_policy_verified_calls"] == 20
    assert set(report["concurrent_tasks"]["states"].values()) == {"completed"}


def test_crashed_windows_writer_is_never_replayed_and_halts_pending_tasks(tmp_path):
    root, profile = setup_campaign(tmp_path, count=2)
    context = multiprocessing.get_context("spawn")
    process = context.Process(target=_worker,
        args=(str(root), str(profile.source), 0, None, True))
    process.start()
    _join_owned([process])
    assert process.exitcode == 17
    assert json.loads((root / "owned-worker-crash-0.json").read_bytes())["guard"]["offline_guards_passed"]
    factory, calls = factory_for(0)
    with pytest.raises(ValueError, match="unknown prior"):
        asyncio.run(backend.run_one_current_multiturn("multi_turn_base_0", root,
            _profile=profile, _test_transport_factory=factory))
    assert calls == []
    row = asyncio.run(backend.run_one_current_multiturn("multi_turn_base_1", root,
        _profile=profile, _test_transport_factory=factory))
    assert row["status"] == "not_run" and row["http_attempts"] == 0 and calls == []
    report = backend.finalize_campaign(root)
    assert report["fatal"] == "unknown_prior_task_outcome"
    assert report["http_transport_attempts"] == 1
    assert [row["result"] for row in report["results"]] == ["case_error", "not_run"]


@pytest.fixture(scope="module", autouse=True)
def _native_prerequisites():
    require_native(promptfoo=False, frozen=False)
