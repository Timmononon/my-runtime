import copy
import hashlib
import json

import httpx
import pytest
from test_bfcl_current import source_at
from test_bfcl_v4_live import response
from test_bfcl_v4_tools import filesystem_case

from aster.evaluation.bfcl_current import capture_current_profile
from aster.evaluation.bfcl_multiturn_live import MAX_MULTITURN_API_CALLS
from aster.evaluation.bfcl_multiturn_unmetered import (
    UnmeteredMultiTurnHTTPBudget,
    _termination_kind,
    finalize_campaign,
    initialize_campaign,
    read_task_receipt,
    run_one_current_multiturn,
)
from aster.providers.deepseek import ProviderError


def case_at(index=1):
    case = filesystem_case()
    case["id"] = f"multi_turn_base_{index}"
    return case


def setup_campaign(tmp_path, cases=None):
    source = source_at(tmp_path)
    before = {p.relative_to(source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in source.rglob("*") if p.is_file()}
    profile = capture_current_profile(source, model="deepseek-flash", reasoning_effort="none")
    root = tmp_path / "campaign"
    control = initialize_campaign(cases or [case_at()], root, profile=profile, _test_only=True)
    after = {p.relative_to(source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in source.rglob("*") if p.is_file()}
    assert before == after
    return root, profile, control


def wire_request(profile, **changes):
    body = {"model": "deepseek-flash", "reasoning": {"effort": "none"}, "max_output_tokens": 8192,
            "input": "PRIVATE_BODY_NOT_FOR_LEDGER"}
    body.update(changes)
    return httpx.Request("POST", profile.base_url + "/responses", json=body,
                         headers={"Authorization": "Bearer NEVER_LOG_THIS_HEADER"})


@pytest.mark.asyncio
async def test_no_global_200_limit_and_safe_wire_witness_survives_reopen(tmp_path):
    profile = capture_current_profile(source_at(tmp_path), model="deepseek-flash", reasoning_effort="none")
    path = tmp_path / "ledger.json"
    bounds = {"task": 400}
    budget = UnmeteredMultiTurnHTTPBudget(path, binding="no-quota", case_step_bounds=bounds, create=True)
    for _ in range(205):
        await budget.before_request(wire_request(profile), profile=profile, case_id="task")
    reopened = UnmeteredMultiTurnHTTPBudget(path, binding="no-quota", case_step_bounds=bounds)
    document = reopened.snapshot()
    assert len(document["calls"]) == 205 and document["maximum"] is None
    assert document["authorized_unlimited_api"] is True and document["case_caps"] == {"task": None}
    assert reopened.remaining_group("task") == 195 and MAX_MULTITURN_API_CALLS == 200
    assert all(row["thinking_disabled_verified"] is True and row["wire_reasoning_effort"] == "none"
               and row["wire_model"] == "deepseek-flash" and row["wire_max_output_tokens"] == 8192
               for row in document["calls"])
    text = path.read_text(encoding="utf-8")
    assert "PRIVATE_BODY_NOT_FOR_LEDGER" not in text and "NEVER_LOG_THIS_HEADER" not in text
    assert "fake-current-key" not in text
    with pytest.raises(ValueError):
        UnmeteredMultiTurnHTTPBudget(path, binding="no-quota", case_step_bounds=bounds, create=True)
    with pytest.raises(ValueError):
        UnmeteredMultiTurnHTTPBudget(path, binding="changed", case_step_bounds=bounds)


@pytest.mark.asyncio
async def test_wire_thinking_model_output_mismatch_rejects_before_request_receipt(tmp_path):
    profile = capture_current_profile(source_at(tmp_path), model="deepseek-flash", reasoning_effort="none")
    budget = UnmeteredMultiTurnHTTPBudget(tmp_path / "ledger.json", binding="off", case_step_bounds={"task": 20}, create=True)
    for changes in ({"reasoning": {"effort": "low"}}, {"reasoning": None}, {"model": "deepseek-pro"},
                    {"max_output_tokens": 2048}, {"max_output_tokens": True}):
        with pytest.raises(ProviderError):
            await budget.before_request(wire_request(profile, **changes), profile=profile, case_id="task")
    for request in (httpx.Request("GET", profile.base_url + "/responses"),
                    httpx.Request("POST", "https://other.invalid/responses")):
        with pytest.raises(ProviderError):
            await budget.before_request(request, profile=profile, case_id="task")
    assert budget.snapshot()["calls"] == []


@pytest.mark.asyncio
async def test_complete_task_can_use_more_than_twenty_actual_requests_with_thinking_off(tmp_path):
    original = case_at()
    root, profile, control = setup_campaign(tmp_path, [original])
    before = copy.deepcopy(original)
    phases = [{"calls": [("touch", {"file_name": "memo.txt"})]}]
    phases += [{"calls": [("echo", {"file_name": "memo.txt", "content": f"first-{i}"})]} for i in range(9)]
    phases += [{"calls": [("echo", {"file_name": "memo.txt", "content": "alpha"})]}, {"text": "Created alpha."}]
    phases += [{"calls": [("echo", {"file_name": "memo.txt", "content": f"second-{i}"})]} for i in range(9)]
    phases += [{"calls": [("echo", {"file_name": "memo.txt", "content": "alpha"})]},
               {"calls": [("cat", {"file_name": "memo.txt"})]}, {"text": "alpha"}]
    calls = []

    def handle(request):
        index = len(calls)
        body = json.loads(request.content)
        assert body["model"] == "deepseek-flash" and body["reasoning"] == {"effort": "none"}
        assert body["max_output_tokens"] == 8192 and "ground_truth" not in request.content.decode()
        calls.append(body)
        return response(request, index=index, **phases[index])

    factory = lambda: httpx.MockTransport(handle)
    row = await run_one_current_multiturn(original["id"], root, _profile=profile, _test_transport_factory=factory)
    assert row["http_attempts"] == 24 and len(calls) == 24
    assert row["verification_result"] == "pass"
    assert len(row["runs"]) == 2 and all(run["runtime_status"] == "completed" for run in row["runs"])
    assert row["allocated_api_call_cap"] is None and row["official_model_step_bound"] == 40
    assert row["model_api_calls"] == 0 and row["model_quality"] == "unsupported"
    assert original == before and control["max_api_calls"] is None
    repeated = await run_one_current_multiturn(original["id"], root, _profile=profile, _test_transport_factory=factory)
    assert repeated == row and len(calls) == 24 and read_task_receipt(root, original["id"]) == row
    report = finalize_campaign(root)
    assert report["http_transport_attempts"] == 24 and report["model_api_calls"] == 0
    assert report["wire_policy_verified_calls"] == 24 and report["api_unlimited"] is True
    assert "fake-current-key" not in json.dumps(report)


@pytest.mark.asyncio
async def test_official_twenty_steps_applies_per_user_turn_not_whole_task(tmp_path):
    root, profile, _ = setup_campaign(tmp_path)
    calls = []

    def handle(request):
        index = len(calls)
        calls.append(request)
        tool = ("touch", {"file_name": "memo.txt"}) if index == 0 else (
            "echo", {"file_name": "memo.txt", "content": f"progress-{index}"})
        return response(request, calls=[tool], index=index)

    row = await run_one_current_multiturn("multi_turn_base_1", root, _profile=profile,
        _test_transport_factory=lambda: httpx.MockTransport(handle))
    assert len(calls) == 20 and row["official_model_step_bound"] == 40
    assert len(row["runs"]) == 1 and row["verification_result"] == "fail"
    assert row["termination_kind"] == "official_per_turn_step_limit"
    assert row["model_failures"][-1]["origin"] == "official_per_turn_steps"


@pytest.mark.asyncio
async def test_unknown_intent_transport_error_and_wrong_order_never_replay(tmp_path):
    root, profile, _ = setup_campaign(tmp_path, [case_at(1), case_at(2)])
    factory = lambda: httpx.MockTransport(lambda request: response(request, text="unused"))
    with pytest.raises(ValueError, match="next serial"):
        await run_one_current_multiturn("multi_turn_base_2", root, _profile=profile, _test_transport_factory=factory)
    suffix = hashlib.sha256(b"multi_turn_base_1").hexdigest()[:24]
    intent = root / f"results/intents/{suffix}.json"
    intent.write_text(json.dumps({"status": "unknown"}), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown prior outcome"):
        await run_one_current_multiturn("multi_turn_base_1", root, _profile=profile, _test_transport_factory=factory)
    report = finalize_campaign(root)
    assert report["results"][0]["status"] == "case_error" and report["results"][1]["status"] == "not_run"
    assert report["http_transport_attempts"] == 0


@pytest.mark.asyncio
async def test_transport_unknown_stops_later_calls_but_known_incomplete_does_not_retry(tmp_path):
    root, profile, _ = setup_campaign(tmp_path, [case_at(1), case_at(2)])
    calls = []

    def broken(request):
        calls.append(request)
        raise httpx.ReadError("simulated unknown outcome")

    first = await run_one_current_multiturn("multi_turn_base_1", root, _profile=profile,
        _test_transport_factory=lambda: httpx.MockTransport(broken))
    second = await run_one_current_multiturn("multi_turn_base_2", root, _profile=profile,
        _test_transport_factory=lambda: httpx.MockTransport(broken))
    assert first["status"] == "case_error" and first["http_attempts"] == 1
    assert second["status"] == "not_run" and len(calls) == 1


@pytest.mark.asyncio
async def test_data_conflict_and_explicit_policy_fail_closed(tmp_path):
    conflict = case_at(1)
    conflict["expected"][0][0] = "touch(file_name=12)"
    root, profile, _ = setup_campaign(tmp_path, [conflict])

    def forbidden():
        raise AssertionError("Data conflicts must never create a model transport")

    row = await run_one_current_multiturn(conflict["id"], root, _profile=profile, _test_transport_factory=forbidden)
    assert row["status"] == "blocked_dataset_conflict" and row["http_attempts"] == 0
    assert row["termination_kind"] == "dataset_conflict"
    for kwargs in ({"max_api_calls": 200}, {"authorized_unlimited_api": False}, {"max_output_tokens": 2048}):
        with pytest.raises(ValueError):
            initialize_campaign([case_at()], tmp_path / "invalid", profile=profile, _test_only=True, **kwargs)


@pytest.mark.asyncio
async def test_incomplete_http200_is_one_attempt_no_partial_tool_then_next_task_runs(tmp_path):
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
    assert first["http_attempts"] == 1 and not first["tool_ledger"]
    second = await run_one_current_multiturn("multi_turn_base_2", root, _profile=profile, _test_transport_factory=factory)
    assert second["verification_result"] == "pass" and second["http_attempts"] == 4 and len(calls) == 5
    assert await run_one_current_multiturn("multi_turn_base_1", root, _profile=profile,
                                        _test_transport_factory=factory) == first
    assert len(calls) == 5


def test_safety_termination_codes_never_claim_api_quota():
    class Budget:
        stopped = None

    assert _termination_kind({"runs": [{"runtime_status": "timed_out"}]}, Budget()) == "safety_deadline"
    assert _termination_kind({"messages": [{"role": "tool", "metadata": {"error_category": "loop_detected"}}]},
                             Budget()) == "repeated_no_progress"
    token = {"runs": [{"runtime_status": "budget_exhausted", "usage": [{"input_tokens": 300000, "output_tokens": 1}]}]}
    assert _termination_kind(token, Budget()) == "token_budget"
    assert _termination_kind({"runs": [{"runtime_status": "budget_exhausted"}]}, Budget()) == "runtime_safety_limit_unclassified"
