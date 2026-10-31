import asyncio
import copy
import json

import pytest
from test_bfcl_v4_tools import filesystem_case

from aster.evaluation.bfcl_v4_runner import (
    OfflineCaseBudget,
    OfflineLimits,
    gold_replay_script,
    reference_admission_issues,
    reference_script,
    replay_multiturn,
    run_case_offline,
)


def test_replay_official_state_response_and_irrelevant_turns(tmp_path):
    case = filesystem_case()
    predictions = [[[expression] for expression in turn] for turn in case["expected"]]
    positive = replay_multiturn(case, predictions, root=tmp_path)
    assert positive["valid"] and positive["model_quality"] == "unsupported" and positive["model_api_calls"] == 0
    wrong_state = copy.deepcopy(predictions)
    wrong_state[0][1] = ["echo(content='beta', file_name='memo.txt')"]
    failure = replay_multiturn(case, wrong_state, root=tmp_path)
    assert not failure["valid"] and failure["turns"][0]["error_type"] == "multi_turn:instance_state_mismatch"
    missing_read = copy.deepcopy(predictions)
    missing_read[1] = [["pwd()"]]
    failure = replay_multiturn(case, missing_read, root=tmp_path)
    assert not failure["valid"] and failure["turns"][1]["error_type"] == "multi_turn:execution_response_mismatch"
    irrelevant = filesystem_case()
    irrelevant["expected"][1] = []
    assert not replay_multiturn(irrelevant, predictions, root=tmp_path)["valid"]
    no_prediction = copy.deepcopy(predictions)
    no_prediction[1] = []
    failure = replay_multiturn(case, no_prediction, root=tmp_path)
    assert failure["turns"][1]["error_type"] == "multi_turn:empty_turn_model_response"


def test_cumulative_read_results_allow_an_earlier_necessary_read(tmp_path):
    case = filesystem_case()
    case["expected"][0].append("cat(file_name='memo.txt')")
    predictions = [[[expression] for expression in case["expected"][0]], [["pwd()"]]]
    assert replay_multiturn(case, predictions, root=tmp_path)["valid"]


@pytest.mark.asyncio
async def test_actual_coordinator_multistep_feedback_and_same_session_history(tmp_path, monkeypatch):
    forbidden = tmp_path / "forbidden-user-runtime"
    monkeypatch.setenv("ASTER_DATA_DIR", str(forbidden))

    def fail(*args, **kwargs):
        raise AssertionError("Offline runtime must not construct any model-provider HTTP client")

    monkeypatch.setattr("httpx.AsyncClient", fail)
    case = filesystem_case()
    case["question"][0].insert(0, {"role": "system", "content": "PINNED_EVALUATION_SYSTEM"})
    case["oracle_private_marker"] = "EVALUATOR_ONLY_DO_NOT_EXPOSE"
    report = await run_case_offline(case, gold_replay_script(case), tmp_path / "run")
    assert report["protocol_result"] == "pass" and report["scoring_verification"]["valid"]
    assert report["model_quality"] == "unsupported" and report["model_api_calls"] == 0
    assert report["budget"]["counts"] == {"requests": 5, "tool_calls": 3}
    assert len({entry["run_id"] for entry in report["runs"]}) == 2
    assert len(report["tool_ledger"]) == 3 and all(entry["state"] == "checkpointed" for entry in report["tool_ledger"])
    observations = report["step_observations"]
    assert any(message["role"] == "tool" for message in observations[1]["messages"])
    assert sum(message["role"] == "user" for message in observations[3]["messages"]) == 2
    assert "PINNED_EVALUATION_SYSTEM" in observations[0]["system"]
    assert "EVALUATOR_ONLY_DO_NOT_EXPOSE" not in json.dumps(observations)
    assert not forbidden.exists()
    assert json.loads((tmp_path / "run/result.json").read_text(encoding="utf-8"))["session_id"] == report["session_id"]


@pytest.mark.asyncio
async def test_case_request_budget_is_not_reset_for_second_user_turn(tmp_path):
    case = filesystem_case()
    report = await run_case_offline(case, gold_replay_script(case), tmp_path / "limited",
                                    limits=OfflineLimits(max_requests=3))
    assert report["protocol_result"] == "fail" and not report["scoring_verification"]["valid"]
    assert report["budget"]["counts"]["requests"] == 3
    assert report["budget"]["counts"]["tool_calls"] == 2
    assert report["budget"]["stop_reason"] == "case_requests_exhausted"


@pytest.mark.asyncio
async def test_case_tool_budget_and_deadline_apply_across_all_user_turns(tmp_path):
    case = filesystem_case()
    report = await run_case_offline(case, gold_replay_script(case), tmp_path / "tools-limited",
                                    limits=OfflineLimits(max_tool_calls=2))
    assert report["budget"]["counts"]["tool_calls"] == 2
    assert report["budget"]["stop_reason"] == "case_tool_calls_exhausted"
    assert not report["scoring_verification"]["valid"]
    scripts = gold_replay_script(case)
    scripts[0][0]["delay_seconds"] = .04
    report = await run_case_offline(case, scripts, tmp_path / "deadline",
                                    limits=OfflineLimits(deadline_seconds=.02))
    assert report["budget"]["stop_reason"] == "case_deadline_exhausted"
    assert report["protocol_result"] == "fail"


def test_saved_predictions_cannot_execute_hidden_or_malicious_methods(tmp_path):
    case = filesystem_case()
    case["missed_function"] = {"1": ["touch"]}
    case["question"][1] = []
    report = replay_multiturn(case, [[["touch(file_name='x')"]], []], root=tmp_path)
    assert not report["valid"] and report["turns"][0]["error_type"] == "multi_turn:unoffered_function"
    case["missed_function"] = {}
    case["question"][1] = [{"role": "user", "content": "Read it."}]
    report = replay_multiturn(case, [[["__import__('os').system('echo should-not-run')"]], []], root=tmp_path)
    assert not report["valid"] and report["turns"][0]["error_type"] == "multi_turn:invalid_call_expression"


def test_incomplete_saved_user_turns_are_forced_failure_without_executing(tmp_path):
    case = filesystem_case()
    for decoded in ([], [[]], [[], [], []]):
        report = replay_multiturn(case, decoded, root=tmp_path)
        assert not report["valid"] and report["forced_termination"]
        assert report["error_type"] == "multi_turn:force_terminated"
        assert report["budget"]["counts"] == {"requests": 0, "tool_calls": 0}
    complete = [[[call] for call in turn] for turn in case["expected"]]
    assert not replay_multiturn(case, complete, root=tmp_path, forced_terminated=True)["valid"]
    with pytest.raises(TypeError):
        replay_multiturn(case, complete, root=tmp_path, forced_terminated="false")


@pytest.mark.asyncio
async def test_hidden_function_is_denied_until_its_official_reveal_turn(tmp_path):
    case = filesystem_case()
    case["category"] = "multi_turn_miss_func"
    case["question"] = [[{"role": "user", "content": "Create the memo if your tools permit."}], [],
                        [{"role": "user", "content": "Read the memo."}]]
    case["missed_function"] = {"1": ["touch"]}
    case["expected"] = [[], *case["expected"]]
    scripts = gold_replay_script(case)
    report = await run_case_offline(case, scripts, tmp_path / "revealed")
    assert report["protocol_result"] == "pass" and report["scoring_verification"]["valid"]
    observations = report["step_observations"]
    assert "touch" not in observations[0]["offered_tools"]
    assert "touch" in next(item for item in observations if item["turn_index"] == 1)["offered_tools"]
    scripts[0] = [{"calls": [{"name": "touch", "arguments": {"file_name": "memo.txt"}}]}]
    denied = await run_case_offline(case, scripts, tmp_path / "denied")
    assert denied["protocol_result"] == "fail" and not denied["scoring_verification"]["valid"]
    assert len(denied["simulator_results"]) == 3


@pytest.mark.asyncio
async def test_twenty_step_force_termination_cannot_pass_even_with_matching_final_state(tmp_path):
    case = filesystem_case()
    case["question"] = [[{"role": "user", "content": "Return nineteen."}]]
    case["expected"] = [["echo(content='19')"]]
    scripts = [[{"calls": [{"name": "echo", "arguments": {"content": str(index)}}]} for index in range(20)]]
    report = await run_case_offline(case, scripts, tmp_path / "forced")
    assert report["forced_termination"] and not report["scoring_verification"]["valid"]
    assert report["budget"]["counts"]["requests"] == 20


@pytest.mark.asyncio
async def test_cancel_before_dispatch_and_cancel_during_script_leave_no_simulator_write(tmp_path):
    case = filesystem_case()
    cancelled = asyncio.Event()
    cancelled.set()
    report = await run_case_offline(case, gold_replay_script(case), tmp_path / "pre-cancel", cancel=cancelled)
    assert report["budget"]["counts"] == {"requests": 0, "tool_calls": 0}
    event = asyncio.Event()
    scripts = gold_replay_script(case)
    scripts[0][0]["delay_seconds"] = .5
    timer = asyncio.get_running_loop().call_later(.12, event.set)
    try:
        report = await run_case_offline(case, scripts, tmp_path / "during-cancel", cancel=event)
    finally:
        timer.cancel()
    assert report["budget"]["counts"]["tool_calls"] == 0 and report["simulator_results"] == []
    assert report["protocol_result"] == "fail"


@pytest.mark.asyncio
async def test_evidence_is_not_replayed_and_invalid_scripts_do_not_start_runtime(tmp_path):
    case = filesystem_case()
    output = tmp_path / "saved"
    output.mkdir()
    with pytest.raises(ValueError, match="no replay"):
        await run_case_offline(case, gold_replay_script(case), output)
    with pytest.raises(ValueError):
        await run_case_offline(case, [{"invalid": True}], tmp_path / "invalid")
    assert not (tmp_path / "invalid").exists()


def test_limits_reject_unbounded_or_invalid_values():
    for kwargs in ({"max_steps_per_turn": 21}, {"max_requests": True}, {"max_tool_calls": 0},
                   {"deadline_seconds": float("inf")}, {"deadline_seconds": 0}):
        with pytest.raises(ValueError):
            OfflineLimits(**kwargs)


@pytest.mark.asyncio
async def test_shared_case_budget_preserves_limits_and_allows_answer_after_last_tool(tmp_path):
    limits = OfflineLimits(max_requests=4, max_tool_calls=1)
    budget = OfflineCaseBudget(limits, path=tmp_path / "shared-budget.json")
    first = filesystem_case()
    first["question"] = [[{"role": "user", "content": "Echo alpha."}]]
    first["expected"] = [["echo(content='alpha')"]]
    report = await run_case_offline(first, gold_replay_script(first), tmp_path / "first",
                                    limits=limits, shared_budget=budget)
    assert report["protocol_result"] == "pass" and report["budget"]["counts"] == {"requests": 2, "tool_calls": 1}
    second = filesystem_case()
    second["question"] = [[{"role": "user", "content": "Do not invoke any tools."}]]
    second["expected"] = [[]]
    report = await run_case_offline(second, gold_replay_script(second), tmp_path / "second",
                                    limits=limits, shared_budget=budget)
    assert report["protocol_result"] == "pass"
    assert report["budget"]["counts"] == {"requests": 1, "tool_calls": 0}
    assert report["budget"]["case_counts"] == {"requests": 3, "tool_calls": 1}
    assert json.loads((tmp_path / "shared-budget.json").read_text())["counts"] == budget.counts
    with pytest.raises(ValueError, match="cannot be upgraded"):
        await run_case_offline(second, gold_replay_script(second), tmp_path / "upgraded",
                                limits=OfflineLimits(max_requests=40), shared_budget=budget)
    with pytest.raises(ValueError, match="cannot be rebound"):
        OfflineCaseBudget(limits, path=tmp_path / "shared-budget.json")
    assert not (tmp_path / "upgraded").exists()


@pytest.mark.asyncio
async def test_source_reference_schema_conflict_stays_rejected_and_is_not_a_model_score(tmp_path):
    case = filesystem_case()
    case["question"] = [[{"role": "user", "content": "Echo alpha."}]]
    case["expected"] = [["echo('alpha')"]]
    next(function for function in case["functions"] if function["name"] == "echo")["parameters"]["properties"]["content"] = {
        "type": "integer"}
    issues = reference_admission_issues(case)
    assert len(issues) == 1 and issues[0]["function"] == "echo"
    assert issues[0]["validator"] == "type" and issues[0]["received_type"] == "str"
    report = await run_case_offline(case, reference_script(case), tmp_path / "conflict")
    assert report["validation_expectation"] == "expected_rejection"
    assert report["runtime_admission_status"] == "blocked_dataset_conflict"
    assert report["protocol_result"] == "fail" and not report["scoring_verification"]["valid"]
    assert report["simulator_results"] == [] and report["model_quality"] == "unsupported"


@pytest.mark.asyncio
async def test_offline_reports_cannot_write_into_user_runtime(tmp_path, monkeypatch):
    live = tmp_path / "user-runtime"
    monkeypatch.setenv("ASTER_DATA_DIR", str(live))
    case = filesystem_case()
    with pytest.raises(ValueError, match="outside the user Runtime"):
        await run_case_offline(case, reference_script(case), live / "attempted-output")
    assert not live.exists()
