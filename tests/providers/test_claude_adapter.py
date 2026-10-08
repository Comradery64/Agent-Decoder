"""Tests for ClaudeAdapter — verifies delegation is faithful (Phase 1 regression gate)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agent_decoder.models import AppConfig
from agent_decoder.providers import registry
from agent_decoder.providers.claude import ClaudeAdapter

FIXTURES = Path(__file__).parent.parent / "fixtures"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def adapter() -> ClaudeAdapter:
    return ClaudeAdapter()


@pytest.fixture(autouse=True)
def _isolated_registry():
    """Clear the registry before and after each test so state doesn't leak."""
    registry.clear()
    yield
    registry.clear()


@pytest.fixture()
def claude_layout(tmp_path: Path) -> tuple[Path, Path]:
    """Build a minimal <root>/projects/<proj>/<uuid>.jsonl layout.

    Returns ``(root, session_path)`` so tests can reference both.
    """
    proj_dir = tmp_path / "projects" / "-Users-alice-myproject"
    proj_dir.mkdir(parents=True)
    session_path = proj_dir / "aaaaaaaa-0000-0000-0000-000000000001.jsonl"
    shutil.copy(FIXTURES / "simple_session.jsonl", session_path)
    return tmp_path, session_path


# ---------------------------------------------------------------------------
# Identity / capabilities
# ---------------------------------------------------------------------------


class TestIdentity:
    def test_name(self, adapter: ClaudeAdapter) -> None:
        assert adapter.name == "claude"

    def test_capabilities_all_true(self, adapter: ClaudeAdapter) -> None:
        caps = adapter.capabilities
        assert caps.can_send is True
        assert caps.can_resume is True
        assert caps.can_fork is True
        assert caps.can_edit is True


# ---------------------------------------------------------------------------
# configured_roots
# ---------------------------------------------------------------------------


class TestConfiguredRoots:
    def test_default_expands_tilde(self, adapter: ClaudeAdapter) -> None:
        config = AppConfig()  # default data_paths = ["~/.claude"]
        roots = adapter.configured_roots(config)
        assert len(roots) >= 1
        # All paths must be absolute (no tilde)
        for root in roots:
            assert root.is_absolute(), f"Expected absolute path, got {root}"

    def test_respects_custom_data_paths(
        self, adapter: ClaudeAdapter, tmp_path: Path
    ) -> None:
        config = AppConfig(data_paths=[str(tmp_path)])
        roots = adapter.configured_roots(config)
        assert tmp_path in roots


# ---------------------------------------------------------------------------
# owns_path
# ---------------------------------------------------------------------------


class TestOwnsPath:
    def test_true_for_projects_jsonl(self, adapter: ClaudeAdapter) -> None:
        path = Path("/home/user/.claude/projects/-Users-alice-proj/abc123.jsonl")
        assert adapter.owns_path(path) is True

    def test_false_for_codex_path(self, adapter: ClaudeAdapter) -> None:
        # Codex sessions live under .codex/sessions/…, not under projects/
        path = Path("/home/user/.codex/sessions/rollout-abc123.jsonl")
        assert adapter.owns_path(path) is False

    def test_false_for_non_jsonl(self, adapter: ClaudeAdapter) -> None:
        path = Path("/home/user/.claude/projects/-foo/session.txt")
        assert adapter.owns_path(path) is False

    def test_false_for_jsonl_without_projects_dir(self, adapter: ClaudeAdapter) -> None:
        path = Path("/tmp/some-flat-dir/session.jsonl")
        assert adapter.owns_path(path) is False


# ---------------------------------------------------------------------------
# parse — delegation is faithful
# ---------------------------------------------------------------------------


class TestParse:
    def test_returns_session_and_messages(
        self, adapter: ClaudeAdapter, claude_layout: tuple[Path, Path]
    ) -> None:
        _, session_path = claude_layout
        session, messages = adapter.parse(session_path)
        assert session is not None
        assert isinstance(messages, list)
        assert len(messages) > 0

    def test_provider_is_claude(
        self, adapter: ClaudeAdapter, claude_layout: tuple[Path, Path]
    ) -> None:
        _, session_path = claude_layout
        session, _ = adapter.parse(session_path)
        assert session.provider == "claude"

    def test_matches_parse_session_output(
        self, adapter: ClaudeAdapter, claude_layout: tuple[Path, Path]
    ) -> None:
        """Delegation must be faithful — same result as calling parse_session directly."""
        from agent_decoder.parser import parse_session

        _, session_path = claude_layout
        adapter_session, adapter_messages = adapter.parse(session_path)
        direct_session, direct_messages = parse_session(session_path)

        assert adapter_session.id == direct_session.id
        assert adapter_session.title == direct_session.title
        assert len(adapter_messages) == len(direct_messages)


# ---------------------------------------------------------------------------
# discover — async generator delegates to scan_paths
# ---------------------------------------------------------------------------


class TestDiscover:
    async def test_yields_session_in_claude_layout(
        self, adapter: ClaudeAdapter, claude_layout: tuple[Path, Path]
    ) -> None:
        root, session_path = claude_layout
        results = []
        async for project, path in adapter.discover([root]):
            results.append((project, path))
        assert len(results) == 1
        assert results[0][1] == session_path

    async def test_empty_for_missing_root(
        self, adapter: ClaudeAdapter, tmp_path: Path
    ) -> None:
        missing = tmp_path / "nonexistent"
        results = []
        async for item in adapter.discover([missing]):
            results.append(item)
        assert results == []

    async def test_yields_nothing_for_root_without_projects(
        self, adapter: ClaudeAdapter, tmp_path: Path
    ) -> None:
        results = []
        async for item in adapter.discover([tmp_path]):
            results.append(item)
        assert results == []
