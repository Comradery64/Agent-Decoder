"""Tests for ephemeral-aware JSON and Markdown exports.

Covers:
- JSON export includes an "ephemerals" array with expected rows
- Markdown export contains the ephemeral marker block for each pair
- Sessions with no ephemerals export with empty array (JSON) / no markers (Markdown)
- Unpaired ephemerals (user row only) export gracefully without crashing
- Ephemeral pairs interleave at the right position by timestamp
- API export routes pass ephemerals through correctly
"""

from datetime import datetime, timezone
from pathlib import Path


from agent_decoder.db import Database
from agent_decoder.models import Message, SessionDetail, TextBlock, TokenUsage
from agent_decoder.reporter import export_json, export_markdown


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_detail(
    session_id: str = "s-export-001",
    messages: list[Message] | None = None,
) -> SessionDetail:
    if messages is None:
        messages = [
            Message(
                id="u1",
                session_id=session_id,
                role="user",
                content_blocks=[TextBlock(text="Hello")],
                timestamp=datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
            ),
            Message(
                id="a1",
                session_id=session_id,
                role="assistant",
                content_blocks=[TextBlock(text="Hi there")],
                timestamp=datetime(2026, 1, 1, 10, 0, 5, tzinfo=timezone.utc),
                usage=TokenUsage(input_tokens=10, output_tokens=5),
            ),
        ]
    return SessionDetail(
        id=session_id,
        project_id="proj-exp",
        file_path="/tmp/test.jsonl",
        title="Export Test Session",
        model="claude-sonnet-4-6",
        started_at=datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, 10, 1, tzinfo=timezone.utc),
        message_count=len(messages),
        cwd="/home/user/project",
        git_branch="main",
        messages=messages,
    )


def _make_ephemerals(
    input_ts: str = "2026-01-01T10:00:30", response_ts: str = "2026-01-01T10:00:35"
) -> list[dict]:
    return [
        {
            "id": 1,
            "session_id": "s-export-001",
            "kind": "btw",
            "role": "user",
            "content": "btw can you check this?",
            "responds_to": None,
            "timestamp": input_ts,
        },
        {
            "id": 2,
            "session_id": "s-export-001",
            "kind": "btw",
            "role": "assistant",
            "content": "Sure, looks fine!",
            "responds_to": 1,
            "timestamp": response_ts,
        },
    ]


# ---------------------------------------------------------------------------
# JSON export tests
# ---------------------------------------------------------------------------


class TestExportJsonEphemerals:
    def test_json_export_includes_ephemerals_key(self):
        detail = _make_detail()
        ephemerals = _make_ephemerals()
        result = export_json(detail, ephemerals=ephemerals)
        assert "ephemerals" in result

    def test_json_export_ephemerals_contains_expected_rows(self):
        detail = _make_detail()
        ephemerals = _make_ephemerals()
        result = export_json(detail, ephemerals=ephemerals)
        assert len(result["ephemerals"]) == 2
        user_row = next(r for r in result["ephemerals"] if r["role"] == "user")
        assert user_row["content"] == "btw can you check this?"
        assert user_row["kind"] == "btw"
        resp_row = next(r for r in result["ephemerals"] if r["role"] == "assistant")
        assert resp_row["content"] == "Sure, looks fine!"
        assert resp_row["responds_to"] == 1

    def test_json_export_no_ephemerals_gives_empty_array(self):
        detail = _make_detail()
        result = export_json(detail)
        assert "ephemerals" in result
        assert result["ephemerals"] == []

    def test_json_export_explicit_empty_ephemerals(self):
        detail = _make_detail()
        result = export_json(detail, ephemerals=[])
        assert result["ephemerals"] == []

    def test_json_export_preserves_all_ephemeral_fields(self):
        detail = _make_detail()
        ephemerals = _make_ephemerals()
        result = export_json(detail, ephemerals=ephemerals)
        for row in result["ephemerals"]:
            for key in (
                "id",
                "session_id",
                "kind",
                "role",
                "content",
                "responds_to",
                "timestamp",
            ):
                assert key in row, f"Missing key: {key}"


# ---------------------------------------------------------------------------
# Markdown export tests
# ---------------------------------------------------------------------------


class TestExportMarkdownEphemerals:
    def test_markdown_contains_ephemeral_marker_block(self):
        detail = _make_detail()
        ephemerals = _make_ephemerals()
        md = export_markdown(detail, ephemerals=ephemerals)
        assert "[ephemeral · btw]" in md

    def test_markdown_contains_ephemeral_user_content(self):
        detail = _make_detail()
        ephemerals = _make_ephemerals()
        md = export_markdown(detail, ephemerals=ephemerals)
        assert "btw can you check this?" in md

    def test_markdown_contains_ephemeral_assistant_content(self):
        detail = _make_detail()
        ephemerals = _make_ephemerals()
        md = export_markdown(detail, ephemerals=ephemerals)
        assert "Sure, looks fine!" in md

    def test_markdown_no_ephemerals_has_no_marker_blocks(self):
        detail = _make_detail()
        md = export_markdown(detail)
        assert "[ephemeral" not in md

    def test_markdown_no_ephemerals_still_renders_conversation(self):
        detail = _make_detail()
        md = export_markdown(detail)
        assert "## Conversation" in md
        assert "Hello" in md

    def test_markdown_unpaired_ephemeral_exports_gracefully(self):
        """A user row with no paired assistant response must not crash."""
        detail = _make_detail()
        ephemerals = [
            {
                "id": 1,
                "session_id": "s-export-001",
                "kind": "btw",
                "role": "user",
                "content": "unpaired user message",
                "responds_to": None,
                "timestamp": "2026-01-01T10:00:30",
            }
        ]
        md = export_markdown(detail, ephemerals=ephemerals)
        assert "unpaired user message" in md
        assert "[ephemeral · btw]" in md

    def test_markdown_ephemeral_interleaved_by_timestamp(self):
        """An ephemeral pair between two regular messages appears in the right order."""
        # Three-event timeline: msg_before @ 10:00:00, ephemeral @ 10:00:30, msg_after @ 10:01:00
        detail = _make_detail(
            messages=[
                Message(
                    id="u-before",
                    session_id="s-export-001",
                    role="user",
                    content_blocks=[TextBlock(text="before_marker")],
                    timestamp=datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
                ),
                Message(
                    id="u-after",
                    session_id="s-export-001",
                    role="user",
                    content_blocks=[TextBlock(text="after_marker")],
                    timestamp=datetime(2026, 1, 1, 10, 1, 0, tzinfo=timezone.utc),
                ),
            ]
        )
        ephemerals = [
            {
                "id": 1,
                "session_id": "s-export-001",
                "kind": "btw",
                "role": "user",
                "content": "middle_ephemeral",
                "responds_to": None,
                "timestamp": "2026-01-01T10:00:30+00:00",
            },
            {
                "id": 2,
                "session_id": "s-export-001",
                "kind": "btw",
                "role": "assistant",
                "content": "middle_reply",
                "responds_to": 1,
                "timestamp": "2026-01-01T10:00:35+00:00",
            },
        ]
        md = export_markdown(detail, ephemerals=ephemerals)
        before_pos = md.index("before_marker")
        ephemeral_pos = md.index("middle_ephemeral")
        after_pos = md.index("after_marker")
        assert before_pos < ephemeral_pos < after_pos, (
            "Ephemeral block should appear between before_marker and after_marker"
        )

    def test_markdown_ephemeral_interleaves_legacy_local_naive_timestamps(
        self, monkeypatch
    ):
        import os
        import time

        old_tz = os.environ.get("TZ")
        monkeypatch.setenv("TZ", "America/Los_Angeles")
        if hasattr(time, "tzset"):
            time.tzset()

        try:
            detail = _make_detail(
                messages=[
                    Message(
                        id="before",
                        session_id="s-export-001",
                        role="user",
                        content_blocks=[TextBlock(text="before_marker")],
                        timestamp=datetime(2026, 6, 4, 0, 15, 0, tzinfo=timezone.utc),
                    ),
                    Message(
                        id="after",
                        session_id="s-export-001",
                        role="user",
                        content_blocks=[TextBlock(text="after_marker")],
                        timestamp=datetime(2026, 6, 4, 0, 16, 0, tzinfo=timezone.utc),
                    ),
                ]
            )
            ephemerals = _make_ephemerals(
                input_ts="2026-06-03T17:15:30",
                response_ts="2026-06-03T17:15:35",
            )
            md = export_markdown(detail, ephemerals=ephemerals)
            assert (
                md.index("before_marker")
                < md.index("btw can you check this?")
                < md.index("after_marker")
            )
        finally:
            if old_tz is None:
                monkeypatch.delenv("TZ", raising=False)
            else:
                monkeypatch.setenv("TZ", old_tz)
            if hasattr(time, "tzset"):
                time.tzset()


# ---------------------------------------------------------------------------
# API-level export route tests
# ---------------------------------------------------------------------------


class TestExportRoutesWithEphemerals:
    async def _make_client_with_session(
        self, tmp_path: Path, session_id: str = "s-api-001"
    ):
        from httpx import AsyncClient, ASGITransport
        from agent_decoder.server import create_app
        from agent_decoder.config import AppConfig
        from agent_decoder.db import Database

        db_path = tmp_path / "export_eph.db"
        async with Database(db_path) as db:
            await db.init_schema()
            await db._conn.execute(
                "INSERT OR IGNORE INTO projects (id, display_name, raw_path, data_source) "
                "VALUES ('proj-api', 'API Export', '/tmp', 'test')"
            )
            await db._conn.execute(
                "INSERT OR IGNORE INTO sessions "
                "(id, project_id, file_path, message_count, user_message_count, is_worktree, is_fork) "
                "VALUES (?, 'proj-api', '/tmp/s.jsonl', 0, 0, 0, 0)",
                (session_id,),
            )
            await db._conn.commit()
        app = create_app(AppConfig(), db_path)
        return AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ), db_path

    async def test_json_export_api_includes_ephemerals_array(self, tmp_path):
        session_id = "s-api-002"
        client, db_path = await self._make_client_with_session(tmp_path, session_id)
        # Seed an ephemeral pair
        async with Database(db_path) as db:
            uid = await db.record_ephemeral_input(
                session_id, "api btw test content", timestamp="2026-01-01T10:05:00"
            )
            await db.record_ephemeral_response(
                uid, "api btw response", timestamp="2026-01-01T10:05:05"
            )

        async with client as c:
            r = await c.get(
                f"/api/sessions/{session_id}/export", params={"format": "json"}
            )
        assert r.status_code == 200
        data = r.json()
        assert "ephemerals" in data
        assert len(data["ephemerals"]) == 2

    async def test_markdown_export_api_includes_ephemeral_marker(self, tmp_path):
        session_id = "s-api-003"
        client, db_path = await self._make_client_with_session(tmp_path, session_id)
        async with Database(db_path) as db:
            uid = await db.record_ephemeral_input(
                session_id, "markdownbtwtest", timestamp="2026-01-01T10:05:00"
            )
            await db.record_ephemeral_response(
                uid, "markdownreplytest", timestamp="2026-01-01T10:05:05"
            )

        async with client as c:
            r = await c.get(
                f"/api/sessions/{session_id}/export", params={"format": "md"}
            )
        assert r.status_code == 200
        md = r.text
        assert "[ephemeral · btw]" in md
        assert "markdownbtwtest" in md

    async def test_json_export_api_no_ephemerals_gives_empty_array(self, tmp_path):
        session_id = "s-api-004"
        client, db_path = await self._make_client_with_session(tmp_path, session_id)

        async with client as c:
            r = await c.get(
                f"/api/sessions/{session_id}/export", params={"format": "json"}
            )
        assert r.status_code == 200
        data = r.json()
        assert data["ephemerals"] == []
