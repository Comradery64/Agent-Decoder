"""Claude Code provider adapter.

Wraps the existing ``scanner.scan_paths`` and ``parser.parse_session``
implementations behind the ``ProviderAdapter`` seam.  No scanning or parsing
logic lives here — this is pure delegation.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

from ..models import AppConfig, Message, Project, Session
from .base import ProviderAdapter, ProviderCaps


class ClaudeAdapter(ProviderAdapter):
    """Adapter for Claude Code's on-disk JSONL session format.

    Session layout::

        <root>/projects/<mangled-dir>/<uuid>.jsonl

    All four interactive operations (send / resume / fork / edit) are
    supported because Claude Code exposes a full PTY interface.
    """

    name = "claude"

    # -- Capability declaration -----------------------------------------------

    @property
    def capabilities(self) -> ProviderCaps:
        return ProviderCaps(
            can_send=True, can_resume=True, can_fork=True, can_edit=True
        )

    # -- Config-aware root resolution -----------------------------------------

    def configured_roots(self, config: AppConfig) -> list[Path]:
        """Return the expanded root directories configured for Claude Code.

        Mirrors ``server._all_scan_roots()`` — reads ``config.get_all_scan_paths()``
        and expands ``~`` / env vars.
        """
        return [Path(p).expanduser() for p in config.get_all_scan_paths()]

    # -- Discovery (async generator) ------------------------------------------

    async def discover(
        self, roots: list[Path], include_subagents: bool = False
    ) -> AsyncIterator[tuple[Project, Path]]:
        """Yield ``(Project, session_file_path)`` by delegating to ``scan_paths``."""
        from ..scanner import scan_paths

        async for item in scan_paths(roots, include_subagents=include_subagents):
            yield item

    # -- Parsing (synchronous) ------------------------------------------------

    def parse(self, path: Path) -> tuple[Session, list[Message]]:
        """Parse *path* by delegating to ``parse_session``.

        Sets ``session.provider = "claude"`` explicitly even though
        ``Session`` already defaults to ``"claude"``, so the field is
        always visibly owned by this adapter.
        """
        from ..parser import parse_session

        session, messages = parse_session(path)
        session.provider = "claude"
        return session, messages

    # -- Path ownership -------------------------------------------------------

    def owns_path(self, path: Path) -> bool:
        """Return True for ``.jsonl`` files nested under a ``projects/`` directory.

        Claude sessions live at ``<root>/projects/<mangled-dir>/<uuid>.jsonl``.
        Codex sessions live under ``.codex/sessions/…`` (no ``projects/`` dir),
        so these two sets are disjoint.
        """
        return path.suffix == ".jsonl" and "projects" in path.parts
