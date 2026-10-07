"""Provider driver abstraction — the transport seam for *driving* a live CLI.

A ``ProviderAdapter`` (``providers/``) reads a provider's on-disk format; a
``ProviderDriver`` (this package) *drives* a live process for that provider —
spawning it, streaming its TUI bytes, sending input, and reporting state.

The two are deliberately separate: decode/recall is cross-platform and always
available, while driving depends on a runtime backend (tmux today, ConPTY
later). v1 ships exactly one backend — ``TmuxDriver`` — and routes only Codex
through it. Claude keeps its tuned direct-PTY path untouched.

Design constraints (see ``docs/phase4-implementation-plan.md``):
  * Transport-agnostic above the backend: no tmux assumptions leak into this
    ABC, so a future ``ConptyDriver`` can implement the same interface.
  * ``availability()`` is a *runtime* probe (is the backend usable on this
    box?). Effective drivability = static ``ProviderCaps`` AND availability.
  * Output is a raw byte stream so the existing xterm.js Native transport can
    consume a driver exactly like it consumes the Claude PTY.
"""

from __future__ import annotations

import abc
from collections.abc import Callable
from enum import Enum

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Runtime availability descriptor
# ---------------------------------------------------------------------------


class DriverAvailability(BaseModel):
    """Whether a driver backend is usable on this machine right now.

    ``available`` gates every driving affordance; ``reason`` is a short,
    user-facing explanation when it is False (e.g. ``"tmux not found on
    PATH"``) so the FE can show *why* live driving is unavailable instead of
    silently hiding it.
    """

    available: bool
    reason: str | None = None


# ---------------------------------------------------------------------------
# TUI lifecycle state
# ---------------------------------------------------------------------------


class DriverState(str, Enum):
    """Coarse lifecycle state of the driven TUI, derived from screen scrape.

    Backend-neutral and intentionally small. The byte stream — not this
    enum — is what the human interacts with; ``capture_state`` exists so the
    *server* can gate submits, report status, and surface blocking prompts.
    """

    STARTING = "starting"  # spawned, nothing recognisable on screen yet
    IDLE = "idle"  # composer ready, accepting input
    RUNNING = "running"  # a turn is in flight ("esc to interrupt")
    NEEDS_TRUST = "needs_trust"  # "trust this directory?" dialog
    NEEDS_APPROVAL = "needs_approval"  # command-approval dialog
    NEEDS_LOGIN = "needs_login"  # not authenticated
    NEEDS_UPDATE = "needs_update"  # blocking "update available" menu
    DEAD = "dead"  # backend session no longer exists


# ---------------------------------------------------------------------------
# Abstract driver
# ---------------------------------------------------------------------------


class ProviderDriver(abc.ABC):
    """Drive one live provider session over some backend transport.

    One instance maps to one driven clau-decode session. Concrete backends
    (``TmuxDriver`` today) own the process lifecycle; this ABC fixes the shape
    the server and the Native transport depend on.
    """

    @classmethod
    @abc.abstractmethod
    def availability(cls) -> DriverAvailability:
        """Probe whether this backend is usable on this machine *now*.

        Pure and cheap — safe to call on every capability check. Must never
        raise; report unavailability via ``DriverAvailability(available=False,
        reason=...)`` instead.
        """
        ...

    # -- Lifecycle ------------------------------------------------------------

    @abc.abstractmethod
    async def spawn(self, *, cols: int, rows: int) -> None:
        """Create the backing session and begin streaming its output.

        Idempotent guard expected: spawning an already-spawned driver should
        raise rather than silently double-spawn.
        """
        ...

    @abc.abstractmethod
    async def kill(self) -> None:
        """Tear down the backing session and release all resources.

        MUST be safe to call more than once (double-kill is a no-op) and safe
        to call on a driver that never spawned.
        """
        ...

    @abc.abstractmethod
    def is_alive(self) -> bool:
        """Return True if the driven session is still running.

        Cheap/sync — for authoritative backend checks use the backend's own
        async probe.
        """
        ...

    # -- Input ----------------------------------------------------------------

    @abc.abstractmethod
    async def send_text(self, text: str) -> None:
        """Submit a complete prompt as one composer message + Enter.

        The structured path behind clau-decode's React composer. Handles
        multi-line bodies without premature submission.
        """
        ...

    @abc.abstractmethod
    async def write_input(self, data: bytes) -> None:
        """Forward raw interactive keystrokes (xterm.js Native view).

        Used for arbitrary bytes — arrow keys, Esc to dismiss an approval,
        Ctrl-C — that the structured ``send_text`` path can't express.
        """
        ...

    # -- Output ---------------------------------------------------------------

    @abc.abstractmethod
    def output_snapshot(self) -> bytes:
        """Return the current bounded output ring (for a reconnecting client)."""
        ...

    @abc.abstractmethod
    def set_on_chunk(self, cb: Callable[[bytes], None] | None) -> None:
        """Register a sync callback fired with each drained output chunk.

        The Native transport uses this to fan bytes out over SSE. The callback
        runs inside the reader loop and MUST NOT block or raise.
        """
        ...

    # -- Sizing & state -------------------------------------------------------

    @abc.abstractmethod
    async def resize(self, *, cols: int, rows: int) -> None:
        """Resize the driven TUI to ``cols`` x ``rows``."""
        ...

    @abc.abstractmethod
    async def capture_state(self) -> DriverState:
        """Scrape the current screen and classify it into a ``DriverState``."""
        ...
