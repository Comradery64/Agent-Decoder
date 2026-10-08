"""Tests for PUT /api/sessions/{id}/title (issue #11).

Covers:
  - 404 for unknown session
  - 422 for malformed payload (Pydantic enforcement)
  - 200 + persists override + clears via null
  - Endpoint publishes a session-meta event on the SSE broadcaster
  - Existing /api/sessions/{id} response carries the override after a write
  - get_sessions list response carries the override
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from agent_decoder.db import Database
from agent_decoder.models import AppConfig, Project, Session


def _make_app(db_path: Path, config: AppConfig):
    from agent_decoder.server import create_app

    return create_app(config, db_path)


async def _seed_session(
    db_path: Path,
    *,
    session_id: str = "11111111-2222-3333-4444-555555555555",
) -> None:
    async with Database(db_path) as db:
        await db.init_schema()
        project = Project(
            id="proj-rename",
            display_name="rename-test",
            raw_path="-rename",
            data_source="test",
        )
        session = Session(
            id=session_id,
            project_id=project.id,
            file_path="/tmp/rename.jsonl",
            title="Original Parsed Title",
        )
        await db.upsert_project(project)
        await db.upsert_session(session)


@pytest.fixture
async def env(tmp_path) -> AsyncIterator[dict]:
    db_path = tmp_path / "title.db"
    session_id = "11111111-2222-3333-4444-555555555555"
    await _seed_session(db_path, session_id=session_id)
    yield {"db_path": db_path, "session_id": session_id}


async def _client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


async def test_rename_unknown_session_returns_404(env) -> None:
    app = _make_app(env["db_path"], AppConfig())
    async with await _client(app) as c:
        r = await c.put(
            "/api/sessions/nope/title",
            json={"title": "Whatever"},
        )
    assert r.status_code == 404


@pytest.mark.parametrize("payload", [{"title": 123}, {"title": []}, {}])
async def test_rename_rejects_malformed_payload(env, payload) -> None:
    """Pydantic enforces title: str | None; everything else → 422."""
    app = _make_app(env["db_path"], AppConfig())
    async with await _client(app) as c:
        r = await c.put(
            f"/api/sessions/{env['session_id']}/title",
            json=payload,
        )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


async def test_rename_persists_and_response_carries_override(env) -> None:
    app = _make_app(env["db_path"], AppConfig())
    sid = env["session_id"]
    async with await _client(app) as c:
        r = await c.put(f"/api/sessions/{sid}/title", json={"title": "My Rename"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body == {"ok": True, "id": sid, "custom_title": "My Rename"}

        # Subsequent detail read reflects the override.
        r2 = await c.get(f"/api/sessions/{sid}")
        assert r2.status_code == 200
        detail = r2.json()
        assert detail["title"] == "Original Parsed Title"
        assert detail["custom_title"] == "My Rename"


async def test_rename_clear_via_null(env) -> None:
    app = _make_app(env["db_path"], AppConfig())
    sid = env["session_id"]
    async with await _client(app) as c:
        await c.put(f"/api/sessions/{sid}/title", json={"title": "Temp"})
        r = await c.put(f"/api/sessions/{sid}/title", json={"title": None})
        assert r.status_code == 200
        assert r.json()["custom_title"] is None

        r2 = await c.get(f"/api/sessions/{sid}")
        assert r2.json()["custom_title"] is None


async def test_rename_blank_string_clears(env) -> None:
    """Frontend sends empty string when user deletes all input — treat as clear."""
    app = _make_app(env["db_path"], AppConfig())
    sid = env["session_id"]
    async with await _client(app) as c:
        await c.put(f"/api/sessions/{sid}/title", json={"title": "Will Clear"})
        r = await c.put(f"/api/sessions/{sid}/title", json={"title": "   "})
    assert r.status_code == 200
    assert r.json()["custom_title"] is None


async def test_rename_appears_in_project_session_list(env) -> None:
    app = _make_app(env["db_path"], AppConfig())
    sid = env["session_id"]
    async with await _client(app) as c:
        await c.put(f"/api/sessions/{sid}/title", json={"title": "Listed"})
        r = await c.get("/api/projects/proj-rename/sessions")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["custom_title"] == "Listed"


# ---------------------------------------------------------------------------
# SSE broadcast
# ---------------------------------------------------------------------------


async def test_rename_publishes_session_meta_event(env) -> None:
    """Two simulated SSE subscribers both receive the rename event."""
    from agent_decoder.events_bus import EventBroadcaster

    # Patch the broadcaster class with an instance we can subscribe to *before*
    # the endpoint publishes. Easiest: create the app, then reach into the
    # closure via a small monkeypatch on EventBroadcaster.publish.
    captured: list = []
    original_publish = EventBroadcaster.publish

    def capturing_publish(self, event):  # type: ignore[no-untyped-def]
        captured.append(event)
        return original_publish(self, event)

    EventBroadcaster.publish = capturing_publish  # type: ignore[assignment]
    try:
        app = _make_app(env["db_path"], AppConfig())
        sid = env["session_id"]
        async with await _client(app) as c:
            r = await c.put(f"/api/sessions/{sid}/title", json={"title": "Broadcasted"})
        assert r.status_code == 200
        assert {"type": "session-meta", "id": sid, "title": "Broadcasted"} in captured
    finally:
        EventBroadcaster.publish = original_publish  # type: ignore[assignment]
        # Defensive — let any spawned tasks finish so they don't leak.
        await asyncio.sleep(0)
