"""Auxiliary memory models must never inherit the chat model's price."""
import pytest
import yaml

from aster.domain import Usage
from aster.engine.telemetry import _projection, cost_estimate
from aster.state import Store


def price(rate):
    return {"per_million": {"input_uncached": rate, "input_cached": rate, "output": rate}}


@pytest.fixture
def pricing_run(tmp_path):
    (tmp_path / "prices.yaml").write_text(yaml.safe_dump({"version": "offline-fixture", "currency": "USD",
        "models": {"chat-model": price(1), "extractor-model": price(3), "jev-priced": price(2), "": price(50)}}), encoding="utf-8")
    store = Store(tmp_path / "runtime.db")
    session = store.create_session(tmp_path)
    run, _ = store.create_run(session["id"], config={"_runtime_identity": {"model": "chat-model"}})
    try:
        yield store, run, tmp_path
    finally:
        store.close()


def test_chat_price_alone_cannot_price_jev_or_inflate_known_subtotal(pricing_run):
    store, run, root = pricing_run
    store.add_usage(run["id"], Usage(input_tokens=100, output_tokens=20,
        raw={"purpose": "memory_auto_gate", "provider": "typesafe", "model": "jev-unpriced"}))
    result = cost_estimate(store, run, root)
    assert result["status"] == "unknown" and result["amount"] is None
    assert result["known_subtotal"] == "0" and result["unknown_requests"] == 1
    store.add_usage(run["id"], Usage(input_tokens=10, output_tokens=2, raw={"purpose": "main"}))
    result = cost_estimate(store, run, root)
    assert result["known_subtotal"] == "0.000012" and result["unknown_requests"] == 1


def test_extractor_and_explicit_jev_price_use_their_own_models(pricing_run):
    store, run, root = pricing_run
    store.add_usage(run["id"], Usage(input_tokens=100, output_tokens=20,
        raw={"purpose": "memory_auto_extract", "provider": "deepseek", "model": "extractor-model"}))
    store.add_usage(run["id"], Usage(input_tokens=10, output_tokens=2,
        raw={"purpose": "memory_auto_gate", "provider": "typesafe", "model": "jev-priced"}))
    result = cost_estimate(store, run, root)
    assert result["status"] == "estimated" and result["amount"] == "0.000384"
    assert result["unknown_requests"] == 0


@pytest.mark.parametrize("raw", [
    {"purpose": "memory_auto_gate"},
    {"purpose": "memory_auto_extract"},
    {"purpose": "memory_auto_gate", "provider": "deepseek", "model": "chat-model"},
    {"purpose": "memory_auto_extract", "provider": "typesafe", "model": "chat-model"},
    {"purpose": "memory_auto_gate", "provider": "typesafe", "model": ""},
    {"purpose": "memory_auto_extract", "provider": "deepseek", "model": "bad model\ntext"},
])
def test_missing_or_invalid_auxiliary_identity_stays_unknown(pricing_run, raw):
    store, run, root = pricing_run
    store.add_usage(run["id"], Usage(input_tokens=100, output_tokens=20, raw=raw))
    result = cost_estimate(store, run, root)
    assert result["status"] == "unknown" and result["amount"] is None
    assert result["known_subtotal"] == "0" and result["unknown_requests"] == 1


def test_unfinished_auxiliary_receipt_remains_unknown_and_purpose_projection_is_safe(pricing_run):
    store, run, root = pricing_run
    store.add_usage(run["id"], Usage(raw={"purpose": "memory_auto_gate", "status": "outcome_unknown"}))
    result = cost_estimate(store, run, root)
    assert result["unknown_requests"] == 1 and result["known_subtotal"] == "0"
    for purpose in ("memory_auto_gate", "memory_auto_extract"):
        assert _projection({"purpose": purpose, "source_text": "private", "api_key": "secret"}) == {"purpose": purpose}
