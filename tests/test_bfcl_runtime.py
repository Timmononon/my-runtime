"""Real Coordinator/Dispatcher BFCL checks; all data and models are offline."""
import asyncio
import json
from pathlib import Path

import pytest

from aster.domain import ModelTurn, ToolCall, Usage
from aster.engine import FakeModel
from aster.evaluation.bfcl_runner import (
    BudgetedPredictionModel,
    PredictionBudget,
    _ExplicitCredentials,
    run_bfcl,
)


def corpus(tmp_path, *, count=1):
    data, answers = tmp_path / "data.jsonl", tmp_path / "answers.jsonl"
    functions = [{"name": "weather.lookup", "description": "Find the temperature for a city.",
                  "parameters": {"type": "dict", "properties": {"city": {"type": "string"}}, "required": ["city"]}}]
    data.write_text("\n".join(json.dumps({"id": f"simple_python_{i}", "question": [[{"role": "user", "content": "Temperature in Oslo?"}]],
                                         "function": functions}) for i in range(count)), encoding="utf-8")
    answers.write_text("\n".join(json.dumps({"id": f"simple_python_{i}", "ground_truth": [{"weather.lookup": {"city": ["Oslo"]}}]})
                                  for i in range(count)), encoding="utf-8")
    return data, answers


@pytest.mark.asyncio
async def test_offline_uses_isolated_coordinator_without_credentials_or_quality_claim(tmp_path, monkeypatch):
    data, answers = corpus(tmp_path)
    monkeypatch.setattr("aster.evaluation.bfcl_runner.CredentialStore", lambda **_: pytest.fail("No credentials may be opened"))
    report = await run_bfcl(data, answers, tmp_path / "report.json")
    assert report["totals"] == {"unsupported": 1}
    assert report["paid_calls_authorized"] is False and report["official_score"] is False
    assert report["results"][0]["runtime_status"] == "completed"
    assert report["results"][0]["model_quality"] == "unsupported"
    evidence = json.loads(Path(report["results"][0]["evidence_file"]).read_text())
    assert [message["role"] for message in evidence["messages"]] == ["user", "assistant"]
    assert not Path(evidence["runtime_data_dir"]).exists()
    assert "aster-bfcl-" in evidence["workspace"]
    assert "not an official BFCL" in (tmp_path / "report.md").read_text()


@pytest.mark.asyncio
async def test_first_complete_prediction_reaches_real_dispatch_and_never_reads_gold(tmp_path):
    data, answers = corpus(tmp_path)
    seen = []

    def prediction(messages, tools):
        seen.append({"messages": messages, "tools": tools})
        assert len(tools) == 1 and tools[0].name.startswith("b_")
        return ModelTurn(tool_calls=[ToolCall("prediction-1", tools[0].name, {"city": "Oslo"})],
                         usage=Usage(input_tokens=17, output_tokens=8))

    model = FakeModel([prediction])
    report = await run_bfcl(data, answers, tmp_path / "report.json", model_factory=lambda _: model)
    row = report["results"][0]
    assert row["prediction_check"]["passed"] is True
    assert row["result"] == "unsupported" and row["model_quality"] == "unsupported"
    assert row["runtime_status"] == "budget_exhausted" and row["prediction_boundary_reached"]
    assert len(model.calls) == row["runtime_budget"]["model_requests"] == 1
    assert len(row["tool_outcomes"]) == 1 and row["tool_outcomes"][0]["state"] == "checkpointed"
    assert "ground_truth" not in repr(seen) and "expected" not in repr(seen)
    evidence = json.loads(Path(row["evidence_file"]).read_text())
    tool = next(message for message in evidence["messages"] if message["role"] == "tool")
    assert tool["metadata"]["data"]["function_executed"] is False
    assert "no real function" in tool["content"]


@pytest.mark.asyncio
async def test_extra_denied_attempt_is_still_graded_not_hidden_by_ledger(tmp_path):
    data, answers = corpus(tmp_path)

    def prediction(messages, tools):
        return ModelTurn(tool_calls=[ToolCall("right", tools[0].name, {"city": "Oslo"}),
                                     ToolCall("extra", "send_real_email", {"secret": "not sent"})])

    report = await run_bfcl(data, answers, tmp_path / "report.json", model_factory=lambda _: FakeModel([prediction]))
    row = report["results"][0]
    assert row["prediction_check"]["passed"] is False
    assert "dispatcher_rejected_prediction" in row["prediction_check"]["failures"]
    assert len(row["tool_outcomes"]) == 1


@pytest.mark.asyncio
async def test_incomplete_stream_is_not_a_prediction_and_never_dispatches(tmp_path):
    data, answers = corpus(tmp_path)

    def prediction(messages, tools):
        return ModelTurn(tool_calls=[ToolCall("unfinished", tools[0].name, {"city": "Oslo"})], finish_reason="length")

    report = await run_bfcl(data, answers, tmp_path / "report.json", model_factory=lambda _: FakeModel([prediction]))
    row = report["results"][0]
    assert row["runtime_status"] == "failed" and not row["tool_outcomes"]
    assert row["prediction_check"]["passed"] is False
    assert "no_complete_model_prediction" in row["prediction_check"]["failures"]


@pytest.mark.asyncio
async def test_live_requires_budget_before_credentials_or_dataset_access(tmp_path, monkeypatch):
    monkeypatch.setattr("aster.evaluation.bfcl_runner.CredentialStore", lambda **_: pytest.fail("No key read"))
    with pytest.raises(ValueError, match="explicit request budget"):
        await run_bfcl(tmp_path / "missing-data", tmp_path / "missing-answers", tmp_path / "out.json", mode="live")
    with pytest.raises(ValueError, match="Injected models"):
        await run_bfcl(tmp_path / "missing-data", tmp_path / "missing-answers", tmp_path / "out.json", mode="live",
                       max_requests=1, credential_data_dir=tmp_path, model_factory=lambda _: FakeModel([]))


@pytest.mark.asyncio
async def test_live_rejects_custom_default_and_existing_runtime_before_key_read(tmp_path, monkeypatch):
    default = tmp_path / "custom-home"
    default.mkdir()
    other = tmp_path / "existing-runtime"
    other.mkdir()
    (other / "runtime.db").write_bytes(b"do not open")
    monkeypatch.setattr("aster.evaluation.bfcl_runner.resolve_data_dir", lambda: default)
    monkeypatch.setattr("aster.evaluation.bfcl_runner.CredentialStore", lambda **_: pytest.fail("No key read"))
    for directory in (default, other):
        with pytest.raises(ValueError, match="separate test credential"):
            await run_bfcl(tmp_path / "missing-data", tmp_path / "missing-answers", tmp_path / "out.json", mode="live",
                           max_requests=1, credential_data_dir=directory)


def test_explicit_credentials_never_fall_back_to_ambient_provider_key(tmp_path, monkeypatch):
    from aster.customization.credentials import CredentialStore
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ambient-production-key-do-not-use")
    explicit = _ExplicitCredentials(tmp_path)
    assert explicit.get() is None
    CredentialStore(data_dir=tmp_path).set("deepseek", "explicit-test-key")
    assert explicit.get() == "explicit-test-key"
    with pytest.raises(ValueError, match="explicit file credentials"):
        explicit.resolve("env:DEEPSEEK_API_KEY")


@pytest.mark.asyncio
async def test_report_paths_cannot_overwrite_inputs_or_alias_markdown(tmp_path):
    data, answers = corpus(tmp_path)
    json_data = tmp_path / "dataset.json"
    json_data.write_bytes(data.read_bytes())
    before = data.read_bytes(), answers.read_bytes()
    with pytest.raises(ValueError, match=".json extension"):
        await run_bfcl(data, answers, tmp_path / "report.md")
    with pytest.raises(ValueError, match="overlap"):
        await run_bfcl(json_data, answers, json_data)
    assert before == (data.read_bytes(), answers.read_bytes())


@pytest.mark.asyncio
async def test_duplicate_case_ids_are_rejected_before_dataset_access(tmp_path):
    with pytest.raises(ValueError, match="distinct"):
        await run_bfcl(tmp_path / "missing-data", tmp_path / "missing-answers", tmp_path / "report.json",
                       case_ids=["simple_python_1", "simple_python_1"])


@pytest.mark.asyncio
async def test_outputs_are_not_overwritten_or_replayed(tmp_path):
    data, answers = corpus(tmp_path)
    output = tmp_path / "report.json"
    await run_bfcl(data, answers, output)
    before = output.read_bytes()
    with pytest.raises(ValueError, match="already exists"):
        await run_bfcl(data, answers, output)
    assert output.read_bytes() == before


@pytest.mark.asyncio
async def test_budget_failure_is_reserved_across_restart_without_error_text(tmp_path):
    path = tmp_path / "budget.json"
    budget = PredictionBudget(path, max_requests=1, max_output_tokens=128, binding="fixed")
    model = FakeModel([RuntimeError("private-provider-key-should-not-be-logged")])
    with pytest.raises(RuntimeError):
        await budget.generate(model, "case", [], [], system="")
    data = json.loads(path.read_text())
    assert data["calls"][0]["status"] == "outcome_unknown" and data["calls"][0]["usage"] is None
    assert "private-provider-key" not in path.read_text()
    restarted = PredictionBudget(path, max_requests=1, max_output_tokens=128, binding="fixed")
    with pytest.raises(RuntimeError, match="exhausted"):
        await restarted.generate(model, "case", [], [], system="")
    with pytest.raises(ValueError, match="cannot be reset"):
        PredictionBudget(path, max_requests=2, max_output_tokens=128, binding="fixed")


@pytest.mark.asyncio
async def test_budget_parallel_reservations_and_identity_proxy(tmp_path):
    budget = PredictionBudget(tmp_path / "budget.json", max_requests=2, max_output_tokens=128, binding="fixed")

    class Model:
        model = "pinned-model"

        def __init__(self):
            self.identity = {"protocol": "test", "revision": 3}

        async def generate(self, *args, **kwargs):
            await asyncio.sleep(0)
            return ModelTurn(usage=Usage(input_tokens=7, output_tokens=2))

    proxy = BudgetedPredictionModel(Model(), budget, "case")
    assert proxy.model == "pinned-model" and proxy.identity["revision"] == 3
    await asyncio.gather(proxy.generate([], [], system=""), proxy.generate([], [], system=""))
    data = budget.snapshot()
    assert len(data["calls"]) == 2
    assert all(row["usage"]["input_tokens"] == 7 for row in data["calls"])
    with pytest.raises(RuntimeError, match="exhausted"):
        await proxy.generate([], [], system="")
