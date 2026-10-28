import asyncio
import copy
import json

import pytest

from aster.domain import ModelTurn, ToolContext, Usage
from aster.evaluation.oracle import evaluate, load_dataset
from aster.evaluation.runner import LiveBudget, ProtocolDriver, _files, run_evaluation
from aster.tools.files import build_file_tools


@pytest.mark.asyncio
async def test_all_original_scenarios_have_honest_offline_results(tmp_path):
    report = await run_evaluation(tmp_path / "report.json")
    assert len(report["results"]) == 24
    assert len({r["category"] for r in report["results"]}) == 7
    assert report["totals"] == {"unsupported": 10, "pass": 14}
    assert all(r["model_quality"] == "unsupported" for r in report["results"])
    assert report["paid_calls_authorized"] is False
    result = next(r for r in report["results"] if r["id"] == "recover_outcome_unknown_no_replay")
    assert result["observed"]["calls"] == {"send_email": 1}
    assert result["runtime_status"] == "needs_reconciliation"
    assert result["tool_outcomes"] == [{"name": "send_email", "state": "outcome_unknown"}]
    assert "never a model-quality score" in (tmp_path / "report.md").read_text()


def test_oracle_detects_fabricated_answer_missing_evidence_and_extra_effects():
    task = next(t for t in load_dataset()["tasks"] if t["id"] == "research_single_source")
    observed = {"status": "complete", "answer": "approximately unknown", "calls": {"web_fetch": 2, "send_email": 1},
                "receipts": [], "state": {"files": {"unexpected.txt": "side effect"}, "effects": {}}}
    failures = evaluate(task, observed)
    assert any(f.startswith("answer:missing:") for f in failures)
    assert "answer:forbidden:approximately" in failures
    assert "calls:web_fetch:2" in failures
    assert "unexpected_call:send_email" in failures
    assert "missing_receipt:src-aurora-1" in failures
    assert "state:files_mismatch" in failures


@pytest.mark.asyncio
async def test_protocol_driver_cannot_read_oracle_or_inject_expected_answer():
    task = copy.deepcopy(next(t for t in load_dataset()["tasks"] if t["id"] == "files_write_deliverable"))
    task["oracle"]["answer"] = {"exact": "SECRET_EXPECTED_ANSWER_SHOULD_NOT_APPEAR"}
    driver = ProtocolDriver(task)
    first = await driver.generate([], [], system="")
    assert "oracle" not in driver.task
    assert first.tool_calls[0].arguments["content"] == task["prompt"].split("no trailing newline: ")[1].split(". Then")[0]
    second = await driver.generate([], [], system="")
    assert "SECRET_EXPECTED" not in second.text
    assert "no model answer" in second.text


@pytest.mark.asyncio
async def test_live_budget_is_explicit_persistent_and_shared(tmp_path):
    with pytest.raises(ValueError):
        await run_evaluation(tmp_path / "live.json", mode="live", max_requests=None)
    budget = LiveBudget(tmp_path / "budget.json", max_requests=2, max_output_tokens=512)
    class Model:
        calls = 0
        async def generate(self, *args, **kwargs):
            self.calls += 1
            await asyncio.sleep(0)
            return ModelTurn(text="actual stub transport response", usage=Usage(input_tokens=7, output_tokens=3))
    model = Model()
    await asyncio.gather(budget.generate(model, "a", [], []), budget.generate(model, "b", [], []))
    restarted = LiveBudget(tmp_path / "budget.json", max_requests=2, max_output_tokens=512)
    with pytest.raises(RuntimeError, match="exhausted"):
        await restarted.generate(model, "c", [], [])
    assert model.calls == 2
    assert len(json.loads((tmp_path / "budget.json").read_text())["calls"]) == 2
    with pytest.raises(ValueError, match="cannot be reset"):
        LiveBudget(tmp_path / "budget.json", max_requests=3, max_output_tokens=512)


@pytest.mark.asyncio
async def test_file_oracle_detects_unexpected_hidden_deliverables(tmp_path):
    task = next(t for t in load_dataset()["tasks"] if t["id"] == "files_write_deliverable")
    tool = next(t for t in build_file_tools() if t.spec.name == "file_write")
    context = ToolContext("s", "r", tmp_path, tmp_path / "artifacts", asyncio.Event())
    ledger = []
    for name, content in {"brief.txt": task["oracle"]["state"]["file_changes"]["brief.txt"],
                          ".unexpected.txt": "real extra side effect", ".user.aster.lock": "user deliverable"}.items():
        args = {"path": name, "content": content}
        result = await tool.execute(args, context)
        assert not result.is_error
        ledger.append({"tool_name": "file_write", "arguments": args})
    observed = {"status": "complete", "answer": "", "calls": {"file_write": 1}, "receipts": [],
                "state": {"files": _files(tmp_path, ledger=ledger), "effects": {}}}
    assert set(observed["state"]["files"]) == {"brief.txt", ".unexpected.txt", ".user.aster.lock"}
    assert "state:files_mismatch" in evaluate(task, observed, include_answer=False)
