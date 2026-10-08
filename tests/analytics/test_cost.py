"""Tests for CostEngine — maps (model, TokenBreakdown) → SessionCost."""

from decimal import Decimal
from unittest.mock import MagicMock

from agent_decoder.analytics.models import TokenBreakdown
from agent_decoder.analytics.pricing import ModelPricing


def _pricing(inp: str, out: str, cw: str = "0", cr: str = "0") -> ModelPricing:
    return ModelPricing(
        input_per_mtok=Decimal(inp),
        output_per_mtok=Decimal(out),
        cache_write_per_mtok=Decimal(cw),
        cache_read_per_mtok=Decimal(cr),
    )


def _make_engine(pricing_map: dict):
    from agent_decoder.analytics.cost import CostEngine

    strategy = MagicMock()
    strategy.get_pricing.side_effect = lambda model: pricing_map.get(model)
    return CostEngine(strategy)


class TestCostEngine:
    def test_computes_cost_for_known_model(self):
        engine = _make_engine(
            {"claude-sonnet-4-6": _pricing("3", "15", "3.75", "0.30")}
        )
        bd = TokenBreakdown(input_tokens=1_000_000, output_tokens=0)
        result = engine.compute("claude-sonnet-4-6", bd)
        assert result.total_usd == Decimal("3.00")
        assert result.pricing is not None

    def test_returns_zero_cost_for_unknown_model(self):
        engine = _make_engine({})
        bd = TokenBreakdown(input_tokens=100_000, output_tokens=50_000)
        result = engine.compute("unknown-model", bd)
        assert result.total_usd == Decimal("0")
        assert result.pricing is None

    def test_session_cost_breakdown_fields(self):
        engine = _make_engine({"claude-haiku-4-5": _pricing("0.80", "4", "1", "0.08")})
        bd = TokenBreakdown(
            input_tokens=500_000,
            output_tokens=200_000,
            cache_creation_tokens=100_000,
            cache_read_tokens=50_000,
        )
        result = engine.compute("claude-haiku-4-5", bd)
        M = Decimal("1000000")
        assert result.input_usd == Decimal("0.80") * 500_000 / M
        assert result.output_usd == Decimal("4") * 200_000 / M
        assert result.cache_write_usd == Decimal("1") * 100_000 / M
        assert result.cache_read_usd == Decimal("0.08") * 50_000 / M
        expected_total = (
            result.input_usd
            + result.output_usd
            + result.cache_write_usd
            + result.cache_read_usd
        )
        assert result.total_usd == expected_total

    def test_zero_tokens_returns_zero_cost(self):
        engine = _make_engine({"claude-sonnet-4-6": _pricing("3", "15")})
        result = engine.compute("claude-sonnet-4-6", TokenBreakdown())
        assert result.total_usd == Decimal("0")


class TestCostRoutes:
    async def test_cost_route_exists(self, tmp_path):
        from httpx import ASGITransport, AsyncClient

        from agent_decoder.config import load_config
        from agent_decoder.db import Database
        from agent_decoder.server import create_app

        db_path = tmp_path / "test.db"
        async with Database(db_path) as db:
            await db.init_schema()
        app = create_app(load_config(), db_path)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            r = await client.get("/api/analytics/sessions/nonexistent/cost")
            # 404 from our handler means route is registered (not a missing-route 404)
            assert r.status_code == 404
            assert r.json().get("detail") == "Session not found"

    async def test_pricing_route_exists(self, tmp_path):
        from httpx import ASGITransport, AsyncClient

        from agent_decoder.config import load_config
        from agent_decoder.db import Database
        from agent_decoder.server import create_app

        db_path = tmp_path / "test.db"
        async with Database(db_path) as db:
            await db.init_schema()
        app = create_app(load_config(), db_path)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            r = await client.get("/api/pricing")
            assert r.status_code == 200
            data = r.json()
            assert "models" in data
            assert "source" in data
