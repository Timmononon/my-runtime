"""User-configured cost estimates. Never a current price list or billing claim."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

import yaml

from aster.domain import Usage


@dataclass(frozen=True)
class CostEstimate:
    status: str
    amount: str | None
    currency: str | None
    pricing_version: str | None
    reason: str | None = None


class PricingCatalog:
    def __init__(self, document: dict | None = None):
        self.document = document or {}
        self.version = self.document.get("version")
        self.currency = self.document.get("currency")
        self.rates: dict[str, dict[str, Decimal]] = {}
        if not self.document:
            return
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("Pricing requires an explicit version")
        if not isinstance(self.currency, str) or len(self.currency) != 3 or not self.currency.isalpha():
            raise ValueError("Pricing requires a three-letter currency code")
        self.currency = self.currency.upper()
        models = self.document.get("models")
        if not isinstance(models, dict):
            raise TypeError("Pricing models must be a mapping")
        for model, configuration in models.items():
            if not isinstance(configuration, dict):
                raise TypeError("Model price must be a mapping")
            rate = configuration.get("per_million", {})
            if set(rate) != {"input_uncached", "input_cached", "output"}:
                raise ValueError(
                    "Model price requires input_uncached, input_cached and output per_million rates"
                )
            try:
                parsed = {key: Decimal(str(value)) for key, value in rate.items()}
            except (InvalidOperation, ValueError) as exc:
                raise ValueError("Price rates must be finite non-negative decimal values") from exc
            if any(not value.is_finite() or value < 0 for value in parsed.values()):
                raise ValueError("Price rates must be finite non-negative decimal values")
            self.rates[model] = parsed

    @classmethod
    def from_yaml(cls, path: Path):
        if not path.exists():
            return cls()
        data = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
        if not isinstance(data, dict):
            raise TypeError("Pricing file must contain a mapping")
        return cls(data)

    def estimate(self, model: str, usage: Usage | dict) -> CostEstimate:
        def unknown(reason):
            return CostEstimate("unknown", None, self.currency, self.version, reason)

        rates = self.rates.get(model)
        if rates is None:
            return unknown("No configured price for this model")
        values = usage.__dict__ if isinstance(usage, Usage) else usage
        input_tokens, output_tokens = values.get("input_tokens"), values.get("output_tokens")
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in (input_tokens, output_tokens)
        ):
            return unknown("Input or output usage is missing or invalid")
        hit, miss = values.get("cache_hit_tokens"), values.get("cache_miss_tokens")
        if miss is not None and (not isinstance(miss, int) or isinstance(miss, bool) or miss < 0):
            return unknown("Cache miss count is invalid")
        if hit is None and miss is not None and isinstance(miss, int):
            hit = input_tokens - miss
        if rates["input_cached"] == rates["input_uncached"]:
            input_cost = Decimal(input_tokens) * rates["input_uncached"]
        elif not isinstance(hit, int) or isinstance(hit, bool) or not 0 <= hit <= input_tokens:
            return unknown("Cache split is required by the configured rates but was not reported")
        else:
            if miss is not None and miss != input_tokens - hit:
                return unknown("Cache counters are inconsistent")
            input_cost = (
                Decimal(hit) * rates["input_cached"] + Decimal(input_tokens - hit) * rates["input_uncached"]
            )
        # DeepSeek output_tokens already includes reasoning_tokens. Never charge
        # reasoning again as a separate output bucket.
        amount = (input_cost + Decimal(output_tokens) * rates["output"]) / Decimal(1_000_000)
        return CostEstimate("estimated", format(amount, "f"), self.currency, self.version)

    def total(self, model: str, usages: list[Usage | dict], *, expected_requests: int | None = None) -> dict:
        estimates = [self.estimate(model, usage) for usage in usages]
        known = sum((Decimal(item.amount) for item in estimates if item.amount is not None), Decimal(0))
        missing = sum(item.status == "unknown" for item in estimates) + max(
            0, (expected_requests or len(usages)) - len(usages)
        )
        return {
            "status": "unknown" if missing or not estimates else "estimated",
            "amount": None if missing or not estimates else format(known, "f"),
            "known_subtotal": format(known, "f"),
            "unknown_requests": missing,
            "currency": self.currency,
            "pricing_version": self.version,
            "requests": len(estimates),
        }
