"""Tests for providers.registry — round-trip, error paths, ordering, path dispatch."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from agent_decoder.models import AppConfig, Message, Project, Session
from agent_decoder.providers import registry
from agent_decoder.providers.base import ProviderAdapter, ProviderCaps


# ---------------------------------------------------------------------------
# Minimal dummy adapter for test use
# ---------------------------------------------------------------------------


class _FakeAdapter(ProviderAdapter):
    """A concrete stub that matches files by suffix."""

    def __init__(self, name: str, suffix: str = ".jsonl") -> None:
        self.name = name
        self._suffix = suffix

    @property
    def capabilities(self) -> ProviderCaps:
        return ProviderCaps(
            can_send=False, can_resume=False, can_fork=False, can_edit=False
        )

    def configured_roots(self, config: AppConfig) -> list[Path]:
        return []

    async def discover(
        self, roots: list[Path], include_subagents: bool = False
    ) -> AsyncIterator[tuple[Project, Path]]:
        # Empty async generator — yields nothing.
        return
        yield  # make Python treat this as a generator function

    def parse(self, path: Path) -> tuple[Session, list[Message]]:
        session = Session(
            id="stub-session",
            project_id="stub-project",
            file_path=str(path),
        )
        return session, []

    def owns_path(self, path: Path) -> bool:
        return path.suffix == self._suffix


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_registry():
    """Ensure the global registry is empty before and after each test."""
    registry.clear()
    yield
    registry.clear()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRegisterAndGet:
    def test_register_get_roundtrip(self):
        adapter = _FakeAdapter("claude")
        registry.register(adapter)
        assert registry.get("claude") is adapter

    def test_register_overwrites_existing(self):
        a1 = _FakeAdapter("claude")
        a2 = _FakeAdapter("claude")
        registry.register(a1)
        registry.register(a2)
        assert registry.get("claude") is a2

    def test_get_unknown_raises_key_error(self):
        with pytest.raises(KeyError, match="codex"):
            registry.get("codex")

    def test_key_error_message_lists_registered(self):
        registry.register(_FakeAdapter("claude"))
        with pytest.raises(KeyError) as exc_info:
            registry.get("codex")
        # The error text should mention what IS registered so it's actionable.
        assert "claude" in str(exc_info.value)


class TestAllAdapters:
    def test_empty_when_nothing_registered(self):
        assert registry.all_adapters() == []

    def test_returns_registered_in_insertion_order(self):
        a = _FakeAdapter("aaa")
        b = _FakeAdapter("bbb")
        c = _FakeAdapter("ccc")
        registry.register(a)
        registry.register(b)
        registry.register(c)
        assert registry.all_adapters() == [a, b, c]

    def test_overwrite_preserves_original_insertion_slot(self):
        """Replacing an adapter keeps it in the same position, not appended."""
        a1 = _FakeAdapter("aaa")
        b = _FakeAdapter("bbb")
        a2 = _FakeAdapter("aaa")
        registry.register(a1)
        registry.register(b)
        registry.register(a2)
        # dict update replaces in-place, preserving insertion order
        adapters = registry.all_adapters()
        assert adapters[0] is a2
        assert adapters[1] is b


class TestAdapterForPath:
    def test_returns_matching_adapter(self):
        adapter = _FakeAdapter("claude", suffix=".jsonl")
        registry.register(adapter)
        result = registry.adapter_for_path(Path("/some/path/session.jsonl"))
        assert result is adapter

    def test_returns_none_when_no_match(self):
        registry.register(_FakeAdapter("claude", suffix=".jsonl"))
        result = registry.adapter_for_path(Path("/some/path/export.csv"))
        assert result is None

    def test_returns_first_match_in_insertion_order(self):
        first = _FakeAdapter("first", suffix=".jsonl")
        second = _FakeAdapter("second", suffix=".jsonl")
        registry.register(first)
        registry.register(second)
        result = registry.adapter_for_path(Path("/x/session.jsonl"))
        assert result is first

    def test_returns_none_when_registry_empty(self):
        assert registry.adapter_for_path(Path("/x/session.jsonl")) is None


class TestClear:
    def test_clear_empties_registry(self):
        registry.register(_FakeAdapter("claude"))
        registry.register(_FakeAdapter("codex"))
        registry.clear()
        assert registry.all_adapters() == []

    def test_clear_makes_get_raise(self):
        registry.register(_FakeAdapter("claude"))
        registry.clear()
        with pytest.raises(KeyError):
            registry.get("claude")
