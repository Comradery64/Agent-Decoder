"""Tests for ephemeral_messages table, FTS5, and Database methods.

Covers:
- Schema idempotency across two Database() instantiations
- record_ephemeral_input: return value, persisted fields
- record_ephemeral_response: responds_to link, session_id/kind inheritance
- FK violation: nonexistent input_row_id raises sqlite3.IntegrityError
- get_ephemeral_messages: ordered pair retrieval
- FTS round-trip: search finds content; session-scoped filter works
- Trigger sync: insert→search, update→search, delete→search
- sessions FK behaviour mirrors messages table (FK pragma off by default)
"""

import sqlite3
import tempfile
from pathlib import Path

import pytest

from agent_decoder.db import Database


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db():
    """Fresh in-memory-ish DB with schema initialised."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test_ephemeral.db"
        async with Database(db_path) as database:
            await database.init_schema()
            yield database


@pytest.fixture
async def db_with_session(db):
    """DB with a real sessions row so FK-checking tests can opt in."""
    await db._conn.execute("PRAGMA foreign_keys = ON")
    # Insert a minimal project first (sessions FK → projects)
    await db._conn.execute(
        "INSERT OR IGNORE INTO projects (id, display_name, raw_path, data_source) "
        "VALUES ('p1', 'Test', '/t', 'test')"
    )
    await db._conn.execute(
        "INSERT OR IGNORE INTO sessions "
        "(id, project_id, file_path, message_count, user_message_count, is_worktree, is_fork) "
        "VALUES ('sess-001', 'p1', '/t/s.jsonl', 0, 0, 0, 0)"
    )
    await db._conn.commit()
    return db


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class TestSchema:
    async def test_init_schema_idempotent_single_db(self, db):
        """Calling init_schema() twice on the same connection must not raise."""
        await db.init_schema()  # second call
        await db.init_schema()  # third call

    async def test_init_schema_idempotent_two_instances(self, tmp_path):
        """Two separate Database() instances opening the same file must both succeed."""
        db_path = tmp_path / "shared.db"
        async with Database(db_path) as db1:
            await db1.init_schema()
        async with Database(db_path) as db2:
            await db2.init_schema()  # must not raise

    async def test_ephemeral_messages_table_exists(self, db):
        async with db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ephemeral_messages'"
        ) as cur:
            row = await cur.fetchone()
        assert row is not None, "ephemeral_messages table not created"

    async def test_ephemeral_messages_fts_table_exists(self, db):
        async with db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='ephemeral_messages_fts'"
        ) as cur:
            row = await cur.fetchone()
        assert row is not None, "ephemeral_messages_fts table not created"

    async def test_idx_ephemeral_session_exists(self, db):
        async with db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_ephemeral_session'"
        ) as cur:
            row = await cur.fetchone()
        assert row is not None, "idx_ephemeral_session index not created"

    async def test_triggers_exist(self, db):
        async with db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'ephemeral_fts_%'"
        ) as cur:
            rows = await cur.fetchall()
        names = {r[0] for r in rows}
        assert "ephemeral_fts_insert" in names
        assert "ephemeral_fts_update" in names
        assert "ephemeral_fts_delete" in names


# ---------------------------------------------------------------------------
# record_ephemeral_input
# ---------------------------------------------------------------------------


class TestRecordEphemeralInput:
    async def test_returns_positive_row_id(self, db):
        row_id = await db.record_ephemeral_input("sess-x", "hello btw world")
        assert isinstance(row_id, int)
        assert row_id > 0

    async def test_sequential_ids_increase(self, db):
        id1 = await db.record_ephemeral_input("sess-x", "first")
        id2 = await db.record_ephemeral_input("sess-x", "second")
        assert id2 > id1

    async def test_persisted_fields(self, db):
        row_id = await db.record_ephemeral_input(
            "sess-a", "/btw what is 2+2?", kind="btw", timestamp="2026-01-01T10:00:00"
        )
        rows = await db.get_ephemeral_messages("sess-a")
        assert len(rows) == 1
        r = rows[0]
        assert r["id"] == row_id
        assert r["session_id"] == "sess-a"
        assert r["kind"] == "btw"
        assert r["role"] == "user"
        assert r["content"] == "/btw what is 2+2?"
        assert r["responds_to"] is None
        assert r["timestamp"] == "2026-01-01T10:00:00"

    async def test_default_kind_is_btw(self, db):
        await db.record_ephemeral_input("sess-b", "some content")
        rows = await db.get_ephemeral_messages("sess-b")
        assert rows[0]["kind"] == "btw"

    async def test_timestamp_defaults_to_now(self, db):
        await db.record_ephemeral_input("sess-ts", "content")
        rows = await db.get_ephemeral_messages("sess-ts")
        # Just verify it's a non-empty ISO string; no exact value assertion
        assert rows[0]["timestamp"] != ""
        assert rows[0]["timestamp"] is not None


# ---------------------------------------------------------------------------
# record_ephemeral_response
# ---------------------------------------------------------------------------


class TestRecordEphemeralResponse:
    async def test_returns_positive_row_id(self, db):
        input_id = await db.record_ephemeral_input("sess-r", "user q")
        resp_id = await db.record_ephemeral_response(input_id, "assistant answer")
        assert isinstance(resp_id, int)
        assert resp_id > input_id

    async def test_responds_to_links_correctly(self, db):
        input_id = await db.record_ephemeral_input("sess-link", "q")
        resp_id = await db.record_ephemeral_response(input_id, "a")
        rows = await db.get_ephemeral_messages("sess-link")
        resp_row = next(r for r in rows if r["id"] == resp_id)
        assert resp_row["responds_to"] == input_id

    async def test_inherits_session_id_from_input(self, db):
        input_id = await db.record_ephemeral_input("sess-inherit", "q")
        resp_id = await db.record_ephemeral_response(input_id, "answer")
        rows = await db.get_ephemeral_messages("sess-inherit")
        resp_row = next(r for r in rows if r["id"] == resp_id)
        assert resp_row["session_id"] == "sess-inherit"

    async def test_inherits_kind_from_input(self, db):
        input_id = await db.record_ephemeral_input("sess-kind", "q", kind="btw")
        resp_id = await db.record_ephemeral_response(input_id, "answer")
        rows = await db.get_ephemeral_messages("sess-kind")
        resp_row = next(r for r in rows if r["id"] == resp_id)
        assert resp_row["kind"] == "btw"

    async def test_role_is_assistant(self, db):
        input_id = await db.record_ephemeral_input("sess-role", "q")
        resp_id = await db.record_ephemeral_response(input_id, "answer")
        rows = await db.get_ephemeral_messages("sess-role")
        resp_row = next(r for r in rows if r["id"] == resp_id)
        assert resp_row["role"] == "assistant"

    async def test_nonexistent_input_row_id_raises_integrity_error(self, db):
        with pytest.raises(sqlite3.IntegrityError):
            await db.record_ephemeral_response(999999, "answer for nobody")

    async def test_custom_timestamp_respected(self, db):
        input_id = await db.record_ephemeral_input(
            "sess-ts2", "q", timestamp="2026-01-01T09:00:00"
        )
        await db.record_ephemeral_response(
            input_id, "a", timestamp="2026-01-01T09:00:05"
        )
        rows = await db.get_ephemeral_messages("sess-ts2")
        resp_row = next(r for r in rows if r["role"] == "assistant")
        assert resp_row["timestamp"] == "2026-01-01T09:00:05"


# ---------------------------------------------------------------------------
# get_ephemeral_messages
# ---------------------------------------------------------------------------


class TestGetEphemeralMessages:
    async def test_returns_empty_for_unknown_session(self, db):
        rows = await db.get_ephemeral_messages("no-such-session")
        assert rows == []

    async def test_returns_pair_in_timestamp_order(self, db):
        input_id = await db.record_ephemeral_input(
            "sess-ord", "user q", timestamp="2026-01-01T10:00:00"
        )
        await db.record_ephemeral_response(
            input_id, "assistant a", timestamp="2026-01-01T10:00:05"
        )
        rows = await db.get_ephemeral_messages("sess-ord")
        assert len(rows) == 2
        assert rows[0]["role"] == "user"
        assert rows[1]["role"] == "assistant"

    async def test_filters_by_session_id(self, db):
        id1 = await db.record_ephemeral_input("sess-A", "hello from A")
        await db.record_ephemeral_response(id1, "reply A")
        id2 = await db.record_ephemeral_input("sess-B", "hello from B")
        await db.record_ephemeral_response(id2, "reply B")

        rows_a = await db.get_ephemeral_messages("sess-A")
        rows_b = await db.get_ephemeral_messages("sess-B")
        assert all(r["session_id"] == "sess-A" for r in rows_a)
        assert all(r["session_id"] == "sess-B" for r in rows_b)

    async def test_multiple_exchanges_in_order(self, db):
        id1 = await db.record_ephemeral_input(
            "sess-multi", "q1", timestamp="2026-01-01T10:00:00"
        )
        await db.record_ephemeral_response(id1, "a1", timestamp="2026-01-01T10:00:05")
        id2 = await db.record_ephemeral_input(
            "sess-multi", "q2", timestamp="2026-01-01T10:01:00"
        )
        await db.record_ephemeral_response(id2, "a2", timestamp="2026-01-01T10:01:05")

        rows = await db.get_ephemeral_messages("sess-multi")
        assert len(rows) == 4
        contents = [r["content"] for r in rows]
        assert contents == ["q1", "a1", "q2", "a2"]


# ---------------------------------------------------------------------------
# FTS search
# ---------------------------------------------------------------------------


class TestSearchEphemeral:
    async def test_fts_finds_input_content(self, db):
        await db.record_ephemeral_input("sess-fts1", "xylophone music theory")
        hits = await db.search_ephemeral("xylophone")
        assert len(hits) >= 1
        assert any("xylophone" in h["content"] for h in hits)

    async def test_fts_finds_response_content(self, db):
        input_id = await db.record_ephemeral_input("sess-fts2", "boring user input")
        await db.record_ephemeral_response(input_id, "quetzalcoatl feathered serpent")
        hits = await db.search_ephemeral("quetzalcoatl")
        assert len(hits) >= 1
        assert any("quetzalcoatl" in h["content"] for h in hits)

    async def test_fts_returns_empty_for_nonexistent_term(self, db):
        await db.record_ephemeral_input("sess-fts3", "perfectly normal content")
        hits = await db.search_ephemeral("xyzzy_nonexistent_12345")
        assert hits == []

    async def test_fts_session_scoped_filter(self, db):
        await db.record_ephemeral_input("sess-fts-A", "unique_word_A_only")
        await db.record_ephemeral_input("sess-fts-B", "unique_word_B_only")

        hits_a = await db.search_ephemeral(
            "unique_word_A_only", session_id="sess-fts-A"
        )
        assert len(hits_a) == 1
        assert hits_a[0]["session_id"] == "sess-fts-A"

        hits_b_in_a = await db.search_ephemeral(
            "unique_word_B_only", session_id="sess-fts-A"
        )
        assert hits_b_in_a == []

    async def test_fts_unscoped_search_crosses_sessions(self, db):
        await db.record_ephemeral_input("sess-cross-1", "sharedterm content one")
        await db.record_ephemeral_input("sess-cross-2", "sharedterm content two")
        hits = await db.search_ephemeral("sharedterm")
        assert len(hits) == 2

    async def test_fts_empty_query_returns_empty(self, db):
        await db.record_ephemeral_input("sess-empty-q", "some content")
        hits = await db.search_ephemeral("")
        assert hits == []


# ---------------------------------------------------------------------------
# Trigger sync
# ---------------------------------------------------------------------------


class TestFtsTriggerSync:
    async def test_insert_trigger_indexes_content(self, db):
        """After INSERT, FTS must find the row immediately."""
        await db.record_ephemeral_input("sess-trig1", "triggerword_insert")
        hits = await db.search_ephemeral("triggerword_insert")
        assert len(hits) == 1

    async def test_update_trigger_reindexes_content(self, db):
        """After UPDATE content, FTS must find the new term and not the old one."""
        input_id = await db.record_ephemeral_input("sess-trig2", "old_triggerword")
        # Verify old term found
        assert len(await db.search_ephemeral("old_triggerword")) == 1

        # Direct UPDATE to test the UPDATE trigger
        await db._conn.execute(
            "UPDATE ephemeral_messages SET content = ? WHERE id = ?",
            ("new_triggerword_replacement", input_id),
        )
        await db._conn.commit()

        assert len(await db.search_ephemeral("new_triggerword_replacement")) == 1
        assert len(await db.search_ephemeral("old_triggerword")) == 0

    async def test_delete_trigger_removes_from_fts(self, db):
        """After DELETE, FTS must no longer find the row."""
        input_id = await db.record_ephemeral_input("sess-trig3", "deleteme_triggerword")
        assert len(await db.search_ephemeral("deleteme_triggerword")) == 1

        await db._conn.execute(
            "DELETE FROM ephemeral_messages WHERE id = ?", (input_id,)
        )
        await db._conn.commit()

        assert len(await db.search_ephemeral("deleteme_triggerword")) == 0

    async def test_insert_then_update_then_delete_cycle(self, db):
        """Full trigger lifecycle in one test."""
        # Insert
        input_id = await db.record_ephemeral_input("sess-cycle", "lifecycle_word_v1")
        assert len(await db.search_ephemeral("lifecycle_word_v1")) == 1

        # Update
        await db._conn.execute(
            "UPDATE ephemeral_messages SET content = ? WHERE id = ?",
            ("lifecycle_word_v2", input_id),
        )
        await db._conn.commit()
        assert len(await db.search_ephemeral("lifecycle_word_v1")) == 0
        assert len(await db.search_ephemeral("lifecycle_word_v2")) == 1

        # Delete
        await db._conn.execute(
            "DELETE FROM ephemeral_messages WHERE id = ?", (input_id,)
        )
        await db._conn.commit()
        assert len(await db.search_ephemeral("lifecycle_word_v2")) == 0


# ---------------------------------------------------------------------------
# Foreign key behaviour (sessions FK) — mirrors messages table behaviour
# ---------------------------------------------------------------------------


class TestSessionFk:
    async def test_fk_off_by_default_allows_orphan_insert(self, db):
        """With FK pragma off (the default), inserting an ephemeral row for a
        non-existent session must succeed — mirrors the existing messages table
        behaviour seen in test_db.py (project/session not pre-inserted)."""
        row_id = await db.record_ephemeral_input("nonexistent-session-id", "content")
        assert row_id > 0

    async def test_fk_on_blocks_orphan_insert(self, db_with_session):
        """When PRAGMA foreign_keys = ON, inserting with an unknown session_id
        must raise an IntegrityError."""
        db = db_with_session
        with pytest.raises(Exception) as exc_info:
            await db._conn.execute(
                "INSERT INTO ephemeral_messages "
                "(session_id, kind, role, content, timestamp) "
                "VALUES ('does-not-exist', 'btw', 'user', 'hi', '2026-01-01')"
            )
            await db._conn.commit()
        assert "FOREIGN KEY" in str(exc_info.value).upper() or isinstance(
            exc_info.value, sqlite3.IntegrityError
        )

    async def test_fk_on_allows_valid_session(self, db_with_session):
        """With FK pragma on, inserting with a real session_id must succeed."""
        db = db_with_session
        row_id = await db.record_ephemeral_input("sess-001", "hello from valid session")
        assert row_id > 0
