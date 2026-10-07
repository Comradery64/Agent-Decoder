"""Tests for the recap feature: DB layer + HTTP routes.

Recap uses a hidden-PTY fork-session spawn. The integration here uses
``fake_claude_tui.py``, which mirrors real claude's TUI behavior (TTY
required, cbreak, emits ``\\x1b[?2004h``, writes user+assistant JSONL on CR).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import time
from pathlib import Path
from typing import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient

from agent_decoder.db import Database
from agent_decoder.models import AppConfig, Project, Session


FAKE = Path(__file__).parent / "fixtures" / "fake_claude_tui.py"


# ---------------------------------------------------------------------------
# Helpers (mirror the patterns in test_server.py)
# ---------------------------------------------------------------------------


def _write_shim(dir_: Path, bin_name: str = "claude", extra_argv: str = "") -> Path:
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
    session_id: str,
    cwd: str,
    file_path: str,
    is_fork: bool = False,
) -> None:
    async with Database(db_path) as db:
        await db.init_schema()
        project = Project(
            id="proj-rc",
            display_name="recap-test",
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


async def _client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def recap_env(tmp_path, monkeypatch) -> AsyncIterator[dict]:
    """Tmp DB + a real session + ``claude`` TUI shim that echoes a canned
    recap response.

    Path layout matches what real claude produces: the source JSONL lives
    at ``<CLAUDE_CONFIG_DIR>/projects/<encoded_cwd>/<session_id>.jsonl``
    where ``encoded_cwd = '-' + cwd.replace('/', '-')``. The recap runner
    derives the fork JSONL path from the source JSONL's parent, so the
    test setup MUST mirror that layout — otherwise the runner won't find
    the fake's output.
    """
    db_path = tmp_path / "test.db"
    config_dir = tmp_path / "root"
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    # Resolve symlinks so the encoded path matches what the spawned
    # ``claude`` child sees via ``os.getcwd()``. On macOS ``/tmp`` is a
    # symlink to ``/private/tmp``; without ``resolve()`` the test would
    # encode ``--tmp-...`` while the fake writes to ``--private-tmp-...``.
    canonical_cwd = cwd.resolve()

    # Mirror the real claude project layout under the test config dir
    # so the recap runner's ``source_jsonl_path.parent / fork.jsonl``
    # lands in the same directory the fake writes to. The fake reads
    # ``CLAUDE_CONFIG_DIR`` to pick its output root, so we set both.
    encoded_cwd = "-" + str(canonical_cwd).replace("/", "-")
    project_dir = config_dir / "projects" / encoded_cwd
    project_dir.mkdir(parents=True)
    session_id = "22222222-3333-4444-5555-666666666666"
    file_path = project_dir / f"{session_id}.jsonl"
    file_path.write_text("")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_shim(bin_dir, extra_argv="--canned-response bullet-recap")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))

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
        "config_dir": config_dir,
    }


# ---------------------------------------------------------------------------
# DB tests
# ---------------------------------------------------------------------------


async def test_db_recap_insert_and_list_roundtrip(tmp_path):
    db_path = tmp_path / "db.sqlite"
    async with Database(db_path) as db:
        await db.init_schema()
        rid1 = await db.insert_recap("sess-1", "first recap", "msg-aaa")
        rid2 = await db.insert_recap("sess-1", "second recap", "msg-bbb")
        rid_other = await db.insert_recap("sess-2", "other", None)
        assert rid1 != rid2

        rows = await db.list_recaps("sess-1")
        assert [r["id"] for r in rows] == [rid1, rid2]  # oldest → newest
        assert rows[0]["text"] == "first recap"
        assert rows[0]["covers_until_message_uuid"] == "msg-aaa"
        assert rows[0]["dismissed"] is False
        assert rows[0]["session_id"] == "sess-1"

        other_rows = await db.list_recaps("sess-2")
        assert len(other_rows) == 1
        assert other_rows[0]["id"] == rid_other
        assert other_rows[0]["covers_until_message_uuid"] is None


async def test_db_recap_dismiss(tmp_path):
    db_path = tmp_path / "db.sqlite"
    async with Database(db_path) as db:
        await db.init_schema()
        rid = await db.insert_recap("sess-1", "to be dismissed", None)
        assert await db.dismiss_recap(rid) is True
        # Idempotent: dismissing missing row returns False
        assert await db.dismiss_recap(99999) is False
        # Default list excludes dismissed
        assert await db.list_recaps("sess-1") == []
        # Including dismissed brings it back
        rows = await db.list_recaps("sess-1", include_dismissed=True)
        assert len(rows) == 1
        assert rows[0]["dismissed"] is True


# ---------------------------------------------------------------------------
# Endpoint: POST /api/sessions/{id}/recap
# ---------------------------------------------------------------------------


async def test_recap_endpoint_generates_and_stores(recap_env):
    e = recap_env
    app = _make_app(e["db_path"], AppConfig())
    async with await _client(app) as c:
        r = await c.post(f"/api/sessions/{e['session_id']}/recap")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["text"] == "bullet-recap"
    assert body["session_id"] == e["session_id"]
    assert body["dismissed"] is False
    assert "id" in body
    assert "created_at" in body

    # Stored — visible via the list endpoint
    async with await _client(app) as c:
        r2 = await c.get(f"/api/sessions/{e['session_id']}/recaps")
    assert r2.status_code == 200
    rows = r2.json()
    assert len(rows) == 1
    assert rows[0]["text"] == "bullet-recap"


async def test_recap_endpoint_rejects_fork_session(recap_env):
    e = recap_env
    # Re-seed with is_fork=True.
    await _seed_session(
        e["db_path"],
        session_id=e["session_id"],
        cwd=e["cwd"],
        file_path=e["file_path"],
        is_fork=True,
    )
    app = _make_app(e["db_path"], AppConfig())
    async with await _client(app) as c:
        r = await c.post(f"/api/sessions/{e['session_id']}/recap")
    assert r.status_code == 422
    assert "fork" in r.json()["detail"].lower()


async def test_recap_endpoint_503_when_bin_missing(recap_env, monkeypatch):
    e = recap_env
    monkeypatch.setenv("PATH", "/nonexistent-dir")
    app = _make_app(e["db_path"], AppConfig())
    async with await _client(app) as c:
        r = await c.post(f"/api/sessions/{e['session_id']}/recap")
    assert r.status_code == 503


# ---------------------------------------------------------------------------
# Endpoint: GET /api/sessions/{id}/recaps
# ---------------------------------------------------------------------------


async def test_list_recaps_excludes_dismissed_by_default(recap_env):
    e = recap_env
    async with Database(e["db_path"]) as db:
        keep = await db.insert_recap(e["session_id"], "keeper", "uuid-1")
        gone = await db.insert_recap(e["session_id"], "dismissed", "uuid-2")
        await db.dismiss_recap(gone)
    app = _make_app(e["db_path"], AppConfig())
    async with await _client(app) as c:
        r = await c.get(f"/api/sessions/{e['session_id']}/recaps")
    assert r.status_code == 200
    rows = r.json()
    assert [row["id"] for row in rows] == [keep]
    assert all(row["dismissed"] is False for row in rows)


async def test_list_recaps_includes_when_flagged(recap_env):
    e = recap_env
    async with Database(e["db_path"]) as db:
        keep = await db.insert_recap(e["session_id"], "keeper", "uuid-1")
        gone = await db.insert_recap(e["session_id"], "dismissed", "uuid-2")
        await db.dismiss_recap(gone)
    app = _make_app(e["db_path"], AppConfig())
    async with await _client(app) as c:
        r = await c.get(
            f"/api/sessions/{e['session_id']}/recaps",
            params={"include_dismissed": "true"},
        )
    assert r.status_code == 200
    rows = r.json()
    ids = [row["id"] for row in rows]
    assert keep in ids and gone in ids
    flags = {row["id"]: row["dismissed"] for row in rows}
    assert flags[gone] is True
    assert flags[keep] is False


# ---------------------------------------------------------------------------
# Endpoint: POST /api/sessions/{id}/recaps/{recap_id}/dismiss
# ---------------------------------------------------------------------------


async def test_dismiss_endpoint_404_when_missing(recap_env):
    e = recap_env
    app = _make_app(e["db_path"], AppConfig())
    async with await _client(app) as c:
        r = await c.post(f"/api/sessions/{e['session_id']}/recaps/99999/dismiss")
    assert r.status_code == 404


async def test_dismiss_endpoint_happy_path(recap_env):
    e = recap_env
    async with Database(e["db_path"]) as db:
        rid = await db.insert_recap(e["session_id"], "dismiss me", None)
    app = _make_app(e["db_path"], AppConfig())
    async with await _client(app) as c:
        r = await c.post(f"/api/sessions/{e['session_id']}/recaps/{rid}/dismiss")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "dismissed": True}


# ---------------------------------------------------------------------------
# Argv verification — recap uses a forked hidden PTY and inherits model choice.
# ---------------------------------------------------------------------------


async def test_recap_argv_shape(recap_env, monkeypatch):
    """After Phase 8 recap spawns ``claude --session-id <fork>
    --resume <source> --fork-session --permission-mode dontAsk`` on a
    hidden PTY (TUI mode, no ``--model`` override).
    """
    e = recap_env
    capture = e["bin_dir"].parent / "recap_argv.json"
    bin_dir2 = e["bin_dir"].parent / "bin_capture_recap"
    bin_dir2.mkdir()
    _write_shim(
        bin_dir2,
        extra_argv=f"--capture-argv {capture} --canned-response captured",
    )
    monkeypatch.setenv("PATH", f"{bin_dir2}{os.pathsep}{os.environ['PATH']}")

    app = _make_app(e["db_path"], AppConfig())
    async with await _client(app) as c:
        r = await c.post(f"/api/sessions/{e['session_id']}/recap")
    assert r.status_code == 200, r.text

    deadline = time.monotonic() + 5.0
    while not capture.exists() and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert capture.exists(), "fake_claude_tui never wrote argv capture file"
    argv = json.loads(capture.read_text())
    assert "--session-id" in argv
    assert "--fork-session" in argv
    assert "--resume" in argv
    assert argv[argv.index("--resume") + 1] == e["session_id"]
    # The fork must be spawned with a fresh session id, not the source's.
    assert argv[argv.index("--session-id") + 1] != e["session_id"]
    # TUI mode: no alternate transport plumbing or retired non-TUI flags.
    retired_one_shot_flag = "--" + "print"
    assert retired_one_shot_flag not in argv
    assert "--input-format" not in argv
    no_persist_flag = "--no-session-" + "persistence"
    assert no_persist_flag not in argv
    # --model is intentionally NOT passed; let the fork inherit the source
    # session's model so cc-mirror provider mappings stay valid.
    assert "--model" not in argv
