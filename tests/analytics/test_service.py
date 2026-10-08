"""Tests for TokenAnalyticsService."""

from agent_decoder.models import Message, TokenUsage
from datetime import datetime, timezone


def _asst(
    id: str,
    input: int,
    output: int,
    ts: datetime | None = None,
    parent_id: str | None = None,
) -> Message:
    return Message(
        id=id,
        session_id="s1",
        role="assistant",
        parent_id=parent_id,
        timestamp=ts,
        usage=TokenUsage(input_tokens=input, output_tokens=output),
    )


SESSION_MESSAGES = [
    Message(id="u1", session_id="s1", role="user"),
    _asst("a1", 10, 5, datetime(2026, 1, 1, 10, tzinfo=timezone.utc), parent_id="u1"),
    Message(id="u2", session_id="s1", role="user", parent_id="a1"),
    _asst("a2", 20, 3, datetime(2026, 1, 2, 10, tzinfo=timezone.utc), parent_id="u2"),
]


class TestTokenAnalyticsService:
    def setup_method(self):
        from agent_decoder.analytics.service import TokenAnalyticsService

        self.svc = TokenAnalyticsService()

    def test_session_totals(self):
        bd = self.svc.session_totals(SESSION_MESSAGES)
        assert bd.input_tokens == 30
        assert bd.output_tokens == 8

    def test_prompt_breakdown_sorted_by_total_desc(self):
        prompts = self.svc.prompt_breakdown(SESSION_MESSAGES)
        assert len(prompts) == 2
        assert prompts[0].breakdown.total >= prompts[1].breakdown.total

    def test_daily_buckets_count(self):
        buckets = self.svc.daily_buckets(SESSION_MESSAGES)
        assert len(buckets) == 2

    def test_dedup_prevents_double_count(self):
        msg = _asst("dup", 100, 50)
        bd = self.svc.session_totals([msg, msg])
        assert bd.input_tokens == 100  # not 200


class TestAnalyticsRoutes:
    async def test_tokens_route_exists(self):
        """Smoke test: route exists and returns token fields."""
        import tempfile
        from pathlib import Path
        from httpx import AsyncClient, ASGITransport
        from agent_decoder.server import create_app
        from agent_decoder.config import load_config
        from agent_decoder.db import Database

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            # Initialise schema so the route can query the DB
            async with Database(db_path) as db:
                await db.init_schema()
            app = create_app(load_config(), db_path)
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                r = await client.get("/api/analytics/sessions/nonexistent/tokens")
        # Route must exist — 404 (session not found) is acceptable; 404 from
        # missing route would also be 404, but the body distinguishes them.
        # We verify the route is registered by confirming the app raises our
        # HTTPException (detail = "Session not found") rather than a framework 404.
        assert r.status_code == 404
        assert r.json()["detail"] == "Session not found"
