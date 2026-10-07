"""Tests for unified search that includes ephemeral (/btw) hits.

Covers:
- Words only in ephemerals are returned with source="ephemeral"
- Words in both messages and ephemerals return hits with correct source fields
- Session-scoped project filter works for ephemeral hits
- The FTS5-operator sanitiser fires for ephemeral queries
- Merged result set respects the limit across both tables
- Empty query / no results behaves gracefully
- Ephemeral hits carry kind and responds_to metadata
- Messages-only session returns no ephemeral hits; ephemeral-only returns no message hits
"""

import tempfile
from pathlib import Path

import pytest

from agent_decoder.db import Database


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_session(db: Database, session_id: str = "s-search-001") -> None:
    """Insert a minimal project + session so ephemeral FK (if on) can work."""
    await db._conn.execute(
        "INSERT OR IGNORE INTO projects (id, display_name, raw_path, data_source) "
        "VALUES ('p-search', 'Search Tests', '/tmp/search', 'test')"
    )
    await db._conn.execute(
        "INSERT OR IGNORE INTO sessions "
        "(id, project_id, file_path, message_count, user_message_count, is_worktree, is_fork) "
        "VALUES (?, 'p-search', '/tmp/search/s.jsonl', 0, 0, 0, 0)",
        (session_id,),
    )
    await db._conn.commit()


async def _seed_message(
    db: Database,
    session_id: str,
    msg_id: str,
    text: str,
    role: str = "user",
    timestamp: str = "2026-01-01T10:00:00",
) -> None:
    """Insert a minimal message row + FTS entry directly."""
    await db._conn.execute(
        "INSERT OR IGNORE INTO messages "
        "(id, session_id, role, content_json, timestamp) "
        "VALUES (?, ?, ?, ?, ?)",
        (msg_id, session_id, role, f'[{{"type":"text","text":"{text}"}}]', timestamp),
    )
    await db._conn.execute(
        "INSERT INTO messages_fts (content, session_id, message_id, role) VALUES (?, ?, ?, ?)",
        (text, session_id, msg_id, role),
    )
    await db._conn.commit()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "search_eph.db"
        async with Database(db_path) as database:
            await database.init_schema()
            yield database


@pytest.fixture
async def db_with_data(db):
    """DB seeded with one session, two regular messages, and two ephemeral pairs."""
    await _seed_session(db, "sess-main")
    # Regular messages
    await _seed_message(
        db,
        "sess-main",
        "msg-u1",
        "regularcontent hello world",
        timestamp="2026-01-01T10:00:00",
    )
    await _seed_message(
        db,
        "sess-main",
        "msg-a1",
        "regularreply fine day",
        role="assistant",
        timestamp="2026-01-01T10:00:05",
    )
    # Ephemeral pair
    uid = await db.record_ephemeral_input(
        "sess-main", "ephemeralonly special content", timestamp="2026-01-01T10:01:00"
    )
    await db.record_ephemeral_response(
        uid, "ephemeralreply answer here", timestamp="2026-01-01T10:01:05"
    )
    return db


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEphemeralOnlySearch:
    async def test_word_only_in_ephemeral_returns_ephemeral_hit(self, db_with_data):
        hits = await db_with_data.search("ephemeralonly")
        assert len(hits) >= 1
        eph_hits = [h for h in hits if h.source == "ephemeral"]
        assert len(eph_hits) >= 1

    async def test_ephemeral_hit_has_correct_source_field(self, db_with_data):
        hits = await db_with_data.search("ephemeralonly")
        eph = next(h for h in hits if h.source == "ephemeral")
        assert eph.source == "ephemeral"

    async def test_ephemeral_hit_carries_kind_metadata(self, db_with_data):
        hits = await db_with_data.search("ephemeralonly")
        eph = next(h for h in hits if h.source == "ephemeral")
        assert eph.kind == "btw"

    async def test_word_only_in_regular_messages_returns_no_ephemeral_hits(
        self, db_with_data
    ):
        hits = await db_with_data.search("regularcontent")
        assert all(h.source == "message" for h in hits)

    async def test_ephemeral_response_hit_carries_responds_to(self, db):
        """The assistant response row should carry a responds_to linking back to the input."""
        await _seed_session(db, "sess-rt")
        uid = await db.record_ephemeral_input(
            "sess-rt", "inputquestion unique123", timestamp="2026-01-01T11:00:00"
        )
        await db.record_ephemeral_response(
            uid, "responseanswer unique456", timestamp="2026-01-01T11:00:05"
        )
        hits = await db.search("unique456")
        resp_hits = [h for h in hits if h.source == "ephemeral"]
        assert len(resp_hits) == 1
        assert resp_hits[0].responds_to == uid


class TestMixedSearch:
    async def test_shared_word_returns_both_sources(self, db):
        """A word present in both a regular message and an ephemeral returns both."""
        await _seed_session(db, "sess-mix")
        await _seed_message(
            db,
            "sess-mix",
            "msg-mix1",
            "sharedword in regular message",
            timestamp="2026-01-01T09:00:00",
        )
        uid = await db.record_ephemeral_input(
            "sess-mix", "sharedword in ephemeral", timestamp="2026-01-01T09:01:00"
        )
        await db.record_ephemeral_response(
            uid, "reply without the word", timestamp="2026-01-01T09:01:05"
        )

        hits = await db.search("sharedword")
        sources = {h.source for h in hits}
        assert "message" in sources
        assert "ephemeral" in sources

    async def test_source_field_is_message_for_regular_hits(self, db_with_data):
        hits = await db_with_data.search("regularcontent")
        msg_hits = [h for h in hits if h.source == "message"]
        assert len(msg_hits) >= 1
        assert all(h.kind is None for h in msg_hits)
        assert all(h.responds_to is None for h in msg_hits)


class TestSessionScopedSearch:
    async def test_ephemeral_in_different_project_not_returned(self, db):
        """Searching with project_id only returns ephemerals in sessions under that project."""
        # Project A
        await db._conn.execute(
            "INSERT OR IGNORE INTO projects (id, display_name, raw_path, data_source) "
            "VALUES ('proj-A', 'A', '/a', 'test')"
        )
        await db._conn.execute(
            "INSERT OR IGNORE INTO sessions "
            "(id, project_id, file_path, message_count, user_message_count, is_worktree, is_fork) "
            "VALUES ('sess-A', 'proj-A', '/a/s.jsonl', 0, 0, 0, 0)"
        )
        # Project B
        await db._conn.execute(
            "INSERT OR IGNORE INTO projects (id, display_name, raw_path, data_source) "
            "VALUES ('proj-B', 'B', '/b', 'test')"
        )
        await db._conn.execute(
            "INSERT OR IGNORE INTO sessions "
            "(id, project_id, file_path, message_count, user_message_count, is_worktree, is_fork) "
            "VALUES ('sess-B', 'proj-B', '/b/s.jsonl', 0, 0, 0, 0)"
        )
        await db._conn.commit()

        await db.record_ephemeral_input("sess-A", "projectAlphaWord content")
        await db.record_ephemeral_input("sess-B", "projectBetaWord content")

        hits_a = await db.search("projectAlphaWord", project_id="proj-A")
        hits_b = await db.search("projectBetaWord", project_id="proj-A")

        assert len(hits_a) >= 1
        assert all(h.session_id == "sess-A" for h in hits_a)
        assert len(hits_b) == 0


class TestSanitiser:
    async def test_fts5_operators_stripped_for_ephemeral(self, db):
        """Queries with FTS5 operators like hyphen/colon must not raise; they're sanitised."""
        await _seed_session(db, "sess-san")
        await db.record_ephemeral_input("sess-san", "sanitiserword content")
        # These would crash FTS5 without sanitisation
        hits_hyphen = await db.search("sanitiserword-stuff")
        hits_colon = await db.search("sanitiserword:field")
        # The sanitiser normalises to plain words; the unique word may or may not match
        # depending on tokenisation, but it must NOT raise an exception.
        assert isinstance(hits_hyphen, list)
        assert isinstance(hits_colon, list)


class TestLimitAndPagination:
    async def test_merged_result_respects_limit(self, db):
        """With limit=2 and hits in both tables, total hits <= 2."""
        await _seed_session(db, "sess-lim")
        await _seed_message(
            db,
            "sess-lim",
            "msg-lim1",
            "limitword first regular",
            timestamp="2026-01-01T08:00:00",
        )
        await _seed_message(
            db,
            "sess-lim",
            "msg-lim2",
            "limitword second regular",
            timestamp="2026-01-01T08:01:00",
        )
        await db.record_ephemeral_input(
            "sess-lim", "limitword third ephemeral", timestamp="2026-01-01T08:02:00"
        )
        await db.record_ephemeral_input(
            "sess-lim", "limitword fourth ephemeral", timestamp="2026-01-01T08:03:00"
        )

        hits = await db.search("limitword", limit=2)
        assert len(hits) <= 2


class TestEdgeCases:
    async def test_empty_query_returns_empty_list(self, db_with_data):
        hits = await db_with_data.search("")
        assert hits == []

    async def test_no_matches_returns_empty_list(self, db_with_data):
        hits = await db_with_data.search("zzz_no_match_xyz_9999")
        assert hits == []

    async def test_session_with_no_ephemerals_returns_message_hits_only(self, db):
        await _seed_session(db, "sess-noeph")
        await _seed_message(db, "sess-noeph", "msg-ne1", "noeword regular only")
        hits = await db.search("noeword")
        assert all(h.source == "message" for h in hits)
