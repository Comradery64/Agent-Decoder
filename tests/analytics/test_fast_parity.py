"""Parity guard: the SQL-backed ``analytics.fast`` bundle must produce
byte-for-byte identical output to the in-memory Python scanners.

The scanners are the source of truth for the dashboard's shapes; ``fast``
is an optimization that aggregates in SQLite instead of loading every
message. These tests seed a crafted corpus exercising every metric
(daily buckets, model usage, prompt-stat percentiles, tool/file counts,
and all three tip rules) and assert the two paths agree.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent_decoder.analytics import fast
from agent_decoder.analytics.service import TokenAnalyticsService
from agent_decoder.analytics.stats import (
    FileTouchScanner,
    ModelUsageScanner,
    PromptStatsScanner,
    ToolUsageScanner,
)
from agent_decoder.analytics.tips import (
    LowCacheHitRule,
    OversizedToolResultRule,
    RepeatedFileReadRule,
    TipRegistry,
)
from agent_decoder.db import Database
from agent_decoder.models import (
    Message,
    TokenUsage,
    ToolResultBlock,
    ToolUseBlock,
)

T0 = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)


def _msgs() -> list[Message]:
    """A corpus touching every metric + tip rule."""
    out: list[Message] = []

    def add(**kw):
        out.append(Message(**kw))

    # Two days, two sessions, real user->assistant prompt links with usage.
    for d in range(2):
        day = T0 + timedelta(days=d)
        sid = f"sess-{d}"
        add(id=f"u{d}", session_id=sid, role="user", timestamp=day, is_meta=False)
        add(
            id=f"a{d}",
            session_id=sid,
            parent_id=f"u{d}",
            role="assistant",
            model="claude-opus-4-7",
            timestamp=day,
            usage=TokenUsage(
                input_tokens=4000,
                output_tokens=900,
                cache_creation_input_tokens=200,
                cache_read_input_tokens=50,
            ),
            content_blocks=[
                ToolUseBlock(
                    id=f"tu{d}", name="Read", input={"file_path": "/repo/a.py"}
                ),
            ],
        )

    # Repeated reads of the same file (>=3, no intervening write) -> tip.
    for i in range(3):
        add(
            id=f"r{i}",
            session_id="sess-0",
            parent_id="u0",
            role="assistant",
            timestamp=T0 + timedelta(minutes=i),
            content_blocks=[
                ToolUseBlock(
                    id=f"rr{i}", name="Read", input={"file_path": "/repo/hot.py"}
                )
            ],
        )

    # A few more tool_use blocks for tool/file counts.
    add(
        id="b1",
        session_id="sess-1",
        role="assistant",
        timestamp=T0,
        content_blocks=[
            ToolUseBlock(id="tb1", name="Bash", input={"command": "ls"}),
            ToolUseBlock(id="tb2", name="Edit", input={"file_path": "/repo/a.py"}),
        ],
    )

    # Oversized tool_result (>50k chars) -> tip.
    add(
        id="res1",
        session_id="sess-1",
        role="user",
        timestamp=T0,
        content_blocks=[ToolResultBlock(tool_use_id="tb1", content="x" * 60_000)],
    )
    return out


async def _seed(db_path) -> None:
    async with Database(db_path) as db:
        await db.init_schema()
        await db.upsert_messages(_msgs())


def _old_bundle(msgs):
    svc = TokenAnalyticsService()
    reg = TipRegistry()
    for rule in (RepeatedFileReadRule(), OversizedToolResultRule(), LowCacheHitRule()):
        reg.register(rule)
    return {
        "daily": [
            {
                "day": b.day.isoformat(),
                "input_tokens": b.breakdown.input_tokens,
                "output_tokens": b.breakdown.output_tokens,
                "cache_creation_tokens": b.breakdown.cache_creation_tokens,
                "cache_read_tokens": b.breakdown.cache_read_tokens,
                "total": b.breakdown.total,
                "prompt_count": b.prompt_count,
                "session_count": b.session_count,
            }
            for b in svc.daily_buckets(msgs)
        ],
        "models": ModelUsageScanner().scan(msgs),
        "stats": PromptStatsScanner().scan(msgs),
        "tools": ToolUsageScanner().scan(msgs),
        "files": FileTouchScanner().scan(msgs),
        "tips": [
            {
                "rule_id": t.rule_id,
                "severity": t.severity,
                "title": t.title,
                "detail": t.detail,
                "evidence": t.evidence,
            }
            for t in reg.run(msgs)
        ],
    }


@pytest.mark.parametrize("key", ["daily", "models", "stats", "tools", "files", "tips"])
async def test_fast_matches_scanner(tmp_path, key):
    db_path = tmp_path / "index.db"
    await _seed(db_path)
    async with Database(db_path) as db:
        old = _old_bundle(await db.get_all_messages())
        new = await fast.compute_bundle(db._conn)
    if key in ("tools", "files"):
        # Counts must match exactly; order among *equal* counts is cosmetic
        # (fast uses a name tiebreak, the scanner uses first-seen). Compare
        # the (name, count) multiset and require non-increasing counts.
        assert {tuple(sorted(d.items())) for d in new[key]} == {
            tuple(sorted(d.items())) for d in old[key]
        }
        counts = [d["count"] for d in new[key]]
        assert counts == sorted(counts, reverse=True)
    else:
        assert new[key] == old[key]


async def test_tips_cover_all_rules(tmp_path):
    """Sanity: the crafted corpus actually triggers each tip rule, so the
    parity assertion above is meaningful (not comparing empty lists)."""
    db_path = tmp_path / "index.db"
    await _seed(db_path)
    async with Database(db_path) as db:
        bundle = await fast.compute_bundle(db._conn)
    rule_ids = {t["rule_id"] for t in bundle["tips"]}
    assert {"repeated_file_read", "oversized_tool_result", "low_cache_hit"} <= rule_ids
