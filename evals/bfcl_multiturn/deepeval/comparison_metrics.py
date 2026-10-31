"""Deterministic DeepEval metrics, activated only by the isolated offline worker."""
from __future__ import annotations

import os

from aster.evaluation.bfcl_multiturn_export import validate_record
from evals.bfcl_multiturn.deepeval.comparison_data import auxiliary_turn_counts

if any(os.environ.get(key) != "1" for key in (
    "DEEPEVAL_TELEMETRY_OPT_OUT", "DEEPEVAL_DISABLE_DOTENV", "DEEPEVAL_DISABLE_LEGACY_KEYFILE",
)):
    raise RuntimeError("Use the isolated offline comparison launcher")

from deepeval.metrics import BaseConversationalMetric

AUXILIARY_NAMES = {
    "no_premature_calls": "Auxiliary: empty-reference turn has no premature calls",
    "post_blocked_continuation": "Auxiliary: later nonempty-turn official checks",
    "nonempty_turn_checks": "Auxiliary: nonempty-turn official check consistency",
}


class BFCLDeterministicAuxiliaryMetric(BaseConversationalMetric):
    """A per-task fraction of eligible observed turns; never weights the main score."""

    def __init__(self, kind: str):
        if kind not in AUXILIARY_NAMES:
            raise ValueError("Unknown deterministic auxiliary metric")
        self.kind = kind
        self.threshold = 1.0
        self.async_mode = False
        self.strict_mode = False
        self.verbose_mode = False
        self.include_reason = True
        self.evaluation_model = "deterministic:saved-bfcl-turn-checks"
        self.evaluation_cost = 0.0
        self.model = None
        self.using_native_model = False

    def measure(self, test_case, *args, **kwargs) -> float:
        metadata = test_case.metadata or {}
        record = metadata.get("aster_bfcl_record")
        validate_record(record)
        passed, eligible = auxiliary_turn_counts(record, metadata["bfcl_aux_reference_call_counts"], self.kind)
        if not eligible:
            raise ValueError("Inapplicable auxiliary cases must be excluded, not scored")
        self.score = passed / eligible
        self.success = self.score >= self.threshold
        self.error = None
        self.reason = f"{passed}/{eligible} eligible saved turns satisfy this deterministic rule"
        self.score_breakdown = {"passed_turns": passed, "eligible_turns": eligible,
                                "main_score_changed": False, "semantic_judge_enabled": False}
        return self.score

    async def a_measure(self, test_case, *args, **kwargs) -> float:
        return self.measure(test_case, *args, **kwargs)

    @property
    def __name__(self):
        return AUXILIARY_NAMES[self.kind]


def evaluate_metric(conversations, metric, *, identifier: str):
    """Real DeepEval evaluate with no cloud/local-store finalization or model."""
    from deepeval import evaluate
    from deepeval.evaluate.configs import AsyncConfig, CacheConfig, DisplayConfig, ErrorConfig
    from deepeval.test_run import global_test_run_manager

    if not conversations:
        return None
    global_test_run_manager.reset()
    global_test_run_manager.create_test_run(identifier=identifier, disable_request=True)
    result = evaluate(
        test_cases=conversations, metrics=[metric], identifier=identifier, _skip_reset=True,
        async_config=AsyncConfig(run_async=False, max_concurrent=1),
        cache_config=CacheConfig(write_cache=False, use_cache=False),
        display_config=DisplayConfig(show_indicator=False, print_results=False, inspect_after_run=False),
        error_config=ErrorConfig(ignore_errors=False, skip_on_missing_params=False),
    )
    if result.confident_link is not None or result.test_run_id is not None:
        raise RuntimeError("Offline evaluation unexpectedly produced a cloud identity")
    return result
