"""Tests for the session_meta override layer (issue #11).

Covers:
  - set_custom_title insert + idempotent update
  - set_custom_title(None) clears the row
  - whitespace-only titles are treated as clears
  - get_sessions/get_session_detail join the override into responses
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_decoder.db import Database
from agent_decoder.models import Project, Session


@pytest.fixture
async def db():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        async with Database(db_path) as database:
            await database.init_schema()
            yield database


async def _seed(db: Database, session_id: str = "sess-1") -> None:
    project = Project(
        id="proj-1",
        display_name="proj",
        raw_path="-proj",
        data_source="test",
    )
    session = Session(
        id=session_id,
        project_id=project.id,
        file_path="/tmp/sess.jsonl",
        title="Parsed title",
        updated_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
    )
    await db.upsert_project(project)
    await db.upsert_session(session)


async def test_set_custom_title_persists(db: Database) -> None:
    await _seed(db)
    stored = await db.set_custom_title("sess-1", "Renamed by user")
    assert stored == "Renamed by user"
    assert await db.get_custom_title("sess-1") == "Renamed by user"


async def test_set_custom_title_idempotent_update(db: Database) -> None:
    await _seed(db)
    await db.set_custom_title("sess-1", "First")
    await db.set_custom_title("sess-1", "Second")
    assert await db.get_custom_title("sess-1") == "Second"


async def test_set_custom_title_none_clears(db: Database) -> None:
    await _seed(db)
    await db.set_custom_title("sess-1", "Whatever")
    await db.set_custom_title("sess-1", None)
    assert await db.get_custom_title("sess-1") is None


async def test_set_custom_title_blank_string_clears(db: Database) -> None:
    """Empty/whitespace input behaves the same as None (frontend sends '')."""
    await _seed(db)
    await db.set_custom_title("sess-1", "Existing")
    stored = await db.set_custom_title("sess-1", "   ")
    assert stored is None
    assert await db.get_custom_title("sess-1") is None


async def test_get_sessions_includes_custom_title(db: Database) -> None:
    await _seed(db)
    await db.set_custom_title("sess-1", "Override")
    sessions = await db.get_sessions(project_id="proj-1")
    assert len(sessions) == 1
    assert sessions[0].title == "Parsed title"
    assert sessions[0].custom_title == "Override"


async def test_get_sessions_without_override_returns_none_custom_title(
    db: Database,
) -> None:
    await _seed(db)
    sessions = await db.get_sessions(project_id="proj-1")
    assert sessions[0].custom_title is None


async def test_get_session_detail_includes_custom_title(db: Database) -> None:
    await _seed(db)
    await db.set_custom_title("sess-1", "Detail Override")
    detail = await db.get_session_detail("sess-1")
    assert detail is not None
    assert detail.title == "Parsed title"
    assert detail.custom_title == "Detail Override"


async def test_get_session_detail_json_bytes_includes_custom_title(
    db: Database,
) -> None:
    """The Pydantic-bypass fast path must also embed the override."""
    import json as _json

    await _seed(db)
    await db.set_custom_title("sess-1", "Bytes Override")
    raw = await db.get_session_detail_json_bytes("sess-1")
    assert raw is not None
    parsed = _json.loads(raw)
    assert parsed["title"] == "Parsed title"
    assert parsed["custom_title"] == "Bytes Override"
