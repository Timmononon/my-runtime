import asyncio
import copy
import hashlib
import json

import httpx
import pytest
from test_bfcl_current import source_at
from test_bfcl_v4_live import response
from test_bfcl_v4_tools import filesystem_case

from aster.evaluation.bfcl_current import capture_current_profile
from aster.evaluation.bfcl_multiturn_live import (
    MultiTurnHTTPBudget,
    finalize_campaign,
    initialize_campaign,
    read_task_receipt,
    run_one_current_multiturn,
)
from aster.evaluation.bfcl_v4_live import MAX_V4_API_CALLS
from aster.providers.deepseek import ProviderError


def case_at(index=1):
    case = filesystem_case()
    case["id"] = f"multi_turn_base_{index}"
    return case


def setup_campaign(tmp_path, cases=None, maximum=200):
    profile = capture_current_profile(source_at(tmp_path))
    root = tmp_path / "campaign"
    control = initialize_campaign(cases or [case_at()], root, profile=profile,
                                  max_api_calls=maximum, _test_only=True)
    return root, profile, control


@pytest.mark.asyncio
async def test_global_200_is_atomic_across_reopened_budgets_and_keeps_old_50(tmp_path):
    profile = capture_current_profile(source_at(tmp_path))
    path = tmp_path / "http.json"
    ids = [f"case-{i}" for i in range(11)]
    MultiTurnHTTPBudget(path, maximum=200, binding="bound", case_ids=ids, create=True)
    budgets = [MultiTurnHTTPBudget(path, maximum=200, binding="bound", case_ids=ids) for _ in range(3)]
    rows = await asyncio.gather(*(budgets[i % 3].before_request(
        httpx.Request("POST", profile.base_url + "/responses"), profile=profile, case_id=ids[i % 11])
        for i in range(201)), return_exceptions=True)
    assert sum(isinstance(row, ProviderError) for row in rows) == 1
    assert len(budgets[0].snapshot()["calls"]) == 200
    assert MAX_V4_API_CALLS == 50
    with pytest.raises(ValueError):
        MultiTurnHTTPBudget(path, maximum=200, binding="bound", case_ids=ids, create=True)
    with pytest.raises(ValueError):
        MultiTurnHTTPBudget(path, maximum=199, binding="bound", case_ids=ids)
    with pytest.raises(ValueError):
        MultiTurnHTTPBudget(tmp_path / "too-much", maximum=201, binding="bad", case_ids=ids, create=True)


@pytest.mark.asyncio
async def test_exact_endpoint_and_bounded_group_reject_before_transport(tmp_path):
    profile = capture_current_profile(source_at(tmp_path))
    budget = MultiTurnHTTPBudget(tmp_path / "http.json", maximum=200, binding="safe", case_ids=["one"], create=True)
    budget.admit_cap("one", 1)
    for request in (httpx.Request("GET", profile.base_url + "/responses"),
                    httpx.Request("POST", "https://other.invalid/responses")):
        with pytest.raises(ProviderError):
            await budget.before_request(request, profile=profile, case_id="one")
    assert budget.snapshot()["calls"] == []
    await budget.before_request(httpx.Request("POST", profile.base_url + "/responses"), profile=profile, case_id="one")
    with pytest.raises(ProviderError):
        await budget.before_request(httpx.Request("POST", profile.base_url + "/responses"), profile=profile, case_id="one")
    with pytest.raises(ValueError):
        budget.admit_cap("one", 2)


@pytest.mark.asyncio
async def test_complete_two_turn_task_executes_once_and_known_receipt_reads(tmp_path):
    original = case_at()
    root, profile, control = setup_campaign(tmp_path, [original])
    before = copy.deepcopy(original)
    phases = [
        {"calls": [("touch", {"file_name": "memo.txt"}), ("echo", {"content": "alpha", "file_name": "memo.txt"})]},
        {"text": "Done."}, {"calls": [("cat", {"file_name": "memo.txt"})]}, {"text": "alpha"},
    ]
    calls = []

    def handle(request):
        index = len(calls)
        body = json.loads(request.content)
        assert "ground_truth" not in request.content.decode() and "expected" not in body
        calls.append(body)
        return response(request, index=index, **phases[index])

    result = await run_one_current_multiturn(original["id"], root, _profile=profile,
        _test_transport_factory=lambda: httpx.MockTransport(handle))
    assert result["verification_result"] == "pass" and result["result"] == "unsupported"
    assert result["model_api_calls"] == 0 and result["http_attempts"] == 4
    assert len(result["runs"]) == 2 and len(calls) == 4 and original == before
    assert "fake-current-key" not in json.dumps(_read_all_evidence(root))
    repeated = await run_one_current_multiturn(original["id"], root, _profile=profile,
        _test_transport_factory=lambda: httpx.MockTransport(handle))
    assert repeated == result and len(calls) == 4
    summary = finalize_campaign(root)
    assert summary["http_transport_attempts"] == 4 and summary["model_api_calls"] == 0
    assert summary["dataset_binding"] == control["dataset_binding"]
    assert read_task_receipt(root, original["id"]) == result
    with pytest.raises(ValueError):
        initialize_campaign([original], root, profile=profile, _test_only=True)


def _read_all_evidence(root):
    return {str(path.relative_to(root)): json.loads(path.read_text(encoding="utf-8"))
            for path in root.rglob("*.json")}


@pytest.mark.asyncio
async def test_fair_future_reserve_caps_a_loop_and_keeps_next_case_budget(tmp_path):
    root, profile, _ = setup_campaign(tmp_path, [case_at(1), case_at(2)], maximum=12)
    requests = []

    def handle(request):
        requests.append(request)
        return response(request, calls=[("touch", {"file_name": f"loop-{len(requests)}.txt"})], index=len(requests))

    first = await run_one_current_multiturn("multi_turn_base_1", root, _profile=profile,
        _test_transport_factory=lambda: httpx.MockTransport(handle))
    assert first["allocated_api_call_cap"] == 6 and first["http_attempts"] == 6
    assert first["verification_result"] == "fail" and first["model_failures"][-1]["category"] == "budget"
    second = await run_one_current_multiturn("multi_turn_base_2", root, _profile=profile,
        _test_transport_factory=lambda: httpx.MockTransport(handle))
    assert second["allocated_api_call_cap"] == 6 and second["http_attempts"] == 6
    assert len(requests) == 12


@pytest.mark.asyncio
async def test_unknown_intent_prevents_retry_and_finalize_never_generates(tmp_path):
    root, profile, control = setup_campaign(tmp_path)
    suffix = hashlib.sha256(b"multi_turn_base_1").hexdigest()[:24]
    (root / f"results/intents/{suffix}.json").write_text(json.dumps({"status": "unknown"}), encoding="utf-8")
    transport_calls = []

    def factory():
        transport_calls.append(True)
        return httpx.MockTransport(lambda request: response(request, text="should never happen"))

    with pytest.raises(ValueError, match="unknown prior outcome"):
        await run_one_current_multiturn("multi_turn_base_1", root, _profile=profile, _test_transport_factory=factory)
    report = finalize_campaign(root)
    assert report["results"][0]["status"] == "case_error" and not transport_calls
    assert report["dataset_binding"] == control["dataset_binding"]


@pytest.mark.asyncio
async def test_data_conflict_is_unscored_and_uninvoked_tasks_are_not_run(tmp_path):
    conflict = case_at(1)
    conflict["expected"][0][0] = "touch(file_name=12)"
    root, profile, _ = setup_campaign(tmp_path, [conflict, case_at(2)])

    def forbidden():
        raise AssertionError("Data conflict must never create a transport")

    row = await run_one_current_multiturn(conflict["id"], root, _profile=profile, _test_transport_factory=forbidden)
    assert row["status"] == "blocked_dataset_conflict" and row["model_api_calls"] == 0
    assert row["http_attempts"] == 0 and row["allocated_api_call_cap"] == 0
    report = finalize_campaign(root)
    assert report["results"][1]["status"] == "not_run"


@pytest.mark.asyncio
async def test_wrong_order_changed_profile_or_transport_cannot_dispatch(tmp_path):
    root, profile, _ = setup_campaign(tmp_path, [case_at(1), case_at(2)])
    factory = lambda: httpx.MockTransport(lambda request: response(request, text="unused"))
    with pytest.raises(ValueError, match="next serial"):
        await run_one_current_multiturn("multi_turn_base_2", root, _profile=profile, _test_transport_factory=factory)
    with pytest.raises(ValueError, match="interchanged"):
        await run_one_current_multiturn("multi_turn_base_1", root, _profile=profile)
    control = json.loads((root / "execution-control.json").read_text(encoding="utf-8"))
    control["profile_guard"] = "changed"
    (root / "execution-control.json").write_text(json.dumps(control), encoding="utf-8")
    with pytest.raises(ValueError, match="identity changed"):
        await run_one_current_multiturn("multi_turn_base_1", root, _profile=profile, _test_transport_factory=factory)


@pytest.mark.asyncio
async def test_transport_error_consumes_attempt_and_blocks_later_model_calls(tmp_path):
    root, profile, _ = setup_campaign(tmp_path, [case_at(1), case_at(2)])
    calls = []

    def failing(request):
        calls.append(request)
        raise httpx.ReadError("simulated unknown response, no credentials")

    first = await run_one_current_multiturn("multi_turn_base_1", root, _profile=profile,
        _test_transport_factory=lambda: httpx.MockTransport(failing))
    assert first["status"] == "case_error" and first["http_attempts"] == 1
    second = await run_one_current_multiturn("multi_turn_base_2", root, _profile=profile,
        _test_transport_factory=lambda: httpx.MockTransport(failing))
    assert second["status"] == "not_run" and len(calls) == 1


@pytest.mark.asyncio
async def test_known_http200_incomplete_is_task_local_no_retry_and_next_task_runs(tmp_path):
    root, profile, _ = setup_campaign(tmp_path, [case_at(1), case_at(2)])
    calls = []
    phases = [
        {"calls": [("touch", {"file_name": "memo.txt"}), ("echo", {"content": "alpha", "file_name": "memo.txt"})]},
        {"text": "Done."}, {"calls": [("cat", {"file_name": "memo.txt"})]}, {"text": "alpha"},
    ]

    def handle(request):
        index = len(calls)
        calls.append(request)
        if index == 0:
            event = {"type": "response.incomplete", "response": {"status": "incomplete", "output": [
                {"type": "function_call", "call_id": "partial", "name": "touch", "arguments": '{"file_name":'}]}}
            return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"},
                text="event: response.incomplete\ndata: " + json.dumps(event) + "\n\n")
        return response(request, index=index, **phases[index - 1])

    factory = lambda: httpx.MockTransport(handle)
    first = await run_one_current_multiturn("multi_turn_base_1", root, _profile=profile, _test_transport_factory=factory)
    assert first["status"] == "case_error" and first["task_local_provider_incomplete"] is True
    assert first["http_attempts"] == 1 and first["http_status_codes"] == [200]
    assert not first["tool_ledger"] and first["model_failures"][0]["category"] == "incomplete"
    control = json.loads((root / "execution-control.json").read_text(encoding="utf-8"))
    assert control["fatal"] is None
    second = await run_one_current_multiturn("multi_turn_base_2", root, _profile=profile, _test_transport_factory=factory)
    assert second["verification_result"] == "pass" and second["http_attempts"] == 4 and len(calls) == 5
    assert await run_one_current_multiturn("multi_turn_base_1", root, _profile=profile,
                                        _test_transport_factory=factory) == first
    assert len(calls) == 5
