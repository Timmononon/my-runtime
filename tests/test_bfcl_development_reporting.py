"""Pure saved-result labels; these tests never construct a model or campaign."""
from __future__ import annotations

import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("development_reporting_only",
    Path(__file__).resolve().parents[1] / "evals/bfcl_multiturn/promptfoo/run_development.py")
reporting = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reporting)


def test_structured_runtime_budget_or_repeated_no_progress_is_budget_termination():
    for row in (
        {"result": "fail", "termination_kind": "completed", "runs": [{"runtime_status": "budget_exhausted"}]},
        {"result": "fail", "termination_kind": "completed", "model_failures": [{"category": "budget"}]},
        {"result": "fail", "termination_kind": "repeated_no_progress"},
    ):
        assert reporting._classification(row) == "budget_termination"


def test_ordinary_tool_rejection_is_a_task_failure():
    row = {"result": "fail", "termination_kind": "tool_rejected",
        "runs": [{"runtime_status": "completed"}], "model_failures": [{"category": "protocol"}]}
    assert reporting._classification(row) == "task_failure"


def test_infrastructure_error_takes_precedence_over_budget_reasons():
    row = {"result": "case_error", "termination_kind": "repeated_no_progress",
        "runs": [{"runtime_status": "budget_exhausted"}], "model_failures": [{"category": "budget"}]}
    assert reporting._classification(row) == "infrastructure_error"
