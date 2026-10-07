"""Tests for Phase 8 — CLI & Config Polish."""

from __future__ import annotations

from datetime import date

import pytest

from agent_decoder.cli import _build_parser, _resolve_host
from agent_decoder.config import load_config


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


class TestParser:
    def test_subcommands_all_parse(self):
        for cmd in ["dashboard", "scan", "today", "stats", "tips", "migrate"]:
            args = _build_parser().parse_args([cmd])
            assert args.command == cmd

    def test_no_command_defaults_to_none(self):
        args = _build_parser().parse_args([])
        assert args.command is None

    def test_expose_flag(self):
        args = _build_parser().parse_args(["--expose"])
        assert args.expose is True

    def test_force_refresh_flag(self):
        args = _build_parser().parse_args(["--force-refresh"])
        assert args.force_refresh is True

    def test_since_parses_date(self):
        args = _build_parser().parse_args(["--since", "20260501"])
        assert args.since == date(2026, 5, 1)

    def test_since_invalid_format_raises(self):
        with pytest.raises(SystemExit):
            _build_parser().parse_args(["--since", "not-a-date"])

    def test_enable_edit_flag(self):
        args = _build_parser().parse_args(["--enable-edit"])
        assert args.enable_edit is True

    def test_path_append(self):
        args = _build_parser().parse_args(["--path", "/a", "--path", "/b"])
        assert args.paths == ["/a", "/b"]

    def test_port_override(self):
        args = _build_parser().parse_args(["--port", "9999"])
        assert args.port == 9999

    def test_host_override(self):
        args = _build_parser().parse_args(["--host", "0.0.0.0"])
        assert args.host == "0.0.0.0"

    def test_no_open_flag(self):
        args = _build_parser().parse_args(["--no-open"])
        assert args.no_open is True

    def test_combined_flags_with_subcommand(self):
        args = _build_parser().parse_args(
            [
                "--expose",
                "--force-refresh",
                "--since",
                "20260101",
                "--enable-edit",
                "--path",
                "/tmp/test",
                "stats",
            ]
        )
        assert args.expose is True
        assert args.force_refresh is True
        assert args.since == date(2026, 1, 1)
        assert args.enable_edit is True
        assert args.paths == ["/tmp/test"]
        assert args.command == "stats"


# ---------------------------------------------------------------------------
# Host resolution
# ---------------------------------------------------------------------------


class TestResolveHost:
    def test_default_is_localhost(self):
        args = _build_parser().parse_args([])
        assert _resolve_host(args) == "127.0.0.1"

    def test_expose_overrides_to_wildcard(self, capsys):
        args = _build_parser().parse_args(["--expose"])
        host = _resolve_host(args)
        assert host == "0.0.0.0"
        captured = capsys.readouterr()
        assert "WARNING" in captured.out

    def test_host_flag_respected(self):
        args = _build_parser().parse_args(["--host", "192.168.1.5"])
        assert _resolve_host(args) == "192.168.1.5"

    def test_expose_overrides_host(self, capsys):
        args = _build_parser().parse_args(["--expose", "--host", "192.168.1.5"])
        host = _resolve_host(args)
        assert host == "0.0.0.0"


# ---------------------------------------------------------------------------
# Force-refresh clears mtimes
# ---------------------------------------------------------------------------


class TestForceRefresh:
    @pytest.fixture
    def db_path(self, tmp_path):
        return tmp_path / "test.db"

    @pytest.mark.asyncio
    async def test_force_refresh_clears_mtimes(self, db_path):
        from agent_decoder.db import Database
        from agent_decoder.models import Project, Session

        async with Database(db_path) as db:
            await db.init_schema()
            proj = Project(
                id="p1", display_name="Test", raw_path="-t", data_source="test"
            )
            await db.upsert_project(proj)
            session = Session(
                id="s1",
                project_id="p1",
                file_path="/tmp/test.jsonl",
                title="Test Session",
            )
            await db.upsert_session(session, file_mtime=12345.0)
            # Confirm mtime was stored
            mtime = await db.get_session_mtime("s1")
            assert mtime == 12345.0

        from agent_decoder.cli import _force_refresh

        await _force_refresh(db_path)

        async with Database(db_path) as db:
            mtime = await db.get_session_mtime("s1")
        assert mtime is None


# ---------------------------------------------------------------------------
# Subcommand: scan
# ---------------------------------------------------------------------------


class TestScanCommand:
    def test_scan_runs_without_error(self, capsys):
        from agent_decoder.cli import _run_scan

        config = load_config()
        args = _build_parser().parse_args(["scan"])
        _run_scan(args, config)
        captured = capsys.readouterr()
        assert "Scanning..." in captured.out


# ---------------------------------------------------------------------------
# Subcommand: today
# ---------------------------------------------------------------------------


class TestTodayCommand:
    def test_today_runs_without_error(self, capsys):
        from agent_decoder.cli import _run_today

        config = load_config()
        args = _build_parser().parse_args(["today"])
        _run_today(args, config)
        captured = capsys.readouterr()
        # Either shows data or "No usage data" — both are valid
        assert ("Input tokens" in captured.out) or ("No usage data" in captured.out)


# ---------------------------------------------------------------------------
# Subcommand: stats
# ---------------------------------------------------------------------------


class TestStatsCommand:
    def test_stats_runs_without_error(self, capsys):
        from agent_decoder.cli import _run_stats

        config = load_config()
        args = _build_parser().parse_args(["stats"])
        _run_stats(args, config)
        captured = capsys.readouterr()
        assert "Prompt Stats" in captured.out


# ---------------------------------------------------------------------------
# Subcommand: tips
# ---------------------------------------------------------------------------


class TestTipsCommand:
    def test_tips_runs_without_error(self, capsys):
        from agent_decoder.cli import _run_tips

        config = load_config()
        args = _build_parser().parse_args(["tips"])
        _run_tips(args, config)
        captured = capsys.readouterr()
        # Either tips or "looking good" message
        assert len(captured.out) > 0


# ---------------------------------------------------------------------------
# DB execute/commit helpers
# ---------------------------------------------------------------------------


class TestDbHelpers:
    @pytest.mark.asyncio
    async def test_execute_and_commit(self, tmp_path):
        from agent_decoder.db import Database

        db_path = tmp_path / "test.db"
        async with Database(db_path) as db:
            await db.init_schema()
            await db.execute(
                "INSERT INTO projects (id, display_name, raw_path, data_source) VALUES (?, ?, ?, ?)",
                ("test-proj", "Test", "-t", "test"),
            )
            await db.commit()
        async with Database(db_path) as db:
            projects = await db.get_projects()
        assert any(p.id == "test-proj" for p in projects)
