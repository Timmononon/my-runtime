import asyncio
import copy
import json

import httpx
import pytest
from test_bfcl_current import source_at
from test_bfcl_v4_tools import filesystem_case

from aster.evaluation.bfcl_current import MAX_API_CALLS, capture_current_profile
from aster.evaluation.bfcl_v4_live import V4HTTPCallBudget, run_current_v4_cases
from aster.evaluation.bfcl_v4_runner import OfflineLimits, run_case_offline
from aster.providers.deepseek import ProviderError


def response(request, *, calls=None, text=None, index=0):
    outputs = [{"type": "function_call", "call_id": f"mock-{index}-{number}", "name": name,
                "arguments": json.dumps(arguments)} for number, (name, arguments) in enumerate(calls or [])]
    if text is not None:
        outputs.append({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]})
    event = {"type": "response.completed", "response": {"status": "completed", "output": outputs,
              "usage": {"input_tokens": 30, "output_tokens": 7}}}
    return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"},
                          text="event: response.completed\ndata: " + json.dumps(event) + "\n\n")


@pytest.mark.asyncio
async def test_v4_fifty_attempt_cap_is_independent_of_old_forty_cap(tmp_path):
    profile = capture_current_profile(source_at(tmp_path))
    assert MAX_API_CALLS == 40
    budget = V4HTTPCallBudget(tmp_path / "budget.json", maximum=50, binding="fifty")
    results = await asyncio.gather(*(budget.before_request(httpx.Request("POST", profile.base_url + "/responses"),
        profile=profile, case_id=f"case-{index}") for index in range(51)), return_exceptions=True)
    assert sum(isinstance(result, ProviderError) for result in results) == 1
    assert len(budget.snapshot()["calls"]) == 50
    assert MAX_API_CALLS == 40
    with pytest.raises(ValueError):
        V4HTTPCallBudget(tmp_path / "budget.json", maximum=50, binding="fifty")
    for maximum in (None, 0, 51, -1, True, 2.5):
        with pytest.raises(ValueError):
            V4HTTPCallBudget(tmp_path / "invalid.json", maximum=maximum, binding="invalid")


@pytest.mark.asyncio
async def test_group_caps_are_atomic_and_unknown_operations_do_not_spend_budget(tmp_path):
    profile = capture_current_profile(source_at(tmp_path))
    budget = V4HTTPCallBudget(tmp_path / "budget.json", maximum=5, binding="groups",
                              case_caps={"memory": 2, "other": 3},
                              case_groups={"prereq": "memory", "assessment": "memory", "other": "other"})
    attempts = await asyncio.gather(*(budget.before_request(httpx.Request("POST", profile.base_url + "/responses"),
        profile=profile, case_id=case) for case in ("prereq", "assessment", "assessment", "other")), return_exceptions=True)
    assert sum(isinstance(result, ProviderError) for result in attempts) == 1
    assert len(budget.snapshot()["calls"]) == 3 and budget.remaining_group("assessment") == 0
    for method, url in (("GET", profile.base_url + "/models"), ("POST", "https://other.example/responses")):
        with pytest.raises(ProviderError):
            await budget.before_request(httpx.Request(method, url), profile=profile, case_id="other")
    assert len(budget.snapshot()["calls"]) == 3
    with pytest.raises(ValueError):
        V4HTTPCallBudget(tmp_path / "budget.json", maximum=5, binding="groups", case_caps={"memory": 3})


@pytest.mark.asyncio
async def test_mock_multistep_and_multiuser_turns_count_real_transport_attempts_but_not_model_quality(tmp_path):
    source = source_at(tmp_path)
    profile = capture_current_profile(source)
    case = filesystem_case()
    case["id"] = "multi_turn_base_live_mock"
    original = copy.deepcopy(case)
    calls = []
    phases = [
        {"calls": [("touch", {"file_name": "memo.txt"}), ("echo", {"content": "alpha", "file_name": "memo.txt"})]},
        {"text": "Created."}, {"calls": [("cat", {"file_name": "memo.txt"})]}, {"text": "alpha"},
    ]

    def handle(request):
        body = json.loads(request.content)
        assert request.method == "POST" and request.url.path == "/responses"
        assert body["model"] == "deepseek-flash" and body["reasoning"] == {"effort": "low"}
        assert "ground_truth" not in request.content.decode()
        index = len(calls)
        calls.append(True)
        return response(request, index=index, **phases[index])

    report = await run_current_v4_cases([case], tmp_path / "run", profile=profile, max_api_calls=4,
        case_api_caps={case["id"]: 4}, _test_transport_factory=lambda: httpx.MockTransport(handle))
    assert len(calls) == report["http_transport_attempts"] == 4
    assert report["mode"] == "test_transport" and report["model_quality"] == "unsupported"
    assert report["model_api_calls"] == 0 and report["metadata_requests"] == 0
    result = report["results"][0]
    assert result["scoring_verification"]["valid"] and result["protocol_result"] == "pass"
    assert result["result"] == "unsupported" and result["model_quality"] == "unsupported"
    assert result["budget"]["counts"] == {"requests": 4, "tool_calls": 3}
    assert result["budget"]["model_api_calls"] == 0 and result["budget"]["http_transport_attempts"] == 4
    assert len(result["runs"]) == 2 and len(result["tool_ledger"]) == 3
    assert result["usage"] and all(row["input_tokens"] == 30 for row in result["usage"])
    persisted_protocol = json.loads((tmp_path / "run/protocol-budget.json").read_text(encoding="utf-8"))
    persisted_http = json.loads((tmp_path / "run/http-budget.json").read_text(encoding="utf-8"))
    assert persisted_protocol["http_transport_attempts"] == len(persisted_http["calls"]) == 4
    assert persisted_protocol["model_api_calls"] == 0
    assert case == original
    assert "fake-current-key" not in json.dumps(report)
    assert len(list((tmp_path / "run/receipts").glob("*.json"))) == 1
    with pytest.raises(ValueError, match="cannot be resumed"):
        await run_current_v4_cases([case], tmp_path / "run", profile=profile, max_api_calls=4,
                                  _test_transport_factory=lambda: httpx.MockTransport(handle))
    assert len(calls) == 4


@pytest.mark.asyncio
async def test_a_case_cap_does_not_stop_other_independent_cases(tmp_path):
    profile = capture_current_profile(source_at(tmp_path))
    first = filesystem_case()
    first.update(id="multi_turn_first", question=[[{"role": "user", "content": "Echo alpha."}]],
                 expected=[["echo(content='alpha')"]])
    second = filesystem_case()
    second.update(id="multi_turn_second", question=[[{"role": "user", "content": "Use no tools."}]], expected=[[]])
    calls = []

    def handle(request):
        calls.append(True)
        return response(request, calls=[("echo", {"content": "alpha"})]) if len(calls) == 1 else response(request, text="No tools needed.")

    report = await run_current_v4_cases([first, second], tmp_path / "caps", profile=profile, max_api_calls=2,
        case_api_caps={first["id"]: 1, second["id"]: 1}, _test_transport_factory=lambda: httpx.MockTransport(handle))
    assert len(calls) == 2 and report["http_transport_attempts"] == 2
    assert [row["verification_result"] for row in report["results"]] == ["fail", "pass"]
    assert report["logical_budget"]["counts"]["requests"] == 2
    diagnostic = report["results"][0]["runs"][0]["runtime_diagnostic"]
    assert diagnostic["reason"] == "request_budget"
    assert "message" not in diagnostic


@pytest.mark.asyncio
async def test_authentication_failure_is_sanitized_and_stops_further_dispatch(tmp_path):
    profile = capture_current_profile(source_at(tmp_path))
    cases = [filesystem_case(), filesystem_case()]
    cases[1]["id"] = "multi_turn_second"
    calls = []

    def handle(request):
        calls.append(True)
        return httpx.Response(401, json={"error": {"message": "fake-current-key private provider body"}})

    report = await run_current_v4_cases(cases, tmp_path / "auth", profile=profile, max_api_calls=5,
        _test_transport_factory=lambda: httpx.MockTransport(handle))
    assert len(calls) == 1 and report["http_transport_attempts"] == 1
    assert report["results"][0]["verification_result"] == "case_error"
    assert report["results"][1]["status"] == "not_run"
    assert "fake-current-key" not in json.dumps(report) and "private provider body" not in json.dumps(report)
    assert report["results"][0]["model_failures"][0]["status_code"] == 401


@pytest.mark.asyncio
async def test_dataset_schema_conflicts_and_bad_budget_fail_without_model_dispatch(tmp_path):
    profile = capture_current_profile(source_at(tmp_path))
    case = filesystem_case()
    case.update(question=[[{"role": "user", "content": "Echo alpha."}]], expected=[["echo(content='alpha')"]])
    next(function for function in case["functions"] if function["name"] == "echo")["parameters"]["properties"]["content"] = {
        "type": "integer"}

    def prohibited(request):
        raise AssertionError("Blocked source references must not dispatch an API request")

    report = await run_current_v4_cases([case], tmp_path / "conflict", profile=profile, max_api_calls=1,
        _test_transport_factory=lambda: httpx.MockTransport(prohibited))
    assert report["http_transport_attempts"] == 0 and report["results"][0]["result"] == "blocked_dataset_conflict"
    assert report["logical_budget"]["counts"]["requests"] == 0
    with pytest.raises(ValueError):
        await run_current_v4_cases([case], tmp_path / "invalid", profile=profile, max_api_calls=1,
                                  limits=OfflineLimits(max_requests=2))
    assert not (tmp_path / "invalid").exists()
    with pytest.raises(TypeError):
        await run_case_offline(case, [], tmp_path / "forbidden-offline-driver", model_factory=lambda: None)
