"""Actual parallel dispatch/usage timelines and missing-cache/cost semantics."""
import json

import httpx
import yaml

from aster.api import create_app
from aster.domain import ModelTurn, ToolCall, ToolResult, ToolSpec, Usage
from aster.engine import FakeModel
from aster.service import RunCoordinator

SECRET = "private source text never included in diagnostics"
TOKEN = "performance-timeline-token-at-least-thirty-two-characters"


class Reader:
    def __init__(self, name):
        self.spec = ToolSpec(name, "offline reader", {"type": "object", "properties": {"query": {"type": "string"}}},
                             risk="read", concurrency_safe=True, replay_safe=True)

    async def execute(self, arguments, context):
        return ToolResult(SECRET)


async def run_fixture(root, *, missing_cache=False):
    model = FakeModel([
        ModelTurn(tool_calls=[ToolCall("call-a", "reader_a", {"query": SECRET}),
                              ToolCall("call-b", "reader_b", {"query": SECRET})],
                  usage=Usage(input_tokens=100, output_tokens=20, cache_hit_tokens=60, cache_miss_tokens=40)),
        ModelTurn(SECRET, usage=Usage() if missing_cache else Usage(
            input_tokens=50, output_tokens=10, cache_hit_tokens=30, cache_miss_tokens=20))])
    model.model = "pricing-fixture"
    coordinator = RunCoordinator(root, tools=[Reader("reader_a"), Reader("reader_b")], model_factory=lambda _: model)
    run = await coordinator.submit(coordinator.create_session(root)["id"], SECRET)
    assert (await coordinator.wait(run["id"], timeout=2)).status == "completed"
    return coordinator, run


def prices(root):
    (root / "prices.yaml").write_text(yaml.safe_dump({"version": "fixture-v1", "currency": "USD", "models": {
        "pricing-fixture": {"per_million": {"input_uncached": 1, "input_cached": 0.5, "output": 2}}}}), encoding="utf-8")


async def test_http_timeline_pairs_actual_batch_ids_and_cost_cache_counts_without_source(tmp_path):
    prices(tmp_path)
    coordinator, run = await run_fixture(tmp_path)
    try:
        coordinator.store.emit_event(run["session_id"], run["id"], "tool.finished", {
            "name": SECRET, "tool_call_id": SECRET, "category": SECRET, "arguments": {"key": SECRET},
            "duration_ms": {"private": SECRET}, "tool_call_ids": [SECRET, {"private": SECRET}]})
        app = create_app(coordinator, token=TOKEN, manage_lifecycle=False)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost",
                                    headers={"Authorization": "Bearer " + TOKEN}) as client:
            response = await client.get(f"/runs/{run['id']}/performance")
            assert response.status_code == 200 and SECRET not in response.text
            result = response.json()
            batches = result["batches"]
            assert len(batches) == 1 and batches[0]["parallel"] and batches[0]["tool_call_ids"] == ["call-a", "call-b"]
            assert {tool["tool_call_id"] for tool in batches[0]["tools"]} == {"call-a", "call-b"}
            assert all(tool["started_at"] <= tool["finished_at"] and tool["duration_ms"] >= 0 for tool in batches[0]["tools"])
            assert batches[0]["duration_ms"] >= 0
            assert any(item["type"] == "run.phase" for item in result["timeline"])
            assert sum(item["type"] == "model.request" for item in result["timeline"]) == 2
            assert result["cache_summary"] == {"requests": 2, "reported_requests": 2, "unknown_requests": 0,
                                               "cache_hit_tokens": 90, "cache_miss_tokens": 60, "hit_ratio": 0.6}
            cost = result["cost_estimate"]
            assert cost["status"] == "estimated" and cost["amount"] == "0.000165"
            assert cost["currency"] == "USD" and cost["pricing_version"] == "fixture-v1" and cost["unknown_requests"] == 0
            assert (await client.get(f"/runs/{run['id']}")).json()["cost_estimate"] == cost
    finally:
        await coordinator.close()


async def test_missing_cache_remains_null_and_unknown_request_cost_is_not_zero(tmp_path):
    prices(tmp_path)
    coordinator, run = await run_fixture(tmp_path, missing_cache=True)
    try:
        from aster.engine.telemetry import performance
        report = performance(coordinator.store, run["id"], data_dir=tmp_path)
        assert report["cache_summary"]["reported_requests"] == 1
        assert report["cache_summary"]["unknown_requests"] == 1
        assert all(report["cache_summary"][field] is None for field in ("cache_hit_tokens", "cache_miss_tokens", "hit_ratio"))
        assert report["cost_estimate"]["status"] == "unknown" and report["cost_estimate"]["amount"] is None
        assert report["cost_estimate"]["unknown_requests"] == 1
        assert report["cost_estimate"]["known_subtotal"] == "0.00011"
        assert SECRET not in json.dumps(report)
        (tmp_path / "prices.yaml").write_text("version: [invalid pricing source]\n", encoding="utf-8")
        invalid = performance(coordinator.store, run["id"], data_dir=tmp_path)["cost_estimate"]
        assert invalid["status"] == "unknown" and "invalid pricing source" not in json.dumps(invalid)
    finally:
        await coordinator.close()


async def test_terminal_legacy_phase_closes_at_recorded_run_boundary(tmp_path):
    coordinator = RunCoordinator(tmp_path, tools=[])
    try:
        session = coordinator.create_session(tmp_path)
        run, _ = coordinator.store.create_run(session["id"], config={})
        coordinator.store.emit_event(session["id"], run["id"], "run.phase", {"phase": "awaiting_model"})
        coordinator.store.set_run_status(run["id"], "failed")
        with coordinator.store.transaction() as db:
            db.execute("UPDATE runs SET created_at=100,updated_at=125 WHERE id=?", (run["id"],))
            db.execute("UPDATE events SET created_at=110 WHERE run_id=? AND type='run.phase'", (run["id"],))
        from aster.engine.telemetry import performance
        result = performance(coordinator.store, run["id"])
        assert result["phase_seconds"]["awaiting_model"] == 15
    finally:
        await coordinator.close()
