"""Abstract base classes for provider adapters.

A ``ProviderAdapter`` bridges an external AI tool's on-disk format into
clau-decode's domain model.  Every adapter must declare its ``capabilities``
and implement the four operations the server uses:

  configured_roots — which paths to scan, per-provider config knowledge
  discover         — async-generate (Project, session_file_path) pairs
  parse            — turn a session file into (Session, [Message])
  owns_path        — quick membership test for dispatcher routing
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from pathlib import Path

from pydantic import BaseModel

from ..models import AppConfig, Message, Project, Session


# ---------------------------------------------------------------------------
# Capabilities descriptor
# ---------------------------------------------------------------------------


class ProviderCaps(BaseModel):
    """Declares which interactive operations this provider supports.

    The frontend (and server) check these before enabling UI affordances so
    that read-only providers (e.g. an imported Codex export) never see a
    send / resume / fork button.
    """

    can_send: bool
    can_resume: bool
    can_fork: bool
    can_edit: bool


# ---------------------------------------------------------------------------
# Abstract adapter
# ---------------------------------------------------------------------------


class ProviderAdapter(abc.ABC):
    """Seam every concrete provider must implement.

    Subclasses set the class attribute ``name`` to a short, unique identifier
    (e.g. ``"claude"``, ``"codex"``).  The registry keys on this string.
    """

    name: str  # e.g. "claude" — concrete subclasses assign this at class level

    # -- Capability declaration -----------------------------------------------

    @property
    @abc.abstractmethod
    def capabilities(self) -> ProviderCaps:
        """Return this adapter's static capability descriptor."""
        ...

    # -- Config-aware root resolution -----------------------------------------

    @abc.abstractmethod
    def configured_roots(self, config: AppConfig) -> list[Path]:
        """Return the expanded root directories this provider should scan.

        Centralises the knowledge of which ``AppConfig`` fields belong to this
        provider so callers never need to know the field names.

        Args:
            config: The live application configuration.

        Returns:
            Fully-expanded ``Path`` objects (no ``~`` / env vars).
        """
        ...

    # -- Discovery (async generator) ------------------------------------------

    @abc.abstractmethod
    async def discover(
        self, roots: list[Path], include_subagents: bool = False
    ) -> AsyncIterator[tuple[Project, Path]]:
        """Yield ``(Project, session_file_path)`` pairs found under *roots*.

        Mirrors the contract of ``scanner.scan_paths``.  Declared as an async
        generator so I/O can be interleaved with DB writes in the scan loop.

        Args:
            roots: Expanded root directories to walk.
            include_subagents: Opt-in: also yield sub-agent transcript files.
                Adapters with no sub-agent concept (e.g. Codex) ignore this.

        Yields:
            Two-tuples of ``(Project, Path)`` where ``Path`` points to an
            individual session file.
        """
        # Abstract async generators must have a body.  Subclasses override
        # the entire method; this line is never reached.
        raise NotImplementedError  # pragma: no cover
        yield  # makes Python treat this as a generator function

    # -- Parsing (synchronous) ------------------------------------------------

    @abc.abstractmethod
    def parse(self, path: Path) -> tuple[Session, list[Message]]:
        """Parse *path* into a ``(Session, messages)`` pair.

        Intentionally synchronous — callers wrap this in
        ``asyncio.to_thread`` when they need non-blocking behaviour.

        Args:
            path: Absolute path to a session file.

        Returns:
            ``(Session, list[Message])`` populated from the file contents.
        """
        ...

    # -- Path ownership -------------------------------------------------------

    @abc.abstractmethod
    def owns_path(self, path: Path) -> bool:
        """Return True if this adapter recognises and can parse *path*.

        Used by the registry dispatcher to route incoming file paths to the
        correct adapter without opening the file.

        Args:
            path: Absolute path to a candidate session file.
        """
        ...
