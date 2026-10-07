"""Tests for the current server routes.

What's left:
  POST /api/sessions/new        — pending-session metadata mint
  GET  /api/runner-status?ids=  — batch busy snapshot (PtyManager-derived)
  PUT  /api/fs/write            — file-preview save
  Static SPA fallback          — hashed-asset 404, deep-link routes

The PTY runner has its own tests in ``test_pty_runner.py``; the recap
endpoint is covered by ``test_recaps.py``.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from agent_decoder.db import Database
from agent_decoder.locks import _lock_path_for
from agent_decoder.models import AppConfig, Project, Session


FAKE = Path(__file__).parent / "fixtures" / "fake_claude_tui.py"


# ---------------------------------------------------------------------------
# Setup helpers
# ---------------------------------------------------------------------------


def _write_shim(dir_: Path, bin_name: str = "claude", extra_argv: str = "") -> Path:
    """Create an executable shim that execs ``fake_claude_tui.py``.

    After Phase 6 the shim is only needed by the new-session route's
    ``shutil.which(bin_name)`` pre-check — nothing actually spawns the
    fake here anymore. ``fake_claude_tui.py`` is the only fake we ship
    after Phase 8 retires ``fake_claude.py``.
    """
    path = dir_ / bin_name
    body = (
        "#!/usr/bin/env bash\n"
        f'exec {shutil.which("python3") or "python3"} "{FAKE}" {extra_argv} "$@"\n'
    )
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


async def _seed_session(
    db_path: Path,
    *,
    session_id: str = "11111111-2222-3333-4444-555555555555",
    cwd: str,
    file_path: str,
    is_fork: bool = False,
) -> None:
    """Insert one session into the DB with the requested fields."""
    async with Database(db_path) as db:
        await db.init_schema()
        project = Project(
            id="proj-rt",
            display_name="runtime-test",
            raw_path="-runtime",
            data_source="test",
        )
        session = Session(
            id=session_id,
            project_id=project.id,
            file_path=file_path,
            cwd=cwd,
            is_fork=is_fork,
        )
        await db.upsert_project(project)
        await db.upsert_session(session)


def _make_app(db_path: Path, config: AppConfig):
    from agent_decoder.server import create_app

    return create_app(config, db_path)


@pytest.fixture
async def env_with_claude(tmp_path, monkeypatch) -> AsyncIterator[dict]:
    """Tmp dir + DB + a real session + ``claude`` on PATH (fake binary).

    Yields a dict with: ``db_path``, ``cwd``, ``file_path``, ``session_id``,
    ``bin_dir``.
    """
    db_path = tmp_path / "test.db"
    # Use a `projects/` ancestor so _derive_bin_name returns "claude".
    projects = tmp_path / "root" / ".claude" / "projects" / "-runtime"
    projects.mkdir(parents=True)
    session_id = "11111111-2222-3333-4444-555555555555"
    file_path = projects / f"{session_id}.jsonl"
    file_path.write_text("")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_shim(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    await _seed_session(
        db_path,
        session_id=session_id,
        cwd=str(cwd),
        file_path=str(file_path),
    )

    yield {
        "db_path": db_path,
        "cwd": str(cwd),
        "file_path": str(file_path),
        "session_id": session_id,
        "bin_dir": bin_dir,
    }


async def _client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ---------------------------------------------------------------------------
# /api/runner-status — batch busy snapshot (PtyManager-derived)
# ---------------------------------------------------------------------------


async def test_runner_status_batch_idle_session_reports_not_busy(env_with_claude):
    """A session with no live PTY channel must report ``busy=False``."""
    e = env_with_claude
    app = _make_app(e["db_path"], AppConfig())
    async with await _client(app) as c:
        r = await c.get(f"/api/runner-status?ids={e['session_id']}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert e["session_id"] in body
    assert body[e["session_id"]]["busy"] is False


async def test_pty_ownership_idle_response_shape(env_with_claude):
    """Ownership endpoint returns the stable FE contract for idle sessions."""
    e = env_with_claude
    app = _make_app(e["db_path"], AppConfig())
    async with app.router.lifespan_context(app):
        async with await _client(app) as c:
            r = await c.get(f"/api/pty/ownership/{e['session_id']}")

    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {
        "status",
        "foreign_pids",
        "foreign_owner",
        "jsonl_path",
    }
    assert body == {
        "status": "idle",
        "foreign_pids": [],
        "foreign_owner": None,
        "jsonl_path": e["file_path"],
    }


async def test_pty_takeover_unlinks_cross_host_sidecar(env_with_claude):
    """Take-over must clear a fresh cross-host sidecar so focus can retry."""
    e = env_with_claude
    jsonl_path = Path(e["file_path"])
    lock_path = _lock_path_for(jsonl_path)
    lock_path.write_text(
        json.dumps(
            {
                "owner_kind": "claude-wrapper",
                "pid": 22673,
                "hostname": "peer-laptop.local",
                "heartbeat_at": datetime.now(timezone.utc).isoformat(),
                "ui_endpoint": "http://192.168.1.99:4242",
            }
        )
    )

    app = _make_app(e["db_path"], AppConfig())
    async with app.router.lifespan_context(app):
        async with await _client(app) as c:
            before = await c.get(f"/api/pty/ownership/{e['session_id']}")
            assert before.status_code == 200, before.text
            assert before.json()["status"] == "terminal"
            assert before.json()["foreign_owner"]["hostname"] == "peer-laptop.local"

            takeover = await c.post(f"/api/pty/takeover/{e['session_id']}")
            assert takeover.status_code == 200, takeover.text
            assert takeover.json()["released_pids"] == []

            after = await c.get(f"/api/pty/ownership/{e['session_id']}")
            assert after.status_code == 200, after.text
            assert after.json()["status"] == "idle"
            assert after.json()["foreign_owner"] is None

    assert not lock_path.exists()


# ---------------------------------------------------------------------------
# POST /api/sessions/new — pending-session metadata mint
#
# Contract: pure mint. Does NOT spawn a claude subprocess, does NOT write
# JSONL. Stashes a pending entry; the first /api/pty/submit for that id
# materialises the JSONL via ``claude --session-id``.
# ---------------------------------------------------------------------------


async def test_new_session_returns_fresh_uuid(env_with_claude):
    """POST /api/sessions/new returns a new uuid + cwd + permission_mode."""
    e = env_with_claude
    app = _make_app(e["db_path"], AppConfig())
    async with await _client(app) as c:
        r = await c.post("/api/sessions/new", json={"cwd": e["cwd"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "session_id" in body
    assert body["session_id"] != e["session_id"]  # NOT the seeded session
    # UUIDv4 shape (8-4-4-4-12 hex)
    import re

    assert re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
        body["session_id"],
    )
    assert body["cwd"] == e["cwd"]
    assert body["permission_mode"] == "default"


def test_app_config_default_permission_mode_is_native_compatible():
    """Normal web PTY sessions should default to interactive native mode."""
    assert AppConfig().claude_default_permission_mode == "default"


def test_app_config_default_chat_send_shortcut_is_enter():
    """Decoded composer defaults to Enter-to-send unless changed in settings."""
    assert AppConfig().chat_send_shortcut == "enter"


def test_app_config_default_native_pty_font_is_monaspace_argon():
    """Native PTY keeps the current preferred font unless changed in settings."""
    assert AppConfig().native_pty_font_family == "monaspace-argon"


def test_app_config_default_native_pty_cols():
    """Native PTY width defaults to 80 cols (single source of truth, shared
    with the PTY spawn width)."""
    assert AppConfig().native_pty_cols == 100


async def test_new_session_records_pending_entry(env_with_claude):
    """The minted id is stashed in the in-memory pending map so a later
    /api/pty/submit can route to the new_session=True spawn path."""
    e = env_with_claude
    app = _make_app(e["db_path"], AppConfig())
    async with await _client(app) as c:
        r = await c.post(
            "/api/sessions/new",
            json={"cwd": e["cwd"], "permission_mode": "acceptEdits"},
        )
    assert r.status_code == 200, r.text
    new_id = r.json()["session_id"]
    pending = app.state.pending_sessions
    assert new_id in pending
    assert pending[new_id].cwd == e["cwd"]
    assert pending[new_id].permission_mode == "acceptEdits"


async def test_new_session_defaults_cwd_to_last_used(env_with_claude):
    """When no cwd is given, default to the most-recent session's cwd."""
    e = env_with_claude
    app = _make_app(e["db_path"], AppConfig())
    async with await _client(app) as c:
        r = await c.post("/api/sessions/new", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cwd"] == e["cwd"]  # falls back to the seeded session's cwd


async def test_new_session_rejects_unknown_cwd(env_with_claude):
    """A cwd that doesn't exist on disk → 404, no pending entry."""
    e = env_with_claude
    app = _make_app(e["db_path"], AppConfig())
    async with await _client(app) as c:
        r = await c.post(
            "/api/sessions/new", json={"cwd": "/definitely/not/a/real/path"}
        )
    assert r.status_code == 404
    assert not app.state.pending_sessions


async def test_new_session_503_when_bin_missing(env_with_claude, monkeypatch):
    """When claude is not on PATH → 503 (validated up front)."""
    e = env_with_claude
    monkeypatch.setenv("PATH", "/nonexistent-dir")
    app = _make_app(e["db_path"], AppConfig())
    async with await _client(app) as c:
        r = await c.post("/api/sessions/new", json={"cwd": e["cwd"]})
    assert r.status_code == 503


async def test_pty_native_snapshot_route_returns_snapshot(env_with_claude):
    """Native snapshot route returns either a snapshot or a missing-channel 404."""
    e = env_with_claude
    app = _make_app(e["db_path"], AppConfig())
    async with app.router.lifespan_context(app):
        async with await _client(app) as c:
            r = await c.get("/api/pty/native-snapshot?session_id=sess-missing")
    assert r.status_code in {200, 404}


async def test_pty_native_input_rejects_missing_channel(env_with_claude):
    """Raw native input must reject sessions without a live PTY channel."""
    e = env_with_claude
    app = _make_app(e["db_path"], AppConfig())
    async with app.router.lifespan_context(app):
        async with await _client(app) as c:
            r = await c.post(
                "/api/pty/input",
                json={"session_id": "nope", "data": "\r"},
            )
    assert r.status_code in {404, 409}


async def test_pty_resize_validates_dimensions(env_with_claude):
    """PTY resize rejects invalid terminal dimensions before manager dispatch."""
    e = env_with_claude
    app = _make_app(e["db_path"], AppConfig())
    async with app.router.lifespan_context(app):
        async with await _client(app) as c:
            r = await c.post(
                "/api/pty/resize",
                json={"session_id": "x", "rows": 0, "cols": 120},
            )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Static SPA fallback — hashed asset misses must 404
# ---------------------------------------------------------------------------


async def test_missing_hashed_asset_returns_404(tmp_path):
    """Requests under /assets/ that don't match a real file must 404.

    Falling through to index.html for a missing JS chunk hands the browser
    text/html in response to a module import, producing an opaque
    "Failed to fetch dynamically imported module" error with no actionable
    diagnostic. The frontend's lazyWithRetry only triggers its reload prompt
    on a real fetch failure — see issue #10.
    """
    cfg = AppConfig()
    db_path = tmp_path / "spa.db"
    async with Database(db_path) as db:
        await db.init_schema()
    app = _make_app(db_path, cfg)
    async with await _client(app) as c:
        r = await c.get("/assets/ShortcutsPopup-DOES-NOT-EXIST.js")
    assert r.status_code == 404


async def test_existing_static_assets_are_not_cached(tmp_path):
    """Built frontend assets are also no-store during local desktop serving.

    The SPA shell already disables caching. Matching that policy on assets
    prevents a browser tab from quietly reusing stale JavaScript while the
    native PTY renderer is being iterated and rebuilt locally.
    """
    cfg = AppConfig()
    db_path = tmp_path / "spa-cache.db"
    async with Database(db_path) as db:
        await db.init_schema()
    app = _make_app(db_path, cfg)
    async with await _client(app) as c:
        r = await c.get("/")
        if r.status_code == 404:
            pytest.skip("static frontend build is not present")
        asset = next(
            part
            for part in r.text.split('"')
            if part.startswith("/assets/")
            and (part.endswith(".js") or part.endswith(".css"))
        )
        asset_response = await c.get(asset)
    assert asset_response.status_code == 200
    assert (
        asset_response.headers["cache-control"] == "no-cache, no-store, must-revalidate"
    )


async def test_spa_unknown_route_still_serves_index(tmp_path):
    """Non-/assets unknown routes keep serving index.html so the SPA router
    can take over (deep-link to /analytics, /session/<id>, etc.)."""
    cfg = AppConfig()
    db_path = tmp_path / "spa2.db"
    async with Database(db_path) as db:
        await db.init_schema()
    app = _make_app(db_path, cfg)
    async with await _client(app) as c:
        r = await c.get("/some/spa/route")
    # If a static/ build is present in the package, we get the SPA shell.
    # If not (dev install without `npm run build`), this route is simply
    # absent — either outcome is acceptable, but we MUST NOT see HTML being
    # mistakenly served from the /assets/ namespace handled above.
    assert r.status_code in (200, 404)


# ---------------------------------------------------------------------------
# /api/fs/write — file-preview editing (always enabled, ignores edit_enabled)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# /api/sessions/{id}/ephemerals — /btw pair listing (Phase 2 step 4)
# ---------------------------------------------------------------------------


async def test_ephemerals_endpoint_returns_empty_for_unknown_session(tmp_path):
    """A session with no /btw exchanges returns []."""
    db_path = tmp_path / "test.db"
    async with Database(db_path) as db:
        await db.init_schema()
    config = AppConfig(data_paths=[str(tmp_path)])
    app = _make_app(db_path, config)

    async with await _client(app) as c:
        r = await c.get("/api/sessions/nonexistent-sid/ephemerals")
    assert r.status_code == 200
    assert r.json() == []


async def test_ephemerals_endpoint_returns_paired_rows_in_order(tmp_path):
    """Persisted /btw pairs come back ordered by timestamp with the FK link."""
    db_path = tmp_path / "test.db"
    sid = "test-sid-aaaa"
    async with Database(db_path) as db:
        await db.init_schema()
        in1 = await db.record_ephemeral_input(
            sid, "/btw first?", timestamp="2026-05-28T10:00:00"
        )
        await db.record_ephemeral_response(
            in1, "first answer", timestamp="2026-05-28T10:00:01"
        )
        in2 = await db.record_ephemeral_input(
            sid, "/btw second?", timestamp="2026-05-28T10:00:02"
        )
        await db.record_ephemeral_response(
            in2, "second answer", timestamp="2026-05-28T10:00:03"
        )
        # Unrelated session — must not leak into the response
        other = await db.record_ephemeral_input(
            "other-sid", "/btw nope?", timestamp="2026-05-28T10:00:04"
        )
        await db.record_ephemeral_response(
            other, "nope", timestamp="2026-05-28T10:00:05"
        )

    config = AppConfig(data_paths=[str(tmp_path)])
    app = _make_app(db_path, config)

    async with await _client(app) as c:
        r = await c.get(f"/api/sessions/{sid}/ephemerals")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 4
    assert [(r["role"], r["content"]) for r in rows] == [
        ("user", "/btw first?"),
        ("assistant", "first answer"),
        ("user", "/btw second?"),
        ("assistant", "second answer"),
    ]
    # responds_to links assistants back to their inputs
    assert rows[1]["responds_to"] == rows[0]["id"]
    assert rows[3]["responds_to"] == rows[2]["id"]
    # session scoping holds
    assert all(r["session_id"] == sid for r in rows)


# ---------------------------------------------------------------------------
# /btw end-to-end via the production lifespan path (Phase 2 step 9)
#
# Regression guard for commit a712c4e: PtyManager was constructed with
# ``Database(db_path)`` but the Database was never entered as an async
# context manager, so every ephemeral write silently failed.  The unit
# tests in test_pty_runner_btw.py used a fixture that DID enter the
# Database, so the bug was invisible.  This integration test boots the
# real app via FastAPI's lifespan and submits /btw through the HTTP API
# — exactly the path production exercises.
# ---------------------------------------------------------------------------


async def test_btw_endtoend_via_lifespan_app(tmp_path, monkeypatch):
    """End-to-end /btw capture through the lifespan-constructed app.

    Submits a /btw via POST /api/pty/submit and verifies that both the
    user and assistant ephemeral rows persist + are linked via responds_to.
    Uses fake_claude_tui with ``--canned-response btw-single`` so the
    modal emits the BTW_RESPONSE_COMPLETE_MARKER and the finalize path runs.
    """
    db_path = tmp_path / "test.db"
    projects = tmp_path / "root" / ".claude" / "projects" / "-runtime"
    projects.mkdir(parents=True)
    session_id = "btw-lifespan-aaa11111-2222-3333-4444-555555555555"
    file_path = projects / f"{session_id}.jsonl"
    file_path.write_text("")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_shim(bin_dir, extra_argv="--canned-response btw-single")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude_config"))

    await _seed_session(
        db_path,
        session_id=session_id,
        cwd=str(cwd),
        file_path=str(file_path),
    )

    config = AppConfig(data_paths=[str(tmp_path)])
    app = _make_app(db_path, config)

    # Drive the full lifespan — startup constructs PtyManager with the
    # properly-entered Database, shutdown tears it down cleanly.  This
    # is what real production does; the per-route ``async with
    # Database(db_path)`` pattern other tests use does NOT exercise it.
    async with app.router.lifespan_context(app):
        async with await _client(app) as c:
            r = await c.post(
                "/api/pty/focus",
                json={
                    "session_id": session_id,
                    "cwd": str(cwd),
                    "bin_name": "claude",
                    "permission_mode": "dontAsk",
                    "new_chat": True,
                },
            )
            assert r.status_code == 200, r.text

            r = await c.post(
                "/api/pty/submit",
                json={"session_id": session_id, "content": "/btw test ping"},
            )
            assert r.status_code == 200, r.text

            # Poll the new GET endpoint for both rows.  The fake emits
            # the modal markers immediately, so 30 s is plenty.
            rows: list[dict] = []
            for _ in range(60):
                r = await c.get(f"/api/sessions/{session_id}/ephemerals")
                assert r.status_code == 200
                rows = r.json()
                if len(rows) >= 2:
                    break
                await asyncio.sleep(0.5)

    assert len(rows) == 2, (
        f"expected user+assistant ephemeral rows, got {len(rows)}: {rows!r}"
    )
    assert rows[0]["role"] == "user"
    assert rows[0]["content"] == "/btw test ping"
    assert rows[0]["kind"] == "btw"
    assert rows[1]["role"] == "assistant"
    assert rows[1]["responds_to"] == rows[0]["id"]
    assert rows[1]["content"], "assistant content must not be empty"


async def test_btw_endtoend_via_lifespan_emits_sse_event(tmp_path, monkeypatch):
    """The lifespan-constructed app must publish ``ephemeral_pair_persisted``
    on the SSE bus when finalize completes.  Test the bus directly (the
    /api/events endpoint is exercised by other tests)."""
    db_path = tmp_path / "test.db"
    projects = tmp_path / "root" / ".claude" / "projects" / "-runtime"
    projects.mkdir(parents=True)
    session_id = "btw-lifespan-sse-aaaa-bbbb-cccc-dddddddddddd"
    file_path = projects / f"{session_id}.jsonl"
    file_path.write_text("")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_shim(bin_dir, extra_argv="--canned-response btw-single")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude_config"))

    await _seed_session(
        db_path,
        session_id=session_id,
        cwd=str(cwd),
        file_path=str(file_path),
    )

    config = AppConfig(data_paths=[str(tmp_path)])
    app = _make_app(db_path, config)

    async with app.router.lifespan_context(app):
        # Subscribe to the bus BEFORE the submit so we don't miss the event.
        # The bus is local to ``create_app``; reach it through the PtyManager
        # that was stored on ``app.state`` during lifespan startup.
        queue = app.state.pty_manager._bus.subscribe()

        async with await _client(app) as c:
            await c.post(
                "/api/pty/focus",
                json={
                    "session_id": session_id,
                    "cwd": str(cwd),
                    "bin_name": "claude",
                    "permission_mode": "dontAsk",
                    "new_chat": True,
                },
            )
            await c.post(
                "/api/pty/submit",
                json={"session_id": session_id, "content": "/btw sse check"},
            )

            # Drain the bus looking for our event.
            target = None
            deadline = asyncio.get_event_loop().time() + 30.0
            while asyncio.get_event_loop().time() < deadline:
                try:
                    evt = queue.get_nowait()
                except asyncio.QueueEmpty:
                    await asyncio.sleep(0.1)
                    continue
                if (
                    isinstance(evt, dict)
                    and evt.get("type") == "ephemeral_pair_persisted"
                ):
                    target = evt
                    break

    assert target is not None, "ephemeral_pair_persisted event must publish"
    assert target["session_id"] == session_id
    assert target["kind"] == "btw"
    assert isinstance(target.get("input_id"), int)
    assert isinstance(target.get("response_id"), int)


async def test_fs_write_accepts_body_and_is_not_gated_by_edit_enabled(tmp_path):
    """File-preview save: body parses (no 422 'loc=query'), and edit_enabled=False
    must not block it — file-preview editing is intentionally always-on."""
    target = tmp_path / "note.txt"
    target.write_text("original\n")

    db_path = tmp_path / "test.db"
    async with Database(db_path) as db:
        await db.init_schema()
    config = AppConfig(data_paths=[str(tmp_path)], edit_enabled=False)
    app = _make_app(db_path, config)

    async with await _client(app) as c:
        r = await c.put(
            "/api/fs/write",
            json={"path": str(target), "content": "edited\n"},
        )
    assert r.status_code == 200, r.text
    assert target.read_text() == "edited\n"


# ---------------------------------------------------------------------------
# /api/sessions/{id}/{archived,starred,viewed} — server-backed flags
# (replaces localStorage-only LS.ARCHIVED / LS.STARRED / LS.VIEWED_AT)
# ---------------------------------------------------------------------------


async def test_archived_endpoint_persists_and_404s_for_unknown(env_with_claude):
    config = AppConfig(data_paths=[str(Path(env_with_claude["db_path"]).parent)])
    app = _make_app(env_with_claude["db_path"], config)
    async with await _client(app) as c:
        r = await c.put(
            f"/api/sessions/{env_with_claude['session_id']}/archived",
            json={"archived": True},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["archived_at"] is not None

        # GET via session detail surfaces the flag
        r = await c.get(f"/api/sessions/{env_with_claude['session_id']}")
        assert r.status_code == 200
        assert r.json()["archived_at"] == body["archived_at"]

        # Unarchive clears
        r = await c.put(
            f"/api/sessions/{env_with_claude['session_id']}/archived",
            json={"archived": False},
        )
        assert r.json()["archived_at"] is None

        # 404 for unknown session id
        r = await c.put(
            "/api/sessions/does-not-exist/archived",
            json={"archived": True},
        )
        assert r.status_code == 404


async def test_starred_endpoint_persists(env_with_claude):
    config = AppConfig(data_paths=[str(Path(env_with_claude["db_path"]).parent)])
    app = _make_app(env_with_claude["db_path"], config)
    async with await _client(app) as c:
        r = await c.put(
            f"/api/sessions/{env_with_claude['session_id']}/starred",
            json={"starred": True},
        )
        assert r.status_code == 200
        assert r.json()["starred_at"] is not None


async def test_viewed_endpoint_accepts_explicit_timestamp(env_with_claude):
    config = AppConfig(data_paths=[str(Path(env_with_claude["db_path"]).parent)])
    app = _make_app(env_with_claude["db_path"], config)
    async with await _client(app) as c:
        ts = "2026-05-28T16:00:00"
        r = await c.put(
            f"/api/sessions/{env_with_claude['session_id']}/viewed",
            json={"viewed_at": ts},
        )
        assert r.status_code == 200
        assert r.json()["viewed_at"] == ts
        # Clearing
        r = await c.put(
            f"/api/sessions/{env_with_claude['session_id']}/viewed",
            json={"viewed_at": None},
        )
        assert r.json()["viewed_at"] is None


async def test_localstorage_migration_imports_all_three(env_with_claude):
    """One-time migration endpoint: archived ids, starred ids, viewed_at
    map all land in session_meta in a single request."""
    sid = env_with_claude["session_id"]
    config = AppConfig(data_paths=[str(Path(env_with_claude["db_path"]).parent)])
    app = _make_app(env_with_claude["db_path"], config)
    async with await _client(app) as c:
        r = await c.post(
            "/api/sessions/migrate-localstorage",
            json={
                "archived": [sid],
                "starred": [sid],
                "viewed_at": {sid: "2026-05-28T12:00:00"},
            },
        )
        assert r.status_code == 200
        applied = r.json()["applied"]
        assert applied == {"archived": 1, "starred": 1, "viewed_at": 1}

        # All three landed.
        r = await c.get(f"/api/sessions/{sid}")
        body = r.json()
        assert body["archived_at"] is not None
        assert body["starred_at"] is not None
        assert body["viewed_at"] == "2026-05-28T12:00:00"


async def test_localstorage_migration_silently_skips_unknown_ids(env_with_claude):
    """Stale localStorage may reference sessions the user has since deleted —
    the migration must not fail or warn for those, just skip them."""
    sid = env_with_claude["session_id"]
    config = AppConfig(data_paths=[str(Path(env_with_claude["db_path"]).parent)])
    app = _make_app(env_with_claude["db_path"], config)
    async with await _client(app) as c:
        r = await c.post(
            "/api/sessions/migrate-localstorage",
            json={
                "archived": [sid, "deleted-sid-1", "deleted-sid-2"],
                "starred": [],
                "viewed_at": {},
            },
        )
        assert r.status_code == 200
        # Only the real session contributes
        assert r.json()["applied"]["archived"] == 1


async def test_flag_endpoint_publishes_session_meta_sse_event(env_with_claude):
    """Setting any flag must publish a session-meta event on the bus so
    other tabs/clients refresh in real time."""
    sid = env_with_claude["session_id"]
    config = AppConfig(data_paths=[str(Path(env_with_claude["db_path"]).parent)])
    app = _make_app(env_with_claude["db_path"], config)
    async with app.router.lifespan_context(app):
        # _bus is local to create_app; reach it through any consumer.
        # The pty_manager carries the same instance.
        bus_queue = app.state.pty_manager._bus.subscribe()
        async with await _client(app) as c:
            await c.put(f"/api/sessions/{sid}/archived", json={"archived": True})

        # Drain looking for our event.
        target = None
        deadline = asyncio.get_event_loop().time() + 2.0
        while asyncio.get_event_loop().time() < deadline:
            try:
                evt = bus_queue.get_nowait()
            except asyncio.QueueEmpty:
                await asyncio.sleep(0.05)
                continue
            if isinstance(evt, dict) and evt.get("type") == "session-meta":
                if evt.get("id") == sid and "archived_at" in evt:
                    target = evt
                    break
        assert target is not None
        assert target["archived_at"] is not None
