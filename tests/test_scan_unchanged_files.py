"""The periodic rescan must not re-parse files whose mtime has not changed.

Codex rollouts are named ``rollout-<timestamp>-<uuid>.jsonl`` but are indexed
under the session id inside the file (and a resumed rollout can carry several
``session_meta`` records, so the id isn't derivable from the name at all). The
rescan used to look the stored mtime up by file stem, found nothing for almost
every Codex file, and re-parsed ~5 GB of transcripts on every pass.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from agent_decoder.db import Database
from agent_decoder.models import AppConfig
from agent_decoder.providers.codex import CodexAdapter
from agent_decoder.server import create_app

FIXTURE = Path(__file__).parent / "fixtures" / "codex" / "sample_rollout.jsonl"


@pytest.fixture
async def scan_env(tmp_path, monkeypatch):
    codex_root = tmp_path / "codex" / "2026" / "06" / "03"
    codex_root.mkdir(parents=True)
    claude_root = tmp_path / "claude"
    claude_root.mkdir()
    db_path = tmp_path / "index.db"
    async with Database(db_path) as db:
        await db.init_schema()

    parses: list[str] = []
    original = CodexAdapter.parse

    def counting_parse(self, path):
        parses.append(path.name)
        return original(self, path)

    monkeypatch.setattr(CodexAdapter, "parse", counting_parse)
    config = AppConfig(
        data_paths=[str(claude_root)], codex_data_paths=[str(tmp_path / "codex")]
    )
    app = create_app(config, db_path)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield {"client": client, "dir": codex_root, "parses": parses, "db": db_path}


def _rollout(directory: Path, name: str) -> Path:
    path = directory / name
    shutil.copy(FIXTURE, path)  # payload id inside != anything in the filename
    return path


async def test_unchanged_codex_file_is_not_reparsed(scan_env):
    _rollout(
        scan_env["dir"],
        "rollout-2026-06-03T17-51-06-aaaa0000-0000-0000-0000-000000000001.jsonl",
    )
    client, parses = scan_env["client"], scan_env["parses"]

    assert (await client.post("/api/refresh")).status_code == 200
    assert len(parses) == 1  # first pass indexes it

    for _ in range(3):
        await client.post("/api/refresh")
    assert len(parses) == 1  # later passes with no file change parse nothing


async def test_files_sharing_a_session_id_are_not_reparsed(scan_env):
    # A resumed Codex session leaves several rollouts with the same session id:
    # one sessions row, so only one file's path can ever be stored.
    for n in (1, 2):
        _rollout(
            scan_env["dir"],
            f"rollout-2026-06-03T17-51-0{n}-bbbb0000-0000-0000-0000-00000000000{n}.jsonl",
        )
    client, parses = scan_env["client"], scan_env["parses"]

    await client.post("/api/refresh")
    assert len(parses) == 2
    await client.post("/api/refresh")
    await client.post("/api/refresh")
    assert len(parses) == 2


async def test_a_changed_file_is_still_reparsed(scan_env):
    path = _rollout(
        scan_env["dir"],
        "rollout-2026-06-03T17-51-06-cccc0000-0000-0000-0000-000000000003.jsonl",
    )
    client, parses = scan_env["client"], scan_env["parses"]
    await client.post("/api/refresh")
    assert len(parses) == 1

    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 10))  # the file grew / was touched
    await client.post("/api/refresh")
    assert len(parses) == 2
    await client.post("/api/refresh")
    assert len(parses) == 2


async def test_db_invalidation_still_forces_a_reparse(scan_env):
    # The DB stays authoritative: code that NULLs file_mtime to force a re-parse
    # (title migrations, force-refresh, patching a session) must not be masked
    # by the in-memory "already indexed" cache.
    _rollout(
        scan_env["dir"],
        "rollout-2026-06-03T17-51-06-eeee0000-0000-0000-0000-000000000005.jsonl",
    )
    client, parses = scan_env["client"], scan_env["parses"]
    await client.post("/api/refresh")
    await client.post("/api/refresh")
    assert len(parses) == 1

    async with Database(scan_env["db"]) as db:
        await db.clear_all_mtimes()
        await db.commit()
    await client.post("/api/refresh")
    assert len(parses) == 2  # invalidated -> re-parsed
    await client.post("/api/refresh")
    assert len(parses) == 2  # and settles again


async def test_legacy_row_keyed_by_file_name_does_not_cause_reparsing(scan_env):
    # An older Codex adapter indexed some rollouts under the FILE NAME as the
    # session id. That orphan row (stale mtime, same file_path) is never updated
    # by today's parse, so a lookup that preferred it made the file look
    # "changed" on every pass forever.
    from agent_decoder.models import Project, Session

    path = _rollout(
        scan_env["dir"],
        "rollout-2026-06-03T17-51-06-ffff0000-0000-0000-0000-000000000006.jsonl",
    )
    client, parses = scan_env["client"], scan_env["parses"]
    await client.post("/api/refresh")
    assert len(parses) == 1
    async with Database(scan_env["db"]) as db:
        await db.upsert_project(Project(id="legacy", display_name="L", raw_path="l"))
        await db.upsert_session(
            Session(
                id=path.stem,  # the legacy id: the file name
                project_id="legacy",
                file_path=str(path),
                provider="codex",
            ),
            file_mtime=1.0,  # stale forever
        )
    for _ in range(3):
        await client.post("/api/refresh")
    assert len(parses) == 1


async def test_get_indexed_file_mtime(scan_env):
    path = _rollout(
        scan_env["dir"],
        "rollout-2026-06-03T17-51-06-dddd0000-0000-0000-0000-000000000004.jsonl",
    )
    await scan_env["client"].post("/api/refresh")
    async with Database(scan_env["db"]) as db:
        found, mtime = await db.get_indexed_file_mtime(path.stem, str(path))
        assert (found, mtime) == (True, path.stat().st_mtime)
        assert await db.get_indexed_file_mtime("nope", "/no/such.jsonl") == (
            False,
            None,
        )
        await db.clear_all_mtimes()
        await db.commit()
        # Row still exists, mtime deliberately NULL: found=True, mtime=None.
        assert await db.get_indexed_file_mtime(path.stem, str(path)) == (True, None)
