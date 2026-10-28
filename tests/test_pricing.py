from aster.domain import Usage
from aster.providers.pricing import PricingCatalog


def catalog():
    return PricingCatalog(
        {
            "version": "fixture-v1",
            "currency": "usd",
            "models": {
                "test-model": {"per_million": {"input_uncached": "2", "input_cached": "0.5", "output": "4"}}
            },
        }
    )


def test_configured_decimal_estimate_and_reasoning_not_double_counted():
    estimate = catalog().estimate(
        "test-model",
        Usage(
            input_tokens=1000,
            output_tokens=200,
            cache_hit_tokens=400,
            cache_miss_tokens=600,
            reasoning_tokens=150,
        ),
    )
    assert estimate.amount == "0.0022"
    assert estimate.currency == "USD" and estimate.pricing_version == "fixture-v1"


def test_unknown_usage_cache_price_or_model_does_not_become_zero():
    prices = catalog()
    assert prices.estimate("test-model", Usage()).status == "unknown"
    assert prices.estimate("test-model", Usage(input_tokens=100, output_tokens=10)).amount is None
    assert prices.estimate("other", Usage(input_tokens=1, output_tokens=1)).status == "unknown"
    assert PricingCatalog().estimate("test-model", Usage(input_tokens=1, output_tokens=1)).amount is None
    total = prices.total(
        "test-model", [Usage(input_tokens=1000, output_tokens=200, cache_hit_tokens=400)], expected_requests=2
    )
    assert total["amount"] is None and total["known_subtotal"] == "0.0022"
    assert total["unknown_requests"] == 1
    assert prices.total("test-model", [])["amount"] is None


def test_equal_input_prices_do_not_require_cache_split(tmp_path):
    path = tmp_path / "pricing.yaml"
    path.write_text(
        "version: fixture-v2\ncurrency: CNY\nmodels:\n  x:\n    per_million:\n      input_uncached: '1'\n      input_cached: '1'\n      output: '2'\n",
        encoding="utf-8",
    )
    estimate = PricingCatalog.from_yaml(path).estimate("x", Usage(input_tokens=1000, output_tokens=100))
    assert estimate.amount == "0.0012"
