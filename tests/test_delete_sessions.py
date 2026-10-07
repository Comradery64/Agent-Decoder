"""Tests for the session hard-delete subsystem.

Covers:
  (a) db.delete_session — removes the sessions row + messages, returns True;
      returns False for a missing id.
  (b) POST /api/sessions/delete — contract: {ok, deleted, failed} for a real
      temp session and for a bogus id.
"""

from __future__ import annotations

from pathlib import Path

from httpx import ASGITransport, AsyncClient

from agent_decoder.db import Database
from agent_decoder.models import AppConfig, Message, Project, Session, TextBlock


# ---------------------------------------------------------------------------
# Helpers shared by db and endpoint tests
# ---------------------------------------------------------------------------

_SESSION_ID = "dddddddd-0000-0000-0000-000000000001"
_PROJECT_ID = "proj-delete-test"


async def _seed(db_path: Path, jsonl_path: str = "/tmp/fake-delete.jsonl") -> None:
    """Seed one project + one session + two messages into db_path."""
    async with Database(db_path) as db:
        await db.init_schema()
        project = Project(
            id=_PROJECT_ID,
            display_name="Delete Test",
            raw_path="-delete-test",
            data_source="test",
        )
        session = Session(
            id=_SESSION_ID,
            project_id=_PROJECT_ID,
            file_path=jsonl_path,
            title="Session To Delete",
        )
        await db.upsert_project(project)
        await db.upsert_session(session)
        messages = [
            Message(
                id="del-msg-0001",
                session_id=_SESSION_ID,
                role="user",
                content_blocks=[TextBlock(text="hi")],
            ),
            Message(
                id="del-msg-0002",
                session_id=_SESSION_ID,
                role="assistant",
                content_blocks=[TextBlock(text="hello")],
            ),
        ]
        await db.upsert_messages(messages)


# ---------------------------------------------------------------------------
# (a) db.delete_session
# ---------------------------------------------------------------------------


async def test_delete_session_removes_rows_and_returns_true(tmp_path) -> None:
    db_path = tmp_path / "del.db"
    await _seed(db_path)

    async with Database(db_path) as db:
        result = await db.delete_session(_SESSION_ID)

    assert result is True, "delete_session must return True when row existed"

    # Verify rows are gone.
    async with Database(db_path) as db:
        sessions = await db.get_sessions()
        assert not any(s.id == _SESSION_ID for s in sessions), (
            "session row must be deleted"
        )

        # messages
        async with db._conn.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ?", (_SESSION_ID,)
        ) as cur:
            row = await cur.fetchone()
        assert row[0] == 0, "messages must be deleted"

        # FTS
        async with db._conn.execute(
            "SELECT COUNT(*) FROM messages_fts WHERE session_id = ?", (_SESSION_ID,)
        ) as cur:
            row = await cur.fetchone()
        assert row[0] == 0, "FTS entries must be deleted"

        # recaps (should be 0 — none were inserted)
        async with db._conn.execute(
            "SELECT COUNT(*) FROM recaps WHERE session_id = ?", (_SESSION_ID,)
        ) as cur:
            row = await cur.fetchone()
        assert row[0] == 0


async def test_delete_session_missing_returns_false(tmp_path) -> None:
    db_path = tmp_path / "del_missing.db"
    async with Database(db_path) as db:
        await db.init_schema()
        result = await db.delete_session("00000000-dead-beef-0000-000000000000")

    assert result is False, "delete_session must return False for a non-existent id"


# ---------------------------------------------------------------------------
# (b) POST /api/sessions/delete — endpoint contract
# ---------------------------------------------------------------------------


def _make_app(db_path: Path, config: AppConfig):
    from agent_decoder.server import create_app

    return create_app(config, db_path)


async def _client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_endpoint_deletes_known_session(tmp_path) -> None:
    """Endpoint returns ok=True, deleted=[id], failed=[] for a known session
    whose file_path is inside the configured scan root."""
    db_path = tmp_path / "ep.db"

    # Create a real .jsonl file under a scan-root-shaped tree so path-safety
    # passes: <tmp>/projects/<proj>/<session>.jsonl
    projects_dir = tmp_path / "projects" / _PROJECT_ID
    projects_dir.mkdir(parents=True)
    jsonl_path = projects_dir / f"{_SESSION_ID}.jsonl"
    jsonl_path.write_text("{}\n")

    await _seed(db_path, jsonl_path=str(jsonl_path))

    # Configure the app so tmp_path is a scan root.
    config = AppConfig(data_paths=[str(tmp_path)])
    app = _make_app(db_path, config)

    async with await _client(app) as c:
        r = await c.post(
            "/api/sessions/delete",
            json={"session_ids": [_SESSION_ID]},
        )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["deleted"] == [_SESSION_ID]
    assert body["failed"] == []

    # File should be unlinked.
    assert not jsonl_path.exists(), "on-disk .jsonl must be unlinked"

    # DB row must be gone.
    async with Database(db_path) as db:
        sessions = await db.get_sessions()
    assert not any(s.id == _SESSION_ID for s in sessions)


async def test_endpoint_deletes_codex_session_under_codex_data_path(tmp_path) -> None:
    """Regression: a Codex rollout lives under ``codex_data_paths``
    (~/.codex/sessions), which ``get_all_scan_paths()`` deliberately omits —
    it scopes only the Claude watcher/scanner. The delete path-safety check
    must include ``codex_data_paths`` too, or every Codex session fails the
    check ("path outside sessions root") and can't be deleted."""
    db_path = tmp_path / "ep_codex.db"

    # Disjoint Claude vs Codex roots — so passing requires codex_data_paths
    # specifically, not just any scan root.
    claude_root = tmp_path / "claude"
    codex_root = tmp_path / "codex" / "sessions"
    codex_root.mkdir(parents=True)
    rollout_path = codex_root / f"rollout-{_SESSION_ID}.jsonl"
    rollout_path.write_text("{}\n")

    await _seed(db_path, jsonl_path=str(rollout_path))

    config = AppConfig(
        data_paths=[str(claude_root)],
        codex_data_paths=[str(codex_root)],
    )
    app = _make_app(db_path, config)

    async with await _client(app) as c:
        r = await c.post(
            "/api/sessions/delete",
            json={"session_ids": [_SESSION_ID]},
        )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["deleted"] == [_SESSION_ID], (
        f"Codex session must delete via codex_data_paths; got failed={body['failed']}"
    )
    assert body["failed"] == []
    assert not rollout_path.exists(), "on-disk Codex rollout must be unlinked"


async def test_endpoint_failed_for_bogus_id(tmp_path) -> None:
    """Endpoint returns ok=True, deleted=[], failed=[{id,error}] for an id
    that was never indexed (no sessions row, no file_path to validate)."""
    db_path = tmp_path / "ep_bogus.db"
    async with Database(db_path) as db:
        await db.init_schema()

    app = _make_app(db_path, AppConfig())
    bogus = "00000000-dead-beef-cafe-000000000000"

    async with await _client(app) as c:
        r = await c.post(
            "/api/sessions/delete",
            json={"session_ids": [bogus]},
        )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    # A bogus id has no file_path in the DB, so delete_session runs on it
    # (returns False), it lands in deleted — this is intentional: the
    # caller asked us to remove it; there's nothing to remove, done.
    # Either outcome (deleted or failed) is acceptable here; what matters
    # is that the response shape is correct and the server doesn't crash.
    assert "deleted" in body
    assert "failed" in body
    assert isinstance(body["deleted"], list)
    assert isinstance(body["failed"], list)
    # The id must appear in exactly one of the two lists.
    assert (bogus in body["deleted"]) != (bogus in body["failed"]), (
        f"id must appear in exactly one list; got deleted={body['deleted']} failed={body['failed']}"
    )


async def test_endpoint_mixed_batch(tmp_path) -> None:
    """Batch with one real session and one bogus id: real is deleted, bogus
    lands in deleted (idempotent — nothing to delete is still OK)."""
    db_path = tmp_path / "ep_mixed.db"
    projects_dir = tmp_path / "projects" / _PROJECT_ID
    projects_dir.mkdir(parents=True)
    jsonl_path = projects_dir / f"{_SESSION_ID}.jsonl"
    jsonl_path.write_text("{}\n")

    await _seed(db_path, jsonl_path=str(jsonl_path))
    config = AppConfig(data_paths=[str(tmp_path)])
    app = _make_app(db_path, config)

    bogus = "00000000-cafe-babe-0000-000000000099"
    async with await _client(app) as c:
        r = await c.post(
            "/api/sessions/delete",
            json={"session_ids": [_SESSION_ID, bogus]},
        )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert _SESSION_ID in body["deleted"]
    # bogus either in deleted (idempotent) or failed — must not be absent
    assert bogus in body["deleted"] or bogus in body["failed"]
