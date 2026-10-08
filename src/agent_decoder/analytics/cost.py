from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from .models import PromptCost, TokenBreakdown

if TYPE_CHECKING:
    from .pricing import CachedPricingStrategy, ModelPricing


@dataclass
class SessionCost:
    model: str
    pricing: "ModelPricing | None"
    breakdown: TokenBreakdown
    input_usd: Decimal
    output_usd: Decimal
    cache_write_usd: Decimal
    cache_read_usd: Decimal
    total_usd: Decimal


@dataclass
class MultiModelCost:
    models: list[SessionCost]
    total_usd: Decimal


class CostEngine:
    """Compute USD cost for a model+breakdown pair using a pricing strategy."""

    def __init__(self, strategy: "CachedPricingStrategy") -> None:
        self._strategy = strategy

    def compute(self, model: str, breakdown: TokenBreakdown) -> SessionCost:
        pricing = self._strategy.get_pricing(model)
        if pricing is None:
            zero = Decimal("0")
            return SessionCost(
                model=model,
                pricing=None,
                breakdown=breakdown,
                input_usd=zero,
                output_usd=zero,
                cache_write_usd=zero,
                cache_read_usd=zero,
                total_usd=zero,
            )
        M = Decimal("1000000")
        inp = pricing.input_per_mtok * breakdown.input_tokens / M
        out = pricing.output_per_mtok * breakdown.output_tokens / M
        cw = pricing.cache_write_per_mtok * breakdown.cache_creation_tokens / M
        cr = pricing.cache_read_per_mtok * breakdown.cache_read_tokens / M
        return SessionCost(
            model=model,
            pricing=pricing,
            breakdown=breakdown,
            input_usd=inp,
            output_usd=out,
            cache_write_usd=cw,
            cache_read_usd=cr,
            total_usd=inp + out + cw + cr,
        )

    def compute_multi(self, prompts: list[PromptCost]) -> MultiModelCost:
        by_model: dict[str, TokenBreakdown] = {}
        for p in prompts:
            bd = by_model.setdefault(p.model, TokenBreakdown())
            by_model[p.model] = bd + p.breakdown
        results: list[SessionCost] = []
        for model, bd in sorted(by_model.items()):
            results.append(self.compute(model, bd))
        total = sum((r.total_usd for r in results), Decimal("0"))
        return MultiModelCost(models=results, total_usd=total)
