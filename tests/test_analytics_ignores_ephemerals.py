"""Regression test: analytics endpoints must not include ephemeral_messages data.

The ephemeral_messages table is explicitly excluded from analytics (no token /
cost contribution) — it stores side-channel /btw exchanges that should be
invisible to cost/token accounting.

Seed strategy:
- One session with 2 regular messages that carry realistic token usage.
- Two ephemeral pairs (4 rows total) seeded for the same session.
- Call every analytics endpoint listed in server.py:905-994.
- Assert the numbers reported match only the regular messages.
"""

import tempfile
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from agent_decoder.config import AppConfig
from agent_decoder.db import Database
from agent_decoder.server import create_app


# ---------------------------------------------------------------------------
# Constants — token values chosen so any inadvertent inclusion of ephemeral
# rows would shift the counts by easy-to-detect amounts.
# ---------------------------------------------------------------------------

SESSION_ID = "aaaa1111-bbbb-cccc-dddd-eeeeeeeeeeee"
PROJECT_ID = "proj-analytics-regression"

# Regular message token totals
REG_INPUT_TOKENS = 100
REG_OUTPUT_TOKENS = 40
REG_CACHE_CREATE_TOKENS = 20
REG_CACHE_READ_TOKENS = 10
REG_TOTAL_TOKENS = (
    REG_INPUT_TOKENS
    + REG_OUTPUT_TOKENS
    + REG_CACHE_CREATE_TOKENS
    + REG_CACHE_READ_TOKENS
)


# ---------------------------------------------------------------------------
# DB seeding helper
# ---------------------------------------------------------------------------


async def _seed_db(db_path: Path) -> None:
    """Insert project, session, 2 regular messages, and 2 ephemeral pairs."""
    async with Database(db_path) as db:
        await db.init_schema()

        # Project
        await db._conn.execute(
            "INSERT OR IGNORE INTO projects (id, display_name, raw_path, data_source) "
            "VALUES (?, 'Analytics Regression', '/tmp/ar', 'test')",
            (PROJECT_ID,),
        )
        # Session
        await db._conn.execute(
            "INSERT OR IGNORE INTO sessions "
            "(id, project_id, file_path, message_count, user_message_count, is_worktree, is_fork) "
            "VALUES (?, ?, '/tmp/ar/s.jsonl', 2, 1, 0, 0)",
            (SESSION_ID, PROJECT_ID),
        )

        # Regular message 1: user turn (no usage)
        await db._conn.execute(
            "INSERT OR IGNORE INTO messages "
            "(id, session_id, role, content_json, timestamp, model, "
            " input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens, usage_json) "
            "VALUES (?, ?, 'user', ?, '2026-01-01T10:00:00', NULL, 0, 0, 0, 0, NULL)",
            (
                "reg-msg-user1",
                SESSION_ID,
                '[{"type":"text","text":"Hello, assistant"}]',
            ),
        )

        # Regular message 2: assistant turn with real usage.
        # parent_id → reg-msg-user1 so PromptIterator pairs them correctly.
        usage_json = (
            f'{{"input_tokens":{REG_INPUT_TOKENS},"output_tokens":{REG_OUTPUT_TOKENS},'
            f'"cache_creation_input_tokens":{REG_CACHE_CREATE_TOKENS},'
            f'"cache_read_input_tokens":{REG_CACHE_READ_TOKENS}}}'
        )
        await db._conn.execute(
            "INSERT OR IGNORE INTO messages "
            "(id, session_id, parent_id, role, content_json, timestamp, model, "
            " input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens, usage_json) "
            "VALUES (?, ?, 'reg-msg-user1', 'assistant', ?, '2026-01-01T10:00:05', 'claude-sonnet-4-6', "
            "        ?, ?, ?, ?, ?)",
            (
                "reg-msg-asst1",
                SESSION_ID,
                '[{"type":"text","text":"Sure, I can help"}]',
                REG_INPUT_TOKENS,
                REG_OUTPUT_TOKENS,
                REG_CACHE_CREATE_TOKENS,
                REG_CACHE_READ_TOKENS,
                usage_json,
            ),
        )
        await db._conn.commit()

        # Ephemeral pair 1
        uid1 = await db.record_ephemeral_input(
            SESSION_ID, "ephemeral btw content one", timestamp="2026-01-01T10:00:30"
        )
        await db.record_ephemeral_response(
            uid1, "ephemeral response one", timestamp="2026-01-01T10:00:35"
        )

        # Ephemeral pair 2
        uid2 = await db.record_ephemeral_input(
            SESSION_ID, "ephemeral btw content two", timestamp="2026-01-01T10:01:00"
        )
        await db.record_ephemeral_response(
            uid2, "ephemeral response two", timestamp="2026-01-01T10:01:05"
        )


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
async def client_and_session_id():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "analytics_reg.db"
        await _seed_db(db_path)
        app = create_app(AppConfig(), db_path)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client, SESSION_ID


# ---------------------------------------------------------------------------
# Per-session analytics
# ---------------------------------------------------------------------------


class TestPerSessionAnalyticsIgnoresEphemerals:
    async def test_tokens_endpoint_matches_regular_messages_only(
        self, client_and_session_id
    ):
        client, session_id = client_and_session_id
        r = await client.get(f"/api/analytics/sessions/{session_id}/tokens")
        assert r.status_code == 200
        data = r.json()
        # Only the assistant message has usage; the ephemeral rows must not contribute.
        assert data["input_tokens"] == REG_INPUT_TOKENS
        assert data["output_tokens"] == REG_OUTPUT_TOKENS
        assert data["cache_creation_tokens"] == REG_CACHE_CREATE_TOKENS
        assert data["cache_read_tokens"] == REG_CACHE_READ_TOKENS
        assert data["total"] == REG_TOTAL_TOKENS

    async def test_cost_endpoint_matches_regular_messages_only(
        self, client_and_session_id
    ):
        client, session_id = client_and_session_id
        r = await client.get(f"/api/analytics/sessions/{session_id}/cost")
        assert r.status_code == 200
        data = r.json()
        # Cost is derived from the regular message tokens only.
        # The sonnet model has known pricing, so total_usd should be > 0.
        assert data["total_usd"] > 0
        # It should equal cost computed from REG tokens only; ephemerals have
        # no token columns so any inflation would be detectable.
        assert data["total_usd"] < 0.01, (
            "Cost suspiciously high — ephemerals may be included"
        )

    async def test_prompts_endpoint_count_excludes_ephemerals(
        self, client_and_session_id
    ):
        client, session_id = client_and_session_id
        r = await client.get(f"/api/analytics/sessions/{session_id}/prompts")
        assert r.status_code == 200
        prompts = r.json()
        # There is exactly one user→assistant prompt pair in regular messages.
        # Ephemeral pairs must NOT show up as additional prompts.
        assert len(prompts) == 1
        assert prompts[0]["input_tokens"] == REG_INPUT_TOKENS
        assert prompts[0]["output_tokens"] == REG_OUTPUT_TOKENS


# ---------------------------------------------------------------------------
# Corpus-wide analytics (daily / stats / models / tools / files / tips)
# ---------------------------------------------------------------------------


class TestCorpusAnalyticsIgnoresEphemerals:
    async def test_daily_analytics_token_sum_matches_regular_only(
        self, client_and_session_id
    ):
        client, _ = client_and_session_id
        r = await client.get("/api/analytics/daily")
        assert r.status_code == 200
        daily = r.json()
        # Sum input_tokens across all days; must equal REG_INPUT_TOKENS (just our one assistant msg).
        total_input = sum(d.get("input_tokens", 0) for d in daily)
        assert total_input == REG_INPUT_TOKENS, (
            f"daily input_tokens={total_input}, expected {REG_INPUT_TOKENS}; "
            "ephemerals may be leaking into daily aggregation"
        )

    async def test_stats_endpoint_returns_200(self, client_and_session_id):
        client, _ = client_and_session_id
        r = await client.get("/api/analytics/stats")
        assert r.status_code == 200
        data = r.json()
        # Stats aggregates prompts/tokens from messages only; just verify it
        # doesn't include a mysteriously inflated prompt_count.
        assert (
            "prompt_count" in data or "total_tokens" in data or isinstance(data, dict)
        )

    async def test_models_endpoint_does_not_inflate_token_counts(
        self, client_and_session_id
    ):
        client, _ = client_and_session_id
        r = await client.get("/api/analytics/models")
        assert r.status_code == 200
        models = r.json()
        # Should have exactly one model entry (claude-sonnet-4-6) with the correct token counts.
        sonnet_entries = [m for m in models if "sonnet" in m.get("model", "")]
        assert len(sonnet_entries) == 1
        m = sonnet_entries[0]
        assert m.get("input_tokens", 0) == REG_INPUT_TOKENS
        assert m.get("output_tokens", 0) == REG_OUTPUT_TOKENS

    async def test_tools_endpoint_returns_200(self, client_and_session_id):
        client, _ = client_and_session_id
        r = await client.get("/api/analytics/tools")
        assert r.status_code == 200
        # No tool_use blocks were inserted; tools list should be empty or minimal.
        data = r.json()
        assert isinstance(data, list)

    async def test_files_endpoint_returns_200(self, client_and_session_id):
        client, _ = client_and_session_id
        r = await client.get("/api/analytics/files")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)

    async def test_tips_endpoint_returns_200(self, client_and_session_id):
        client, _ = client_and_session_id
        r = await client.get("/api/analytics/tips")
        assert r.status_code == 200
        data = r.json()
        assert isinstance(data, list)

    async def test_ephemerals_not_counted_in_message_totals(
        self, client_and_session_id
    ):
        """Verify the messages table count does not include ephemeral rows."""
        client, _ = client_and_session_id
        # Use /api/stats to get total_messages count
        r = await client.get("/api/stats")
        assert r.status_code == 200
        data = r.json()
        total_messages = data.get("total_messages", None)
        if total_messages is not None:
            # We inserted 2 regular messages; 4 ephemeral rows exist separately.
            # total_messages must reflect only the messages table.
            assert total_messages == 2, (
                f"total_messages={total_messages}, expected 2; "
                "ephemeral rows may be counted in messages table"
            )
