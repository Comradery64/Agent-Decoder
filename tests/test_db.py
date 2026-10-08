"""Tests for db.py — Agent 1 must make all of these pass."""

import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_decoder.models import Message, Project, Session, TextBlock, ToolUseBlock


@pytest.fixture
async def db():
    from agent_decoder.db import Database

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        async with Database(db_path) as database:
            await database.init_schema()
            yield database


@pytest.fixture
def sample_project():
    return Project(
        id="proj-test",
        display_name="Test Project",
        raw_path="-test-project",
        resolved_path="/test/project",
        data_source="~/.claude",
        session_count=0,
    )


@pytest.fixture
def sample_session():
    return Session(
        id="aaaaaaaa-0000-0000-0000-000000000001",
        project_id="proj-test",
        file_path="/tmp/test.jsonl",
        title="Test Session",
        model="claude-sonnet-4-6",
        started_at=datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, 10, 5, 0, tzinfo=timezone.utc),
        message_count=3,
        user_message_count=2,
        cwd="/home/user",
        git_branch="main",
    )


@pytest.fixture
def sample_messages():
    return [
        Message(
            id="msg-0001",
            session_id="aaaaaaaa-0000-0000-0000-000000000001",
            parent_id=None,
            role="user",
            content_blocks=[TextBlock(text="Hello!")],
            timestamp=datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
        ),
        Message(
            id="msg-0002",
            session_id="aaaaaaaa-0000-0000-0000-000000000001",
            parent_id="msg-0001",
            role="assistant",
            content_blocks=[TextBlock(text="Hi! How can I help you today?")],
            timestamp=datetime(2026, 1, 1, 10, 0, 5, tzinfo=timezone.utc),
            model="claude-sonnet-4-6",
        ),
    ]


class TestSchema:
    async def test_init_schema_is_idempotent(self, db):
        await db.init_schema()  # Second call should not raise
        await db.init_schema()


class TestProjects:
    async def test_upsert_and_get_project(self, db, sample_project):
        await db.upsert_project(sample_project)
        projects = await db.get_projects()
        assert len(projects) == 1
        assert projects[0].id == "proj-test"
        assert projects[0].display_name == "Test Project"

    async def test_upsert_project_updates_existing(self, db, sample_project):
        await db.upsert_project(sample_project)
        updated = sample_project.model_copy(update={"display_name": "Updated"})
        await db.upsert_project(updated)
        projects = await db.get_projects()
        assert len(projects) == 1
        assert projects[0].display_name == "Updated"

    async def test_get_projects_empty(self, db):
        projects = await db.get_projects()
        assert projects == []


class TestSessions:
    async def test_upsert_and_get_session(self, db, sample_project, sample_session):
        await db.upsert_project(sample_project)
        await db.upsert_session(sample_session)
        sessions = await db.get_sessions()
        assert len(sessions) == 1
        assert sessions[0].id == sample_session.id

    async def test_get_sessions_filtered_by_project(
        self, db, sample_project, sample_session
    ):
        await db.upsert_project(sample_project)
        await db.upsert_session(sample_session)
        sessions = await db.get_sessions(project_id="proj-test")
        assert len(sessions) == 1
        sessions_other = await db.get_sessions(project_id="other-proj")
        assert len(sessions_other) == 0

    async def test_session_mtime_roundtrip(self, db, sample_project, sample_session):
        await db.upsert_project(sample_project)
        session_with_mtime = sample_session.model_copy()
        await db.upsert_session(session_with_mtime)
        # mtime tracking is internal — just verify we can get None for missing
        result = await db.get_session_mtime("nonexistent-session")
        assert result is None


class TestMessages:
    async def test_upsert_messages_and_get_detail(
        self, db, sample_project, sample_session, sample_messages
    ):
        await db.upsert_project(sample_project)
        await db.upsert_session(sample_session)
        await db.upsert_messages(sample_messages)
        detail = await db.get_session_detail(sample_session.id)
        assert detail is not None
        assert len(detail.messages) == 2

    async def test_get_session_detail_returns_none_for_missing(self, db):
        detail = await db.get_session_detail("nonexistent-id")
        assert detail is None

    async def test_cwd_exists_true_when_cwd_dir_exists_on_disk(
        self, db, sample_project, sample_session, sample_messages, tmp_path
    ):
        """``cwd_exists`` is computed via a live ``Path.is_dir()`` check at
        request time, not from any cached DB column. A session whose cwd
        points at a real directory reports True."""
        real_cwd_session = sample_session.model_copy(update={"cwd": str(tmp_path)})
        await db.upsert_project(sample_project)
        await db.upsert_session(real_cwd_session)
        await db.upsert_messages(sample_messages)
        detail = await db.get_session_detail(real_cwd_session.id)
        assert detail is not None
        assert detail.cwd_exists is True

    async def test_cwd_exists_false_when_cwd_dir_missing(
        self, db, sample_project, sample_session, sample_messages, tmp_path
    ):
        """A session whose cwd points at a path that doesn't exist on disk
        reports False — without trusting any DB cache that may have been
        stale from a previous scan."""
        gone_cwd_session = sample_session.model_copy(
            update={"cwd": str(tmp_path / "deleted-dir-that-does-not-exist")}
        )
        await db.upsert_project(sample_project)
        await db.upsert_session(gone_cwd_session)
        await db.upsert_messages(sample_messages)
        detail = await db.get_session_detail(gone_cwd_session.id)
        assert detail is not None
        assert detail.cwd_exists is False

    async def test_cwd_exists_true_when_session_cwd_is_null(
        self, db, sample_project, sample_session, sample_messages
    ):
        """Legacy / corrupted sessions with no cwd default to True so we
        don't false-positive an undeliverable banner on them."""
        no_cwd_session = sample_session.model_copy(update={"cwd": None})
        await db.upsert_project(sample_project)
        await db.upsert_session(no_cwd_session)
        await db.upsert_messages(sample_messages)
        detail = await db.get_session_detail(no_cwd_session.id)
        assert detail is not None
        assert detail.cwd_exists is True

    async def test_messages_ordered_by_timestamp(
        self, db, sample_project, sample_session, sample_messages
    ):
        await db.upsert_project(sample_project)
        await db.upsert_session(sample_session)
        await db.upsert_messages(sample_messages)
        detail = await db.get_session_detail(sample_session.id)
        timestamps = [m.timestamp for m in detail.messages if m.timestamp]
        assert timestamps == sorted(timestamps)


class TestMergeMessages:
    """merge_messages backs sub-agent transcript ingest (include_subagent_chats):
    it must add messages to a session WITHOUT disturbing that session's other,
    already-indexed messages — unlike upsert_messages, which does a
    session-scoped delete + reinsert of FTS/message_blocks rows."""

    async def test_merge_does_not_wipe_existing_messages_fts(
        self, db, sample_project, sample_session, sample_messages
    ):
        await db.upsert_project(sample_project)
        await db.upsert_session(sample_session)
        await db.upsert_messages(sample_messages)

        subagent_message = Message(
            id="side-0001",
            session_id=sample_session.id,
            parent_id="unresolvable-elsewhere",
            role="assistant",
            content_blocks=[TextBlock(text="Sub-agent reply about widgets")],
            is_sidechain=True,
            source_tool_assistant_uuid="msg-0002",
        )
        await db.merge_messages([subagent_message])

        # Original messages are still searchable — merge_messages must not
        # have deleted their FTS rows via a session-scoped DELETE.
        hits = await db.search("Hi! How can I help", project_id=None, limit=10)
        assert any(h.session_id == sample_session.id for h in hits)

        # And the new message is present too.
        detail = await db.get_session_detail(sample_session.id)
        ids = {m.id for m in detail.messages}
        assert {"msg-0001", "msg-0002", "side-0001"} <= ids

    async def test_merge_is_idempotent(
        self, db, sample_project, sample_session, sample_messages
    ):
        await db.upsert_project(sample_project)
        await db.upsert_session(sample_session)
        await db.upsert_messages(sample_messages)

        subagent_message = Message(
            id="side-0001",
            session_id=sample_session.id,
            parent_id=None,
            role="user",
            content_blocks=[TextBlock(text="repeat me")],
            is_sidechain=True,
        )
        await db.merge_messages([subagent_message])
        await db.merge_messages([subagent_message])

        detail = await db.get_session_detail(sample_session.id)
        assert sum(1 for m in detail.messages if m.id == "side-0001") == 1


class TestFindMessageByToolUseId:
    async def test_finds_assistant_message_containing_tool_use_block(
        self, db, sample_project, sample_session
    ):
        await db.upsert_project(sample_project)
        await db.upsert_session(sample_session)
        assistant_msg = Message(
            id="main-0002",
            session_id=sample_session.id,
            parent_id="main-0001",
            role="assistant",
            content_blocks=[
                ToolUseBlock(id="toolu_agent123", name="Task", input={}),
            ],
        )
        await db.upsert_messages([assistant_msg])

        found = await db.find_message_by_tool_use_id(
            sample_session.id, "toolu_agent123"
        )
        assert found == "main-0002"

    async def test_returns_none_when_tool_use_id_not_found(
        self, db, sample_project, sample_session
    ):
        await db.upsert_project(sample_project)
        await db.upsert_session(sample_session)
        found = await db.find_message_by_tool_use_id(
            sample_session.id, "toolu_does_not_exist"
        )
        assert found is None


class TestSearch:
    async def test_search_finds_content(
        self, db, sample_project, sample_session, sample_messages
    ):
        await db.upsert_project(sample_project)
        await db.upsert_session(sample_session)
        await db.upsert_messages(sample_messages)
        hits = await db.search("Hello")
        assert len(hits) >= 1
        assert any("Hello" in h.snippet or "hello" in h.snippet.lower() for h in hits)

    async def test_search_no_results_for_missing_term(
        self, db, sample_project, sample_session, sample_messages
    ):
        await db.upsert_project(sample_project)
        await db.upsert_session(sample_session)
        await db.upsert_messages(sample_messages)
        hits = await db.search("xyzzy_nonexistent_term_12345")
        assert hits == []

    async def test_search_filtered_by_project(
        self, db, sample_project, sample_session, sample_messages
    ):
        await db.upsert_project(sample_project)
        await db.upsert_session(sample_session)
        await db.upsert_messages(sample_messages)
        hits = await db.search("Hello", project_id="proj-test")
        assert len(hits) >= 1
        hits_wrong = await db.search("Hello", project_id="wrong-project")
        assert hits_wrong == []

    async def test_search_offset_pages_through_older_matches(
        self, db, sample_project, sample_session
    ):
        """A page fetched with `limit` + `offset` must reach matches older
        than the first `limit` most-recent rows, not just re-truncate to the
        newest ones (the bug behind "search only goes back a couple days")."""
        await db.upsert_project(sample_project)
        await db.upsert_session(sample_session)
        messages = [
            Message(
                id=f"msg-page-{i:04d}",
                session_id=sample_session.id,
                parent_id=None,
                role="user",
                content_blocks=[TextBlock(text=f"needle occurrence {i}")],
                timestamp=datetime(2026, 1, 1, 10, 0, i, tzinfo=timezone.utc),
            )
            for i in range(5)
        ]
        await db.upsert_messages(messages)

        page1 = await db.search("needle", limit=2, offset=0)
        page2 = await db.search("needle", limit=2, offset=2)
        page3 = await db.search("needle", limit=2, offset=4)

        assert [h.message_id for h in page1] == ["msg-page-0004", "msg-page-0003"]
        assert [h.message_id for h in page2] == ["msg-page-0002", "msg-page-0001"]
        assert [h.message_id for h in page3] == ["msg-page-0000"]


class TestStats:
    async def test_stats_counts(
        self, db, sample_project, sample_session, sample_messages
    ):
        await db.upsert_project(sample_project)
        await db.upsert_session(sample_session)
        await db.upsert_messages(sample_messages)
        stats = await db.get_stats()
        assert stats.total_projects == 1
        assert stats.total_sessions == 1
        assert stats.total_messages == 2

    async def test_stats_empty_db(self, db):
        stats = await db.get_stats()
        assert stats.total_projects == 0
        assert stats.total_sessions == 0
        assert stats.total_messages == 0


class TestUsagePersistence:
    async def test_upsert_message_with_usage(self, db, sample_project, sample_session):
        from agent_decoder.models import TokenUsage

        await db.upsert_project(sample_project)
        await db.upsert_session(sample_session)
        msg = Message(
            id="msg-usage-001",
            session_id=sample_session.id,
            role="assistant",
            content_blocks=[TextBlock(text="Hi")],
            timestamp=datetime(2026, 1, 1, 10, 0, 5, tzinfo=timezone.utc),
            usage=TokenUsage(
                input_tokens=10,
                output_tokens=5,
                cache_creation_input_tokens=100,
                cache_read_input_tokens=50,
            ),
        )
        await db.upsert_messages([msg])
        detail = await db.get_session_detail(sample_session.id)
        stored = next(m for m in detail.messages if m.id == "msg-usage-001")
        assert stored.usage is not None
        assert stored.usage.input_tokens == 10
        assert stored.usage.output_tokens == 5
        assert stored.usage.cache_creation_input_tokens == 100
        assert stored.usage.cache_read_input_tokens == 50

    async def test_upsert_message_without_usage_returns_none(
        self, db, sample_project, sample_session
    ):
        await db.upsert_project(sample_project)
        await db.upsert_session(sample_session)
        msg = Message(
            id="msg-no-usage",
            session_id=sample_session.id,
            role="user",
            content_blocks=[TextBlock(text="Hello")],
            timestamp=datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
        )
        await db.upsert_messages([msg])
        detail = await db.get_session_detail(sample_session.id)
        stored = next(m for m in detail.messages if m.id == "msg-no-usage")
        assert stored.usage is None


class TestPhase0Integration:
    async def test_full_pipeline_usage_fixture(self, db):
        """Parse session_with_usage.jsonl → DB → retrieve, assert tokens preserved."""
        from agent_decoder.parser import parse_session
        from agent_decoder.models import Project

        fixture = Path(__file__).parent / "fixtures" / "session_with_usage.jsonl"
        project = Project(
            id="test-proj", display_name="Test", raw_path="-test", data_source="test"
        )
        session, messages = parse_session(fixture)
        session.project_id = project.id
        await db.upsert_project(project)
        await db.upsert_session(session)
        await db.upsert_messages(messages)

        detail = await db.get_session_detail(session.id)
        assistant_msgs = [m for m in detail.messages if m.role == "assistant"]
        assert len(assistant_msgs) == 2
        total_input = sum(m.usage.input_tokens for m in assistant_msgs if m.usage)
        assert total_input == 32  # 12 + 20
        total_cache_read = sum(
            m.usage.cache_read_input_tokens for m in assistant_msgs if m.usage
        )
        assert total_cache_read == 50


class TestDeleteMessage:
    async def test_delete_removes_message(self, db, sample_messages):
        project = Project(
            id="p1",
            display_name="p",
            raw_path="/",
            resolved_path="/",
            data_source="local",
        )
        session = Session(
            id=sample_messages[0].session_id, project_id="p1", file_path="/f.jsonl"
        )
        await db.upsert_project(project)
        await db.upsert_session(session)
        await db.upsert_messages(sample_messages[:2])
        target_id = sample_messages[0].id
        await db.delete_message(target_id)
        detail = await db.get_session_detail(session.id)
        assert all(m.id != target_id for m in detail.messages)

    async def test_delete_updates_session_message_count(self, db, sample_messages):
        project = Project(
            id="p1",
            display_name="p",
            raw_path="/",
            resolved_path="/",
            data_source="local",
        )
        session = Session(
            id=sample_messages[0].session_id,
            project_id="p1",
            file_path="/f.jsonl",
            title="Test",
        )
        await db.upsert_project(project)
        await db.upsert_session(session)
        await db.upsert_messages(sample_messages[:2])
        await db.delete_message(sample_messages[0].id)
        sessions = await db.get_sessions(project_id="p1")
        assert sessions[0].message_count == 1

    async def test_delete_nonexistent_is_noop(self, db):
        await db.delete_message("nonexistent-uuid-0000-0000-000000000000")


class TestUpdateMessageContent:
    async def test_update_changes_content(self, db, sample_messages):
        project = Project(
            id="p1",
            display_name="p",
            raw_path="/",
            resolved_path="/",
            data_source="local",
        )
        session = Session(
            id=sample_messages[0].session_id, project_id="p1", file_path="/f.jsonl"
        )
        await db.upsert_project(project)
        await db.upsert_session(session)
        await db.upsert_messages([sample_messages[0]])
        new_blocks = [TextBlock(text="updated content")]
        await db.update_message_content(sample_messages[0].id, new_blocks)
        detail = await db.get_session_detail(session.id)
        assert detail.messages[0].content_blocks[0].text == "updated content"

    async def test_update_nonexistent_is_noop(self, db):
        await db.update_message_content(
            "nonexistent-0000-0000-0000-000000000000", [TextBlock(text="x")]
        )


class TestGetSessionFilePathForMessage:
    async def test_returns_file_path(self, db, sample_messages):
        project = Project(
            id="p1",
            display_name="p",
            raw_path="/",
            resolved_path="/",
            data_source="local",
        )
        session = Session(
            id=sample_messages[0].session_id,
            project_id="p1",
            file_path="/sessions/test.jsonl",
        )
        await db.upsert_project(project)
        await db.upsert_session(session)
        await db.upsert_messages([sample_messages[0]])
        path = await db.get_session_file_path_for_message(sample_messages[0].id)
        assert path == "/sessions/test.jsonl"

    async def test_returns_none_for_unknown_message(self, db):
        path = await db.get_session_file_path_for_message(
            "unknown-0000-0000-0000-000000000000"
        )
        assert path is None


# ---------------------------------------------------------------------------
# Session flags — archived / starred / viewed (server-backed)
#
# These were originally localStorage-only on the frontend (LS.ARCHIVED,
# LS.STARRED, LS.VIEWED_AT), making them invisible across browsers.
# ---------------------------------------------------------------------------


class TestSessionFlags:
    async def test_set_archived_round_trips(self, db, sample_project, sample_session):
        await db.upsert_project(sample_project)
        await db.upsert_session(sample_session)
        sid = sample_session.id

        # Initially unset.
        meta = await db.get_session_meta(sid)
        assert meta["archived_at"] is None

        ts = await db.set_archived(sid, True)
        assert ts is not None and len(ts) > 0
        meta = await db.get_session_meta(sid)
        assert meta["archived_at"] == ts

        cleared = await db.set_archived(sid, False)
        assert cleared is None
        meta = await db.get_session_meta(sid)
        assert meta["archived_at"] is None

    async def test_set_starred_round_trips(self, db, sample_project, sample_session):
        await db.upsert_project(sample_project)
        await db.upsert_session(sample_session)
        sid = sample_session.id
        ts = await db.set_starred(sid, True)
        assert ts is not None
        meta = await db.get_session_meta(sid)
        assert meta["starred_at"] == ts
        await db.set_starred(sid, False)
        meta = await db.get_session_meta(sid)
        assert meta["starred_at"] is None

    async def test_set_viewed_at_accepts_explicit_timestamp(
        self, db, sample_project, sample_session
    ):
        await db.upsert_project(sample_project)
        await db.upsert_session(sample_session)
        sid = sample_session.id
        # Caller supplies the timestamp (typically message_updated_at) so the
        # bell-dismiss logic can compare exact wall-clocks.
        ts = "2026-05-28T15:00:00"
        stored = await db.set_viewed_at(sid, ts)
        assert stored == ts
        meta = await db.get_session_meta(sid)
        assert meta["viewed_at"] == ts

        # Clearing.
        cleared = await db.set_viewed_at(sid, None)
        assert cleared is None
        meta = await db.get_session_meta(sid)
        assert meta["viewed_at"] is None

    async def test_flags_are_independent(self, db, sample_project, sample_session):
        """Setting archived must not clobber starred / custom_title / viewed_at,
        and vice versa — each upsert touches only its own column."""
        await db.upsert_project(sample_project)
        await db.upsert_session(sample_session)
        sid = sample_session.id

        await db.set_custom_title(sid, "my title")
        await db.set_archived(sid, True)
        await db.set_starred(sid, True)
        await db.set_viewed_at(sid, "2026-05-28T10:00:00")

        meta = await db.get_session_meta(sid)
        assert meta["custom_title"] == "my title"
        assert meta["archived_at"] is not None
        assert meta["starred_at"] is not None
        assert meta["viewed_at"] == "2026-05-28T10:00:00"

        # Toggling one field must leave the others intact.
        await db.set_archived(sid, False)
        meta = await db.get_session_meta(sid)
        assert meta["archived_at"] is None
        assert meta["custom_title"] == "my title"
        assert meta["starred_at"] is not None
        assert meta["viewed_at"] == "2026-05-28T10:00:00"

    async def test_flags_project_into_session_list(
        self, db, sample_project, sample_session
    ):
        """Database.get_sessions includes the flags via the LEFT JOIN so the
        FE gets them on first paint without a separate request."""
        await db.upsert_project(sample_project)
        await db.upsert_session(sample_session)
        sid = sample_session.id
        await db.set_archived(sid, True)
        await db.set_starred(sid, True)
        await db.set_viewed_at(sid, "2026-05-28T11:00:00")

        sessions = await db.get_sessions()
        target = next((s for s in sessions if s.id == sid), None)
        assert target is not None, "session must appear in list"
        assert target.archived_at is not None, (
            "archived_at must be projected from session_meta into the list payload"
        )
        assert target.starred_at is not None
        assert target.viewed_at == "2026-05-28T11:00:00"

    async def test_unsupported_column_rejected(
        self, db, sample_project, sample_session
    ):
        await db.upsert_project(sample_project)
        await db.upsert_session(sample_session)
        with pytest.raises(ValueError):
            await db._set_meta_flag(sample_session.id, "evil_column", "x")

    async def test_get_session_meta_returns_default_dict_when_missing(self, db):
        """No session_meta row → returns a dict with all-None values rather
        than raising or returning None.  Lets callers branch on field truthiness."""
        meta = await db.get_session_meta("nonexistent-sid")
        assert meta == {
            "custom_title": None,
            "archived_at": None,
            "starred_at": None,
            "viewed_at": None,
        }

    async def test_migration_idempotent(self, db):
        """Running init_schema twice must not error or duplicate columns."""
        await db.init_schema()
        await db.init_schema()
        # Should still work after re-init.
        meta = await db.get_session_meta("any-sid")
        assert meta["archived_at"] is None


# ---------------------------------------------------------------------------
# Provider column migration tests
# ---------------------------------------------------------------------------


class TestProviderMigration:
    async def test_provider_column_exists_on_fresh_schema(self, db):
        """A freshly initialised DB must have exactly one provider column."""
        async with db._conn.execute(
            "SELECT COUNT(*) FROM pragma_table_info('sessions') WHERE name='provider'"
        ) as cur:
            row = await cur.fetchone()
        assert row[0] == 1, "sessions table should have exactly one 'provider' column"

    async def test_init_schema_twice_no_duplicate_provider_column(self, db):
        """Running init_schema twice must not create duplicate provider columns."""
        await db.init_schema()  # second call — first already ran inside fixture
        async with db._conn.execute(
            "SELECT COUNT(*) FROM pragma_table_info('sessions') WHERE name='provider'"
        ) as cur:
            row = await cur.fetchone()
        assert row[0] == 1, (
            "provider column must not be duplicated by idempotent re-init"
        )

    async def test_migrate_add_provider_on_old_db_without_column(self, tmp_path):
        """Simulate an old DB that pre-dates the provider column.

        Steps:
          1. Create a minimal sessions table WITHOUT provider.
          2. Insert a row so we can assert the DEFAULT kicks in.
          3. Run _migrate_add_provider().
          4. Verify the column now exists and the pre-existing row defaults to 'claude'.
        """
        import aiosqlite

        from agent_decoder.db import Database

        db_path = tmp_path / "old.db"
        async with aiosqlite.connect(db_path) as conn:
            conn.row_factory = aiosqlite.Row
            # Minimal sessions table — no provider column, mirrors the pre-migration shape.
            await conn.execute("""
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    title TEXT,
                    model TEXT,
                    started_at TEXT,
                    updated_at TEXT,
                    message_count INTEGER NOT NULL DEFAULT 0,
                    user_message_count INTEGER NOT NULL DEFAULT 0,
                    cwd TEXT,
                    git_branch TEXT,
                    is_worktree INTEGER NOT NULL DEFAULT 0,
                    is_fork INTEGER NOT NULL DEFAULT 0,
                    permission_mode TEXT,
                    file_mtime REAL
                )
            """)
            await conn.execute(
                "INSERT INTO sessions (id, project_id, file_path) VALUES (?, ?, ?)",
                ("old-session-1", "proj-old", "/tmp/old.jsonl"),
            )
            await conn.commit()

        # Open via Database and run only the targeted migration method.
        async with Database(db_path) as database:
            # Run migration — should add the column.
            await database._migrate_add_provider()

            # Column must now be present.
            async with database._conn.execute(
                "SELECT COUNT(*) FROM pragma_table_info('sessions') WHERE name='provider'"
            ) as cur:
                row = await cur.fetchone()
            assert row[0] == 1, "provider column should be added by migration"

            # Pre-existing row must default to 'claude'.
            async with database._conn.execute(
                "SELECT provider FROM sessions WHERE id = 'old-session-1'"
            ) as cur:
                row = await cur.fetchone()
            assert row["provider"] == "claude", (
                "pre-existing row should default to 'claude' after migration"
            )

            # Running again must be idempotent (no error, no duplicate).
            await database._migrate_add_provider()
            async with database._conn.execute(
                "SELECT COUNT(*) FROM pragma_table_info('sessions') WHERE name='provider'"
            ) as cur:
                row = await cur.fetchone()
            assert row[0] == 1

    async def test_provider_roundtrips_through_upsert(
        self, db, sample_project, sample_session
    ):
        """provider='claude' stored via upsert_session comes back from _row_to_session."""
        await db.upsert_project(sample_project)
        await db.upsert_session(sample_session)
        sessions = await db.get_sessions()
        assert len(sessions) == 1
        assert sessions[0].provider == "claude"


class TestIncrementalUpsert:
    """upsert_messages rewrites only rows whose content actually changed,
    leaving unchanged rows (and their rowids) untouched."""

    async def _rows(self, db, session_id):
        async with db._conn.execute(
            "SELECT id, rowid, content_hash FROM messages WHERE session_id=?",
            (session_id,),
        ) as cur:
            return {
                r["id"]: (r["rowid"], r["content_hash"]) for r in await cur.fetchall()
            }

    async def _fts(self, db, session_id):
        async with db._conn.execute(
            "SELECT message_id, content FROM messages_fts WHERE session_id=?",
            (session_id,),
        ) as cur:
            return {r["message_id"]: r["content"] for r in await cur.fetchall()}

    async def test_initial_index_populates_hash_and_fts(
        self, db, sample_session, sample_messages
    ):
        await db.upsert_session(sample_session)
        await db.upsert_messages(sample_messages)
        rows = await self._rows(db, sample_session.id)
        assert set(rows) == {"msg-0001", "msg-0002"}
        assert all(h for _, h in rows.values()), "content_hash must be backfilled"
        assert "Hello" in (await self._fts(db, sample_session.id))["msg-0001"]

    async def test_noop_reparse_rewrites_nothing(
        self, db, sample_session, sample_messages
    ):
        await db.upsert_session(sample_session)
        await db.upsert_messages(sample_messages)
        before = await self._rows(db, sample_session.id)
        await db.upsert_messages(sample_messages)  # identical re-parse
        assert await self._rows(db, sample_session.id) == before  # same rowids + hashes

    async def test_changed_row_updates_fts_but_keeps_sibling_rowid(
        self, db, sample_session, sample_messages
    ):
        await db.upsert_session(sample_session)
        await db.upsert_messages(sample_messages)
        before = await self._rows(db, sample_session.id)
        edited = list(sample_messages)
        edited[1] = edited[1].model_copy(
            update={"content_blocks": [TextBlock(text="Edited reply about penguins")]}
        )
        await db.upsert_messages(edited)
        after = await self._rows(db, sample_session.id)
        # unchanged sibling keeps its rowid (ordering stays stable)…
        assert after["msg-0001"] == before["msg-0001"]
        # …changed row's hash moved and its FTS reflects the new text only
        assert after["msg-0002"][1] != before["msg-0002"][1]
        fts = await self._fts(db, sample_session.id)
        assert "penguins" in fts["msg-0002"]
        assert "help you today" not in fts["msg-0002"]

    async def test_appended_message_is_added(self, db, sample_session, sample_messages):
        await db.upsert_session(sample_session)
        await db.upsert_messages(sample_messages)
        extra = Message(
            id="msg-0003",
            session_id=sample_session.id,
            parent_id="msg-0002",
            role="user",
            content_blocks=[TextBlock(text="Follow-up question")],
            timestamp=datetime(2026, 1, 1, 10, 1, 0, tzinfo=timezone.utc),
        )
        await db.upsert_messages(sample_messages + [extra])
        assert "msg-0003" in await self._rows(db, sample_session.id)

    async def test_removed_message_and_its_fts_are_deleted(
        self, db, sample_session, sample_messages
    ):
        await db.upsert_session(sample_session)
        await db.upsert_messages(sample_messages)
        await db.upsert_messages(sample_messages[:1])  # drop msg-0002
        assert set(await self._rows(db, sample_session.id)) == {"msg-0001"}
        assert "msg-0002" not in await self._fts(db, sample_session.id)

    async def test_token_backfill_triggers_rewrite(
        self, db, sample_session, sample_messages
    ):
        """Same content, backfilled usage → hash changes → row is rewritten."""
        from agent_decoder.models import TokenUsage

        await db.upsert_session(sample_session)
        await db.upsert_messages(sample_messages)
        before = await self._rows(db, sample_session.id)
        edited = list(sample_messages)
        edited[1] = edited[1].model_copy(
            update={
                "usage": TokenUsage(
                    input_tokens=10,
                    output_tokens=20,
                    cache_creation_input_tokens=0,
                    cache_read_input_tokens=0,
                )
            }
        )
        await db.upsert_messages(edited)
        after = await self._rows(db, sample_session.id)
        assert after["msg-0002"][1] != before["msg-0002"][1]  # hash moved
        async with db._conn.execute(
            "SELECT output_tokens FROM messages WHERE id='msg-0002'"
        ) as cur:
            assert (await cur.fetchone())[0] == 20


async def _fts_rowids(db) -> set:
    async with db._conn.execute("SELECT rowid FROM messages_fts") as cur:
        return {r[0] for r in await cur.fetchall()}


async def _map_rowids(db) -> set:
    async with db._conn.execute("SELECT fts_rowid FROM messages_fts_map") as cur:
        return {r[0] for r in await cur.fetchall()}


class TestFtsRowidMap:
    """messages_fts_map exists purely so session/message-scoped FTS deletes are
    rowid seeks instead of full scans of the FTS table (which has no secondary
    indexes). If the two tables ever drift, deletes silently start missing rows
    and stale hits linger in search — so every write path must keep them equal."""

    async def test_map_matches_fts_across_every_write_path(
        self, db, sample_project, sample_session, sample_messages
    ):
        await db.upsert_project(sample_project)
        await db.upsert_session(sample_session)

        await db.upsert_messages(sample_messages)
        assert await _fts_rowids(db) == await _map_rowids(db) != set()

        # Re-upsert (the hot path: every JSONL append re-indexes the file).
        await db.upsert_messages(sample_messages)
        assert await _fts_rowids(db) == await _map_rowids(db)

        await db.update_message_content(
            "msg-0001", [TextBlock(text="Edited content zzyzx")]
        )
        assert await _fts_rowids(db) == await _map_rowids(db)
        assert [h.message_id for h in await db.search("zzyzx")] == ["msg-0001"]

        await db.delete_message("msg-0001")
        assert await _fts_rowids(db) == await _map_rowids(db)
        assert await db.search("zzyzx") == []

        await db.delete_session_messages(sample_session.id)
        assert await _fts_rowids(db) == await _map_rowids(db) == set()

    async def test_allocated_rowids_never_collide_with_a_live_row(
        self, db, sample_project, sample_session, sample_messages
    ):
        """Explicit rowids are only safe because they come from MAX + 1. If a
        second session's insert landed on rowids the first session still owns,
        the insert would either fail or overwrite live search content."""
        await db.upsert_project(sample_project)
        await db.upsert_session(sample_session)
        await db.upsert_messages(sample_messages)
        held = await _fts_rowids(db)

        other = sample_session.model_copy(
            update={"id": "bbbbbbbb-0000-0000-0000-000000000002"}
        )
        await db.upsert_session(other)
        await db.upsert_messages(
            [
                Message(
                    id="msg-other-1",
                    session_id=other.id,
                    parent_id=None,
                    role="user",
                    content_blocks=[TextBlock(text="Second session content")],
                )
            ]
        )
        new = await _fts_rowids(db) - held
        assert new and not (new & held)
        assert await _fts_rowids(db) == await _map_rowids(db)

        # The first session's rows survived intact.
        assert [h.session_id for h in await db.search("Hi! How can I help")] == [
            sample_session.id
        ]

    async def test_duplicate_message_ids_in_one_batch_stay_in_sync(
        self, db, sample_project, sample_session, sample_messages
    ):
        """A resumed/repaired JSONL can repeat a message uuid. messages dedupes
        via INSERT OR REPLACE; the FTS pair must dedupe the same way or the map
        (keyed on message_id) ends up one row short of the FTS table."""
        await db.upsert_project(sample_project)
        await db.upsert_session(sample_session)
        dupe = sample_messages[0].model_copy(
            update={"content_blocks": [TextBlock(text="Later duplicate wins")]}
        )
        await db.upsert_messages([*sample_messages, dupe])

        assert await _fts_rowids(db) == await _map_rowids(db)
        assert [h.message_id for h in await db.search("Later duplicate wins")] == [
            "msg-0001"
        ]
        assert await db.search("Hello") == []

    async def test_migration_backfills_a_legacy_index(
        self, db, sample_project, sample_session, sample_messages
    ):
        """A DB written before the map existed has FTS rows and no map rows.
        migrate_fts_map_v1 must adopt them rather than orphan them."""
        await db.upsert_project(sample_project)
        await db.upsert_session(sample_session)
        await db.upsert_messages(sample_messages)

        # Rewind to the legacy shape: FTS rows intact, map empty, flag unset.
        await db._conn.execute("DELETE FROM messages_fts_map")
        await db._conn.execute("DELETE FROM _meta WHERE key = 'fts_map_v1'")
        await db._conn.commit()
        assert await _map_rowids(db) == set()

        await db.migrate_fts_map_v1()
        assert await _fts_rowids(db) == await _map_rowids(db) != set()

        # The adopted rows are now deletable through the indexed path.
        await db.delete_session_messages(sample_session.id)
        assert await _fts_rowids(db) == set()
        assert await db.search("Hello") == []

    async def test_migration_is_idempotent(self, db, sample_messages):
        await db.migrate_fts_map_v1()
        await db.migrate_fts_map_v1()

    async def test_session_scoped_delete_is_an_index_seek(self, db):
        """Regression guard on the whole point of this table: if the planner
        stops using idx_fts_map_session, the delete is a full FTS scan again."""
        async with db._conn.execute(
            "EXPLAIN QUERY PLAN DELETE FROM messages_fts WHERE rowid IN "
            "(SELECT fts_rowid FROM messages_fts_map WHERE session_id = ?)",
            ("any",),
        ) as cur:
            plan = " ".join(str(r["detail"]) for r in await cur.fetchall())
        assert "idx_fts_map_session" in plan
