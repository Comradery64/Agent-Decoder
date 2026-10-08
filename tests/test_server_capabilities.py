"""Phase 4b — server capability gating, /api/providers, provider-aware
open-terminal. No tmux/codex binary needed: every assertion here is about the
*gate*, which fires before any driver spawn.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import AsyncIterator
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from agent_decoder.db import Database
from agent_decoder.drivers import DriverAvailability
from agent_decoder.models import AppConfig, Message, Project, Session, TextBlock


def _make_app(db_path: Path, config: AppConfig):
    from agent_decoder.server import create_app

    return create_app(config, db_path)


async def _seed(db_path: Path, *, session_id: str, provider: str, cwd: str, fp: str):
    async with Database(db_path) as db:
        await db.init_schema()
        project = Project(
            id=f"proj-{provider}",
            display_name="cap-test",
            raw_path="-cap",
            data_source="test",
            resolved_path=cwd,
        )
        await db.upsert_project(project)
        await db.upsert_session(
            Session(
                id=session_id,
                project_id=project.id,
                file_path=fp,
                cwd=cwd,
                provider=provider,
            )
        )


@pytest.fixture
async def env(tmp_path) -> AsyncIterator[dict]:
    db_path = tmp_path / "cap.db"
    cwd = tmp_path / "work"
    cwd.mkdir()
    codex_fp = tmp_path / "rollout-codex.jsonl"
    codex_fp.write_text("")
    claude_fp = tmp_path / "claude.jsonl"
    claude_fp.write_text("")
    await _seed(
        db_path,
        session_id="codex-sess-1",
        provider="codex",
        cwd=str(cwd),
        fp=str(codex_fp),
    )
    await _seed(
        db_path,
        session_id="claude-sess-1",
        provider="claude",
        cwd=str(cwd),
        fp=str(claude_fp),
    )
    yield {"db_path": db_path, "cwd": str(cwd)}


# ---------------------------------------------------------------------------
# /api/providers
# ---------------------------------------------------------------------------


def _force_drivable(monkeypatch, available: bool):
    """Pin Codex runtime drivability so gate tests don't depend on the host
    having (or lacking) tmux+codex."""
    monkeypatch.setattr(
        "agent_decoder.server._driver_availability",
        lambda p: DriverAvailability(
            available=available, reason=None if available else "no tmux"
        ),
    )


async def test_providers_endpoint_shape(env, monkeypatch):
    _force_drivable(monkeypatch, True)
    app = _make_app(env["db_path"], AppConfig())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        r = await c.get("/api/providers")
    assert r.status_code == 200
    by_name = {p["name"]: p for p in r.json()}
    assert {"claude", "codex"} <= set(by_name)

    codex = by_name["codex"]
    # Phase 4e: Codex is drivable; with the backend available, effective follows.
    assert codex["caps"]["can_send"] is True
    assert codex["effective"]["can_send"] is True
    assert codex["driver_backed"] is True
    # fork/edit remain off even when drivable.
    assert codex["effective"]["can_edit"] is False

    claude = by_name["claude"]
    # Claude is not driver-backed; it sends over its own PTY path → can_send.
    assert claude["effective"]["can_send"] is True
    assert claude["driver_backed"] is False
    assert claude["availability"]["available"] is True


async def test_providers_availability_degrades_without_backend(env, monkeypatch):
    # Codex caps are now True, but a box without tmux must still degrade to
    # read-only: effective = static caps AND availability.
    _force_drivable(monkeypatch, False)
    app = _make_app(env["db_path"], AppConfig())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        r = await c.get("/api/providers")
    codex = next(p for p in r.json() if p["name"] == "codex")
    assert codex["caps"]["can_send"] is True  # static cap flipped (4e)
    assert codex["availability"]["available"] is False
    assert codex["effective"]["can_send"] is False  # ...but not drivable here


# ---------------------------------------------------------------------------
# Capability gate — read-only degrade vs gate-open
# ---------------------------------------------------------------------------


async def test_pty_submit_codex_409_when_not_drivable(env, monkeypatch):
    # No tmux/codex → Codex degrades to read-only; submit must 409, never fall
    # through to spawn a (wrong) claude PTY on the Codex cwd.
    _force_drivable(monkeypatch, False)
    app = _make_app(env["db_path"], AppConfig())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        r = await c.post(
            "/api/pty/submit", json={"session_id": "codex-sess-1", "content": "hi"}
        )
    assert r.status_code == 409
    assert r.json()["detail"]["kind"] == "capability_unsupported"
    assert r.json()["detail"]["capability"] == "can_send"
    assert r.json()["detail"]["provider"] == "codex"


async def test_pty_focus_codex_409_when_not_drivable(env, monkeypatch):
    _force_drivable(monkeypatch, False)
    app = _make_app(env["db_path"], AppConfig())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        r = await c.post("/api/pty/focus", json={"session_id": "codex-sess-1"})
    assert r.status_code == 409
    assert r.json()["detail"]["kind"] == "capability_unsupported"


async def test_pty_submit_codex_gate_opens_when_drivable(env, monkeypatch):
    # When drivable, the capability gate PASSES (no 409 capability_unsupported)
    # and routes to the DriverManager. Without a lifespan the manager is None,
    # so we expect 503 "driver manager not ready" — proof the gate opened.
    _force_drivable(monkeypatch, True)
    app = _make_app(env["db_path"], AppConfig())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        r = await c.post(
            "/api/pty/submit", json={"session_id": "codex-sess-1", "content": "hi"}
        )
    assert r.status_code == 503
    assert "driver manager" in r.json()["detail"]


# Claude is never capability-gated (it owns its direct-PTY path). That
# guarantee is asserted positively by test_providers_endpoint_shape
# (claude.effective.can_send is True) and negatively by the pre-existing
# Claude pty_submit suite remaining green — no fragile lifespan-less stub here.


# ---------------------------------------------------------------------------
# Provider-aware open-terminal
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS AppleScript branch")
async def test_open_terminal_codex_uses_codex_resume(env):
    app = _make_app(env["db_path"], AppConfig())
    with patch("subprocess.Popen") as mock_popen:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            r = await c.post("/api/sessions/codex-sess-1/open-terminal")
    assert r.status_code == 200
    script = mock_popen.call_args[0][0][2]
    assert "codex resume codex-sess-1" in script
    assert " -r codex-sess-1" not in script  # never the claude flag


# ---------------------------------------------------------------------------
# Edit-route capability gate (defense in depth — never corrupt a Codex rollout)
# ---------------------------------------------------------------------------


async def _seed_codex_message(db_path: str, *, session_id: str, message_id: str):
    async with Database(db_path) as db:
        await db.upsert_messages(
            [
                Message(
                    id=message_id,
                    session_id=session_id,
                    role="user",
                    content_blocks=[TextBlock(text="hello")],
                    provider="codex",
                )
            ]
        )


async def test_delete_message_codex_returns_409(env):
    await _seed_codex_message(
        env["db_path"], session_id="codex-sess-1", message_id="codex-msg-1"
    )
    app = _make_app(env["db_path"], AppConfig(edit_enabled=True))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        r = await c.delete("/api/messages/codex-msg-1")
    assert r.status_code == 409
    assert r.json()["detail"]["kind"] == "capability_unsupported"
    assert r.json()["detail"]["capability"] == "can_edit"


async def test_patch_message_codex_returns_409(env):
    await _seed_codex_message(
        env["db_path"], session_id="codex-sess-1", message_id="codex-msg-2"
    )
    app = _make_app(env["db_path"], AppConfig(edit_enabled=True))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        r = await c.patch(
            "/api/messages/codex-msg-2",
            json={"content_blocks": [{"type": "text", "text": "edited"}]},
        )
    assert r.status_code == 409
    assert r.json()["detail"]["capability"] == "can_edit"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS AppleScript branch")
async def test_open_terminal_claude_unchanged(env):
    app = _make_app(env["db_path"], AppConfig())
    with patch("subprocess.Popen") as mock_popen:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            r = await c.post("/api/sessions/claude-sess-1/open-terminal")
    assert r.status_code == 200
    script = mock_popen.call_args[0][0][2]
    assert "-r claude-sess-1" in script
    assert "codex resume" not in script
