"""Tests for POST /api/sessions/{id}/open-terminal shell quoting.

Verifies that cwd and worktree names containing spaces are properly quoted in
the shell command strings so `cd` and the binary invocation don't break.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import AsyncIterator
from unittest.mock import patch

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
    session_id: str,
    cwd: str,
    file_path: str,
    is_worktree: bool = False,
) -> None:
    async with Database(db_path) as db:
        await db.init_schema()
        project = Project(
            id="proj-term",
            display_name="terminal-test",
            raw_path="-terminal",
            data_source="test",
        )
        session = Session(
            id=session_id,
            project_id=project.id,
            file_path=file_path,
            cwd=cwd,
            is_worktree=is_worktree,
        )
        await db.upsert_project(project)
        await db.upsert_session(session)


@pytest.fixture
async def spaced_cwd_env(tmp_path) -> AsyncIterator[dict]:
    """Session whose cwd contains spaces."""
    db_path = tmp_path / "term.db"
    projects = tmp_path / "root" / ".claude" / "projects" / "-terminal"
    projects.mkdir(parents=True)
    session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    file_path = projects / f"{session_id}.jsonl"
    file_path.write_text("")

    cwd = tmp_path / "My Projects" / "foo bar"
    cwd.mkdir(parents=True)

    await _seed_session(
        db_path,
        session_id=session_id,
        cwd=str(cwd),
        file_path=str(file_path),
    )
    yield {"db_path": db_path, "cwd": str(cwd), "session_id": session_id}


# ---------------------------------------------------------------------------
# cwd quoting
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS AppleScript branch")
async def test_open_terminal_quotes_spaced_cwd_darwin(spaced_cwd_env):
    """AppleScript do script contains single-quoted cwd when cwd has spaces."""
    e = spaced_cwd_env
    app = _make_app(e["db_path"], AppConfig())

    with patch("subprocess.Popen") as mock_popen:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            r = await c.post(f"/api/sessions/{e['session_id']}/open-terminal")

    assert r.status_code == 200
    script = mock_popen.call_args[0][0][2]  # osascript -e <script>
    assert "unset ANTHROPIC_API_KEY" in script
    assert f"cd '{e['cwd']}'" in script


@pytest.mark.skipif(sys.platform == "darwin", reason="Linux branch only")
async def test_open_terminal_quotes_spaced_cwd_linux(spaced_cwd_env, monkeypatch):
    """bash -c string contains single-quoted cwd when cwd has spaces."""
    e = spaced_cwd_env
    app = _make_app(e["db_path"], AppConfig())

    # Simulate the x-terminal-emulator fallback (no gnome/konsole/xfce).
    monkeypatch.setattr("shutil.which", lambda _name: None)

    with patch("subprocess.Popen") as mock_popen:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            r = await c.post(f"/api/sessions/{e['session_id']}/open-terminal")

    assert r.status_code == 200
    bash_cmd = mock_popen.call_args[0][0][-1]  # last arg is the -c string
    assert "unset ANTHROPIC_API_KEY" in bash_cmd
    assert f"cd '{e['cwd']}'" in bash_cmd


# ---------------------------------------------------------------------------
# worktree name quoting
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS AppleScript branch")
async def test_open_terminal_quotes_spaced_worktree_darwin(tmp_path):
    """Worktree name with spaces is single-quoted in the -w argument."""
    db_path = tmp_path / "wt.db"
    projects = tmp_path / "root" / ".claude" / "projects" / "-terminal"
    projects.mkdir(parents=True)
    session_id = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    file_path = projects / f"{session_id}.jsonl"
    file_path.write_text("")

    wt_name = "my feature branch"
    cwd = tmp_path / "proj" / ".claude" / "worktrees" / wt_name
    cwd.mkdir(parents=True)

    await _seed_session(
        db_path,
        session_id=session_id,
        cwd=str(cwd),
        file_path=str(file_path),
        is_worktree=True,
    )

    app = _make_app(db_path, AppConfig())
    with patch("subprocess.Popen") as mock_popen:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            r = await c.post(f"/api/sessions/{session_id}/open-terminal")

    assert r.status_code == 200
    script = mock_popen.call_args[0][0][2]  # osascript -e <script>
    assert "unset ANTHROPIC_API_KEY" in script
    assert f"-w '{wt_name}'" in script


# ---------------------------------------------------------------------------
# refusal guards
# ---------------------------------------------------------------------------


async def test_open_terminal_400_root_cwd(tmp_path):
    """Session whose cwd is the filesystem root → 400 refusal, never spawns."""
    db_path = tmp_path / "root-cwd.db"
    projects = tmp_path / "root" / ".claude" / "projects" / "-terminal"
    projects.mkdir(parents=True)
    session_id = "00000000-0000-0000-0000-000000000000"
    file_path = projects / f"{session_id}.jsonl"
    file_path.write_text("")

    await _seed_session(
        db_path,
        session_id=session_id,
        cwd="/",
        file_path=str(file_path),
    )

    app = _make_app(db_path, AppConfig())
    with patch("subprocess.Popen") as mock_popen:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            r = await c.post(f"/api/sessions/{session_id}/open-terminal")
    assert r.status_code == 400
    assert "filesystem root" in r.json()["detail"]
    mock_popen.assert_not_called()


# ---------------------------------------------------------------------------
# 404 guards
# ---------------------------------------------------------------------------


async def test_open_terminal_404_unknown_session(tmp_path):
    """Unknown session → 404."""
    db_path = tmp_path / "empty.db"
    async with Database(db_path) as db:
        await db.init_schema()
    app = _make_app(db_path, AppConfig())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        r = await c.post("/api/sessions/no-such-session/open-terminal")
    assert r.status_code == 404


async def test_open_terminal_404_missing_cwd(tmp_path):
    """Session whose cwd no longer exists on disk → 404."""
    db_path = tmp_path / "gone.db"
    projects = tmp_path / "root" / ".claude" / "projects" / "-terminal"
    projects.mkdir(parents=True)
    session_id = "12345678-1234-1234-1234-123456789012"
    file_path = projects / f"{session_id}.jsonl"
    file_path.write_text("")

    await _seed_session(
        db_path,
        session_id=session_id,
        cwd="/nonexistent/path/that/does/not/exist",
        file_path=str(file_path),
    )

    app = _make_app(db_path, AppConfig())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        r = await c.post(f"/api/sessions/{session_id}/open-terminal")
    assert r.status_code == 404
    assert "Directory not found" in r.json()["detail"]
