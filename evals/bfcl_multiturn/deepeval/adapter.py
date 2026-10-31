"""Native DeepEval conversations and deterministic projections of BFCL evidence.

No model or judge is constructed. Official state/response checks remain the
authority; this module consumes their saved combined decisions without inventing
individual state/response flags that the original result did not retain.
"""
from __future__ import annotations

import os
from collections.abc import Callable
from copy import deepcopy

from aster.evaluation.bfcl_multiturn_export import validate_record

# Fail before DeepEval's import-time dotenv/telemetry initialization. The public
# entry is a launcher, not an invitation to mutate the live Aster process env.
if any(os.environ.get(key) != "1" for key in (
        "DEEPEVAL_TELEMETRY_OPT_OUT", "DEEPEVAL_DISABLE_DOTENV", "DEEPEVAL_DISABLE_LEGACY_KEYFILE")):
    raise RuntimeError("Use the isolated DeepEval offline launcher")

from deepeval.metrics import BaseConversationalMetric
from deepeval.test_case import ConversationalTestCase, ToolCall, Turn


def record_to_conversation(record: dict) -> ConversationalTestCase:
    """One BFCL task -> one conversation, preserving model/tool steps in order.

    DeepEval Turn accepts only user/assistant roles. Tool responses are projected
    into ToolCall.output and metadata on their preceding assistant step. The
    complete canonical user-turn transcript remains in metadata as well. Future
    user turns never entered by the original run remain empty not_run boundaries;
    they are not fabricated into a conversation.
    """
    validate_record(record)
    turns = []
    for user_turn in record["turns"]:
        messages = user_turn["messages"]
        responses = {message["tool_call_id"]: message["content"]
                     for message in messages if message["role"] == "tool"}
        for index, message in enumerate(messages):
            if message["role"] not in {"user", "assistant"}:
                continue
            tools = []
            for call in message["tool_calls"]:
                parameters = call["arguments"]
                if parameters is not None and not isinstance(parameters, dict):
                    raise ValueError("Canonical tool arguments must be structured objects")
                tools.append(ToolCall(name=call["name"], input_parameters=parameters,
                                      output=responses.get(call["id"])))
            turns.append(Turn(role=message["role"], content=message["content"] or "",
                              tools_called=tools or None,
                              metadata={"bfcl_user_turn_index": user_turn["turn_index"],
                                        "canonical_message_index": index,
                                        "tool_call_ids": [call["id"] for call in message["tool_calls"]]}))
    if not turns:
        raise ValueError("This task has no recorded conversation; it is not scorable")
    return ConversationalTestCase(
        name=record["task_id"], turns=turns,
        tags=[record["config_label"], record["subcategory"], "saved-result-offline"],
        metadata={"aster_bfcl_record": deepcopy(record), "judge_enabled": False,
                  "task_boundary": "complete_original_task", "new_model_api_calls": 0,
                  "new_judge_api_calls": 0, "model_capability_measurement": False},
    )


class BFCLSavedTaskMetric(BaseConversationalMetric):
    """The original complete-task pass/fail, including forced termination.

    It is deliberately stricter than a diagnostic subset of valid tool states.
    Incomplete/infra/data-conflict rows must be excluded by the evaluator and
    remain null in the common export, rather than being converted to zero.
    """

    def __init__(self):
        self.threshold = 1.0
        self.async_mode = False
        self.strict_mode = True
        self.verbose_mode = False
        self.include_reason = True
        self.evaluation_model = "deterministic:saved-official-bfcl-checks"
        self.evaluation_cost = 0.0
        self.model = None
        self.using_native_model = False

    def measure(self, test_case: ConversationalTestCase, *args, **kwargs) -> float:
        record = (test_case.metadata or {}).get("aster_bfcl_record")
        if not isinstance(record, dict):
            raise TypeError("Only validated Aster saved evidence can be measured")
        validate_record(record)
        passed = record["scoring"]["passed"]
        if type(passed) is not bool:
            raise ValueError("Unknown BFCL outcome cannot become a scored DeepEval task")
        self.score = float(passed)
        self.success = passed
        self.error = None
        self.reason = "; ".join([record["outcome"]["category"], *record["outcome"]["reasons"]])
        self.score_breakdown = {
            "recorded_formal_result": record["scoring"]["formal_result"],
            "recorded_turn_checks": record["scoring"]["turn_checks"],
            "diagnostic_turn_checks": record["scoring"].get("diagnostic_turn_checks", []),
            "new_model_api_calls": 0, "new_judge_api_calls": 0,
        }
        return self.score

    async def a_measure(self, test_case: ConversationalTestCase, *args, **kwargs) -> float:
        return self.measure(test_case, *args, **kwargs)

    @property
    def __name__(self):
        return "BFCL saved complete-task result"


def auxiliary_observations(record: dict, *, extensions: dict[str, Callable] | None = None) -> dict:
    """Separate optional deterministic auxiliaries; never affect BFCL success.

    No built-in semantic clarification judge or tool-necessity judge is enabled.
    Trusted local callers may register deterministic functions returning dicts.
    The offline CLI has no plugin/model/judge import option.
    """
    validate_record(record)
    result = {
        "clarification_quality": {"status": "not_implemented", "score": None, "judge_enabled": False},
        "unavailable_tool_exploration": {"status": "not_implemented", "score": None, "judge_enabled": False},
        "observed_tool_calls": sum(len(message["tool_calls"]) for turn in record["turns"]
                                   for message in turn["messages"] if message["role"] == "assistant"),
    }
    for name, measure in (extensions or {}).items():
        if name not in {"clarification_quality", "unavailable_tool_exploration"}:
            raise ValueError("Unknown auxiliary extension")
        result[name] = measure(deepcopy(record))
    return result


def evaluate_records(records: list[dict]) -> tuple[object | None, list[ConversationalTestCase], list[dict]]:
    """Execute the real DeepEval evaluator locally with deterministic metrics."""
    from deepeval import evaluate
    from deepeval.evaluate.configs import AsyncConfig, CacheConfig, DisplayConfig, ErrorConfig
    from deepeval.test_run import global_test_run_manager

    conversations, excluded = [], []
    for record in records:
        validate_record(record)
        if record["scoring"]["passed"] is None:
            excluded.append({"task_id": record["task_id"], "outcome": record["outcome"], "score": None})
            continue
        conversations.append(record_to_conversation(record))
    if not conversations:
        return None, [], excluded
    # Pinned 4.3.2 supports an explicit in-memory manager and _skip_reset. This
    # evaluates all test cases via evaluate/execute_test_cases, then returns
    # before wrap_up_test_run (cloud/local-store finalization). No library patch.
    global_test_run_manager.reset()
    global_test_run_manager.create_test_run(identifier="aster-bfcl-saved-offline", disable_request=True)
    result = evaluate(
        test_cases=conversations, metrics=[BFCLSavedTaskMetric()],
        identifier="aster-bfcl-saved-offline", _skip_reset=True,
        async_config=AsyncConfig(run_async=False, max_concurrent=1),
        cache_config=CacheConfig(write_cache=False, use_cache=False),
        display_config=DisplayConfig(show_indicator=False, print_results=False, inspect_after_run=False),
        error_config=ErrorConfig(ignore_errors=False, skip_on_missing_params=False),
    )
    if result.confident_link is not None or result.test_run_id is not None:
        raise RuntimeError("Offline evaluation must not produce a cloud result")
    return result, conversations, excluded
