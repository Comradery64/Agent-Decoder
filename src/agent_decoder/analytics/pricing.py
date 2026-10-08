from __future__ import annotations

import json
import time as _time
from dataclasses import dataclass, field
from decimal import Decimal

import httpx

from .models import TokenBreakdown

_LITELLM_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)

# API pricing as of 2026-07-19 (USD per million tokens), covering the
# Claude Fable 5 / Opus 4.8 / Sonnet 5 lineup.
# Keys are model name prefixes for flexible matching.
_HARDCODED_RATES: dict[str, "ModelPricing"] = {}


@dataclass
class ModelPricing:
    """Per-model pricing in USD per million tokens."""

    input_per_mtok: Decimal = field(default_factory=lambda: Decimal("0"))
    output_per_mtok: Decimal = field(default_factory=lambda: Decimal("0"))
    cache_write_per_mtok: Decimal = field(default_factory=lambda: Decimal("0"))
    cache_read_per_mtok: Decimal = field(default_factory=lambda: Decimal("0"))

    def compute_cost(self, breakdown: TokenBreakdown) -> Decimal:
        M = Decimal("1000000")
        return (
            self.input_per_mtok * breakdown.input_tokens / M
            + self.output_per_mtok * breakdown.output_tokens / M
            + self.cache_write_per_mtok * breakdown.cache_creation_tokens / M
            + self.cache_read_per_mtok * breakdown.cache_read_tokens / M
        )


_HARDCODED_RATES = {
    "claude-fable-5": ModelPricing(
        input_per_mtok=Decimal("10.00"),
        output_per_mtok=Decimal("50.00"),
        cache_write_per_mtok=Decimal("12.50"),
        cache_read_per_mtok=Decimal("1.00"),
    ),
    "claude-opus-4-8": ModelPricing(
        input_per_mtok=Decimal("5.00"),
        output_per_mtok=Decimal("25.00"),
        cache_write_per_mtok=Decimal("6.25"),
        cache_read_per_mtok=Decimal("0.50"),
    ),
    "claude-opus-4-7": ModelPricing(
        input_per_mtok=Decimal("5.00"),
        output_per_mtok=Decimal("25.00"),
        cache_write_per_mtok=Decimal("6.25"),
        cache_read_per_mtok=Decimal("0.50"),
    ),
    # Standard rates; intro pricing $2/$10 per 1M applied through 2026-08-31.
    "claude-sonnet-5": ModelPricing(
        input_per_mtok=Decimal("3.00"),
        output_per_mtok=Decimal("15.00"),
        cache_write_per_mtok=Decimal("3.75"),
        cache_read_per_mtok=Decimal("0.30"),
    ),
    "claude-sonnet-4-6": ModelPricing(
        input_per_mtok=Decimal("3.00"),
        output_per_mtok=Decimal("15.00"),
        cache_write_per_mtok=Decimal("3.75"),
        cache_read_per_mtok=Decimal("0.30"),
    ),
    "claude-haiku-4-5": ModelPricing(
        input_per_mtok=Decimal("1.00"),
        output_per_mtok=Decimal("5.00"),
        cache_write_per_mtok=Decimal("1.25"),
        cache_read_per_mtok=Decimal("0.10"),
    ),
    "claude-3-5-sonnet": ModelPricing(
        input_per_mtok=Decimal("3.00"),
        output_per_mtok=Decimal("15.00"),
        cache_write_per_mtok=Decimal("3.75"),
        cache_read_per_mtok=Decimal("0.30"),
    ),
    "claude-3-5-haiku": ModelPricing(
        input_per_mtok=Decimal("0.80"),
        output_per_mtok=Decimal("4.00"),
        cache_write_per_mtok=Decimal("1.00"),
        cache_read_per_mtok=Decimal("0.08"),
    ),
    "claude-3-opus": ModelPricing(
        input_per_mtok=Decimal("15.00"),
        output_per_mtok=Decimal("75.00"),
        cache_write_per_mtok=Decimal("18.75"),
        cache_read_per_mtok=Decimal("1.50"),
    ),
}


class HardcodedPricingStrategy:
    """Static pricing table — exact match first, then prefix match."""

    def get_pricing(self, model: str) -> ModelPricing | None:
        if model in _HARDCODED_RATES:
            return _HARDCODED_RATES[model]
        for prefix, pricing in _HARDCODED_RATES.items():
            if model.startswith(prefix):
                return pricing
        return None


class LiteLLMPricingFetcher:
    """Fetch per-model pricing from LiteLLM's published JSON registry."""

    def __init__(self, url: str = _LITELLM_URL, timeout: float = 10.0) -> None:
        self._url = url
        self._timeout = timeout

    async def fetch(self) -> dict[str, ModelPricing]:
        """Return a dict mapping model name → ModelPricing."""
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(self._url)
                response.raise_for_status()
                raw: dict = json.loads(response.text)
        except Exception:
            return {}

        result: dict[str, ModelPricing] = {}
        for model, entry in raw.items():
            if not isinstance(entry, dict):
                continue
            if not model.startswith("claude"):
                continue
            inp = entry.get("input_cost_per_token") or 0.0
            out = entry.get("output_cost_per_token") or 0.0
            cw = entry.get("cache_creation_input_token_cost") or 0.0
            cr = entry.get("cache_read_input_token_cost") or 0.0
            result[model] = ModelPricing(
                input_per_mtok=Decimal(str(round(inp * 1_000_000, 6))),
                output_per_mtok=Decimal(str(round(out * 1_000_000, 6))),
                cache_write_per_mtok=Decimal(str(round(cw * 1_000_000, 6))),
                cache_read_per_mtok=Decimal(str(round(cr * 1_000_000, 6))),
            )
        return result


class CachedPricingStrategy:
    """Live pricing from LiteLLM with TTL cache; falls back to hardcoded rates."""

    def __init__(
        self,
        fetcher: LiteLLMPricingFetcher | None = None,
        fallback: HardcodedPricingStrategy | None = None,
        ttl_seconds: float = 3600.0,
    ) -> None:
        self._fetcher = fetcher or LiteLLMPricingFetcher()
        self._fallback = fallback or HardcodedPricingStrategy()
        self._ttl = ttl_seconds
        self._cached_data: dict[str, ModelPricing] = {}
        self._cache_fetched_at: float = 0.0

    def _is_cache_stale(self) -> bool:
        return (_time.monotonic() - self._cache_fetched_at) >= self._ttl

    async def refresh(self) -> None:
        """Fetch fresh pricing from LiteLLM and update the in-memory cache."""
        data = await self._fetcher.fetch()
        if data:
            self._cached_data = data
            self._cache_fetched_at = _time.monotonic()

    def get_pricing(self, model: str) -> ModelPricing | None:
        # Try live cache first (exact then prefix)
        if model in self._cached_data:
            return self._cached_data[model]
        for key, pricing in self._cached_data.items():
            if model.startswith(key):
                return pricing
        # Fall back to hardcoded
        return self._fallback.get_pricing(model)
