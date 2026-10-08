"""Tests for scanner.py — Agent 2 must make all of these pass."""

import pytest


@pytest.fixture
def fake_claude_dir(tmp_path):
    """Build a fake ~/.claude directory structure."""
    projects = tmp_path / "projects"
    proj_a = projects / "-Users-alice-project-foo"
    proj_b = projects / "-Volumes-ExternalDrive-Work-bar"
    proj_a.mkdir(parents=True)
    proj_b.mkdir(parents=True)

    (proj_a / "aaaaaaaa-0000-0000-0000-000000000001.jsonl").write_text(
        '{"type":"custom-title","customTitle":"foo","sessionId":"aaaaaaaa-0000-0000-0000-000000000001"}\n'
    )
    (proj_a / "aaaaaaaa-0000-0000-0000-000000000002.jsonl").write_text(
        '{"type":"custom-title","customTitle":"foo2","sessionId":"aaaaaaaa-0000-0000-0000-000000000002"}\n'
    )
    (proj_b / "bbbbbbbb-0000-0000-0000-000000000001.jsonl").write_text(
        '{"type":"custom-title","customTitle":"bar","sessionId":"bbbbbbbb-0000-0000-0000-000000000001"}\n'
    )
    return tmp_path


class TestScanPaths:
    async def test_finds_all_sessions(self, fake_claude_dir):
        from agent_decoder.scanner import scan_paths

        results = []
        async for project, path in scan_paths([fake_claude_dir]):
            results.append((project, path))
        assert len(results) == 3

    async def test_yields_correct_project_ids(self, fake_claude_dir):
        from agent_decoder.scanner import scan_paths

        project_ids = set()
        async for project, _ in scan_paths([fake_claude_dir]):
            project_ids.add(project.id)
        assert len(project_ids) == 2

    async def test_handles_empty_directory(self, tmp_path):
        from agent_decoder.scanner import scan_paths

        results = []
        async for item in scan_paths([tmp_path]):
            results.append(item)
        assert results == []

    async def test_handles_nonexistent_path(self, tmp_path):
        from agent_decoder.scanner import scan_paths

        missing = tmp_path / "nonexistent"
        results = []
        async for item in scan_paths([missing]):
            results.append(item)
        assert results == []

    async def test_scans_multiple_roots(self, fake_claude_dir, tmp_path):
        from agent_decoder.scanner import scan_paths

        extra_root = tmp_path / "extra"
        extra_proj = extra_root / "projects" / "-extra-project"
        extra_proj.mkdir(parents=True)
        (extra_proj / "cccccccc-0000-0000-0000-000000000001.jsonl").write_text("{}\n")

        results = []
        async for item in scan_paths([fake_claude_dir, extra_root]):
            results.append(item)
        assert len(results) == 4

    async def test_skips_backup_files(self, fake_claude_dir):
        """Backup files (*.bak.*.jsonl) must not be yielded by the scanner."""
        from agent_decoder.scanner import scan_paths

        proj_dir = fake_claude_dir / "projects" / "-Users-alice-project-foo"
        (
            proj_dir / "aaaaaaaa-0000-0000-0000-000000000001.bak.20260505_120000.jsonl"
        ).write_text(
            '{"type":"custom-title","sessionId":"aaaaaaaa-0000-0000-0000-000000000001"}\n'
        )
        results = []
        async for _, path in scan_paths([fake_claude_dir]):
            results.append(path.name)
        assert not any(".bak." in name for name in results)
        # Originals still found
        assert "aaaaaaaa-0000-0000-0000-000000000001.jsonl" in results

    async def test_include_subagents_off_by_default(self, fake_claude_dir):
        """Sub-agent transcripts are never yielded unless opted in."""
        from agent_decoder.scanner import scan_paths

        proj_dir = fake_claude_dir / "projects" / "-Users-alice-project-foo"
        subagents_dir = proj_dir / "aaaaaaaa-0000-0000-0000-000000000001" / "subagents"
        subagents_dir.mkdir(parents=True)
        (subagents_dir / "agent-xyz.jsonl").write_text(
            '{"sessionId":"aaaaaaaa-0000-0000-0000-000000000001","isSidechain":true}\n'
        )

        results = []
        async for _, path in scan_paths([fake_claude_dir]):
            results.append(path)
        assert not any("subagents" in path.parts for path in results)

    async def test_include_subagents_on_yields_subagent_files(self, fake_claude_dir):
        """With include_subagents=True, agent-*.jsonl files under
        <session>/subagents/ are yielded in addition to the top-level sessions."""
        from agent_decoder.scanner import scan_paths

        proj_dir = fake_claude_dir / "projects" / "-Users-alice-project-foo"
        subagents_dir = proj_dir / "aaaaaaaa-0000-0000-0000-000000000001" / "subagents"
        subagents_dir.mkdir(parents=True)
        (subagents_dir / "agent-xyz.jsonl").write_text(
            '{"sessionId":"aaaaaaaa-0000-0000-0000-000000000001","isSidechain":true}\n'
        )

        results = []
        async for _, path in scan_paths([fake_claude_dir], include_subagents=True):
            results.append(path)
        subagent_paths = [p for p in results if "subagents" in p.parts]
        assert len(subagent_paths) == 1
        assert subagent_paths[0].name == "agent-xyz.jsonl"
        # Top-level sessions are still found alongside the subagent file.
        assert len(results) == 4

    async def test_include_subagents_skips_backup_files(self, fake_claude_dir):
        """Backup subagent files (*.bak.*.jsonl) are excluded too."""
        from agent_decoder.scanner import scan_paths

        proj_dir = fake_claude_dir / "projects" / "-Users-alice-project-foo"
        subagents_dir = proj_dir / "aaaaaaaa-0000-0000-0000-000000000001" / "subagents"
        subagents_dir.mkdir(parents=True)
        (subagents_dir / "agent-xyz.bak.20260505_120000.jsonl").write_text("{}\n")

        results = []
        async for _, path in scan_paths([fake_claude_dir], include_subagents=True):
            results.append(path)
        assert not any(".bak." in path.name for path in results)


class TestBuildProjectFromDir:
    def test_basic_path_parsing(self):
        from agent_decoder.scanner import build_project_from_dir

        proj = build_project_from_dir("-Users-alice-project", "~/.claude")
        assert proj.raw_path == "-Users-alice-project"
        assert proj.data_source == "~/.claude"
        assert "alice" in proj.display_name or "Users" in proj.display_name

    def test_project_id_is_stable(self):
        from agent_decoder.scanner import build_project_from_dir

        p1 = build_project_from_dir("-Users-alice-project", "~/.claude")
        p2 = build_project_from_dir("-Users-alice-project", "~/.claude")
        assert p1.id == p2.id

    def test_different_dirs_have_different_ids(self):
        from agent_decoder.scanner import build_project_from_dir

        p1 = build_project_from_dir("-Users-alice-foo", "~/.claude")
        p2 = build_project_from_dir("-Users-alice-bar", "~/.claude")
        assert p1.id != p2.id


class TestResolvePath:
    def test_resolves_existing_home(self):
        from agent_decoder.scanner import resolve_path
        import os

        home = os.path.expanduser("~")
        # Build a mangled version of home
        mangled = "-" + home.replace("/", "-").lstrip("-")
        result = resolve_path(mangled)
        # May or may not resolve, but should not raise
        assert result is None or isinstance(result, str)

    def test_returns_none_for_nonexistent(self):
        from agent_decoder.scanner import resolve_path

        result = resolve_path("-Volumes-NonExistent-Path-12345")
        assert result is None
