"""PTY-attached runner — drives the CLI binary in interactive TUI mode.

One PTY + one ``claude`` subprocess per focused chat. ``PtyChannel`` owns
the low-level fd/termios plumbing (master/slave allocation, ``TIOCSCTTY``
controlling-terminal handoff, ``TIOCSWINSZ`` window sizing, non-blocking
master-fd drain via ``loop.add_reader``). ``PtyManager`` is the module
singleton attached to ``app.state.pty_manager`` — it orchestrates
lazy-spawn-on-focus, dead-PTY auto-respawn on submit, the 5-minute idle
timer with a 4-minute SSE warn, and clean shutdown.

There is no general per-session busy lock: ``submit()`` succeeds while a
normal turn is in flight so ``/btw`` can flow straight through to PTY stdin.
The one exception is an active ``/btw`` modal/capture; until that resolves,
new submits are rejected so foreground slash commands cannot be swallowed by
the modal. The hidden TUI interprets the bytes; agent-decoder's existing JSONL
watcher → SQLite → SSE pipeline remains the canonical content channel for
non-ephemeral turns.

Phase 2 ships ``/btw`` ephemeral capture: both the user's input and
claude's PTY-rendered response are persisted to the ``ephemeral_messages``
table. See ``docs/pty-runner-plan.md`` Phase 2 and
``docs/pty-runner-phase2-spike.md`` for the full rationale and wire
protocol. HITL surfaces (auth modal, trust modal, stuck-session inline
notice) are deferred to later phases.
"""

from __future__ import annotations

import asyncio
import errno
import fcntl
import logging
import os
import pty
import signal
import socket
import struct
import subprocess
import sys
import termios
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from ._auth_env import _subscription_env, spawn_env as _spawn_env  # noqa: F401 — re-export
from .btw_capture import (
    BTW_DISMISS_SEQUENCE,
    extract_btw_response,
    find_response_complete,
    is_btw_input,
)
from .locks import LockAlreadyHeld, LockSidecar, _lock_path_for
from .pty_native import encode_pty_output_chunk, encode_pty_snapshot
from .pty_screen_state import classify_screen

if TYPE_CHECKING:
    from .db import Database
    from .events_bus import EventBroadcaster


_log = logging.getLogger(__name__)

DEFAULT_IDLE_TIMEOUT_S = 300.0
DEFAULT_IDLE_WARN_S = 240.0
# When the FE signals nav-away via ``/api/pty/blur``, the idle-kill window.
# Previously 5s, which meant switching between sessions killed the one you
# navigated away from almost immediately — so you could never keep more than
# one native session alive at a time, and re-attaching to a switched-away
# session lost its captured scrollback ring (you'd resume into a fresh,
# shorter render). Keep blurred sessions alive as long as focused ones so
# multiple PTYs coexist and their scrollback survives a session switch.
BLURRED_IDLE_TIMEOUT_S = DEFAULT_IDLE_TIMEOUT_S
DEFAULT_ROWS = 40
# Fallback spawn width. The authoritative width is AppConfig.native_pty_cols,
# pushed onto PtyManager via set_native_cols(); this constant only applies if
# that was never set. Keep it equal to the AppConfig default.
DEFAULT_COLS = 100

# Bounded ring buffer for PTY output. Two consumers:
#   1. HITL pattern matching (auth, trust, stuck-session) — only needs recent bytes.
#   2. Native View hydration on (re)attach — replays this ring into xterm so the
#      browser terminal shows the session's scrollback. This is the binding
#      constraint: the frontend keeps 5000 scrollback rows, so the ring must hold
#      a comparable amount of escape-laden output, or a re-attach can only scroll
#      back through the last ~64KB (≈20% of a long session). Sized to ~5000 rows.
OUTPUT_RING_BYTES = 4 * 1024 * 1024

# Bytes to read per drain callback invocation. ``loop.add_reader`` is
# level-triggered, so whatever we don't drain now fires the callback again
# immediately — the only effect of a bigger chunk is FEWER callbacks (and thus
# fewer SSE messages) for the same burst. Each drained chunk becomes one
# base64+JSON ``pty_output_chunk`` event (see PtyManager._scan_chunk_for_hitl),
# so a small chunk fragments a single full-screen repaint into many SSE frames,
# and that per-frame encode/transport overhead is what makes a mouse-flood
# scroll choppy over HTTP. 64 KiB coalesces a typical claude repaint into one or
# two frames while still being small enough that no single read pins the loop.
_DRAIN_CHUNK = 64 * 1024

# Bytes off the TAIL of the ring fed to classify_screen on each chunk. The live
# screen state we classify is always in the last screenful of output; this caps
# the per-chunk classification cost at a constant instead of letting it grow
# with the (up to 4 MB) ring. 64 KiB covers many full repaints of a tall TUI.
_CLASSIFY_TAIL_BYTES = 64 * 1024

# Output coalescing window. claude emits a single TUI repaint as a burst of tiny
# (~1 KB) PTY writes; without batching, each becomes its own SSE event + classify
# + frontend xterm.write/repaint. Accumulating reads and flushing once per this
# window collapses a scroll flick from hundreds of events into a few — roughly
# one frontend write per frame. ~12 ms ≈ under a 60 fps frame, so the added
# delivery latency is imperceptible while the batching is large.
_OUTPUT_COALESCE_S = 0.012

# Auth-required pattern: claude prints this banner verbatim when the user
# is not logged in. We scan each drained chunk for the substring and emit
# an SSE ``auth_required`` event the first time we see it (per channel).
# Carryover length is the longest pattern length minus one so the substring
# is detected even when it straddles two consecutive read() chunks.
_AUTH_REQUIRED_PATTERN = b"Not logged in"
_AUTH_REQUIRED_CARRYOVER = len(_AUTH_REQUIRED_PATTERN) - 1

# Submission control bytes. Per Phase 0 empirical verification:
#   - ``\r`` (CR, 0x0D) terminates a TUI message — sent at end of submit
#   - ``\x0a`` (LF / Ctrl+J) inserts a newline INSIDE a message — used for
#     content-string ``\n`` translation
_SUBMIT_BYTE = b"\r"
_INLINE_NEWLINE = b"\n"  # 0x0A — submitted as Ctrl+J, identical wire byte

# Graceful-kill timing. SIGINT first (claude's TUI handles SIGINT cleanly),
# then two Ctrl-Cs in quick succession, then SIGKILL.
_KILL_SIGINT_WAIT_S = 3.0
_KILL_CTRLC_WAIT_S = 3.0

# Phase 2 — /btw ephemeral capture.
# If the response-complete marker never arrives (e.g. claude crashes mid-modal
# or network stall), give up after this many seconds, persist whatever's in the
# buffer (may be None if extraction yields nothing), and reset the capture state.
# 180 s mirrors the zai-latency cap used in the spike and the recap path.
_BTW_STUCK_TIMEOUT_S = 180.0

# After the response-complete marker fires, send ESC and wait this long for the
# TUI to finish its redraw before the channel returns to normal operation.
_BTW_DISMISS_SETTLE_S = 2.0


class PtyOwnershipConflict(RuntimeError):
    """Raised by ``PtyManager.focus`` when another claude is already
    attached to the session and we'd race it by spawning our own.

    ``foreign_pids`` is the deduped list of OS pids belonging to other
    claudes on the same session id, minus our server pid and our own
    spawned claudes. The HTTP layer turns this into a 409 with the pid
    list in the body so the FE can offer a Take-over banner.
    """

    def __init__(self, foreign_pids: list[int]) -> None:
        super().__init__(
            f"session already attached by foreign claude pid(s): {foreign_pids}"
        )
        self.foreign_pids = list(foreign_pids)


class PtySubmitInFlight(RuntimeError):
    """Raised when the PTY cannot safely accept another submit yet."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


# Pids of claudes spawned by this process. PtyChannel.start adds to
# this set; PtyChannel.kill drains it. ``_session_conflict_pids`` uses
# the set to filter out our own children so the focus-time pre-spawn
# check doesn't flag them as foreign owners.
_OWN_CLAUDE_PIDS: set[int] = set()


def _lsof_owners(jsonl_path: Optional[Path]) -> list[int]:
    """PIDs holding ``jsonl_path`` open per ``lsof -t``.

    Backstop signal — catches the brief append window when a remote
    claude is mid-write. **Blind to idle terminal claudes**, which
    open → write → close per turn (verified 2026-05-26 against
    ``zai``; see ``docs/pty-ownership-phase0-findings.md``). The
    pgrep signal in ``_pgrep_session_id`` is the primary detector.

    macOS + Linux only; fails open (``[]``) on any error.
    """
    if jsonl_path is None or not jsonl_path.exists():
        return []
    try:
        out = subprocess.run(
            ["lsof", "-t", str(jsonl_path)],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        _log.debug("pty: _lsof_owners(%s) unavailable: %s", jsonl_path, exc)
        return []
    return [int(x) for x in out.stdout.split() if x.strip().isdigit()]


def _pgrep_session_id(session_id: str) -> list[int]:
    """PIDs whose cmdline carries ``session_id`` as a CLI argument.

    The primary Phase-0 detector for idle terminal claudes. A live
    ``claude --resume <sid>`` (or ``-r <sid>``, ``--resume=<sid>``,
    ``--session-id <sid>``) carries the UUID in argv even when the
    JSONL fd is closed between turns — empirically the only signal
    that catches an idle terminal claude (see findings doc).

    We require the sid to follow a known flag (``--resume``,
    ``--session-id``, or ``-r``) separated by ``=`` or a single space.
    This filters out incidental matches in shell history, scripts whose
    text contains the sid in a non-argv context, etc. UUIDs are unique
    enough that even an unqualified substring match has near-zero false
    positives in practice; the flag prefix tightens it further at no
    cost.

    Known gaps:
      - wrappers / IDE plugins that exec claude with sid embedded in a
        way that doesn't match the flag pattern. Phase 2's lock sidecar
        covers those.
      - the moment between a wrapper script's start and its ``exec`` to
        claude: the wrapper's argv may not include the flag pattern.
        Tolerated — this window is sub-second.
    """
    pattern = rf"(--resume|--session-id|-r)[= ]{session_id}"
    try:
        out = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        _log.debug("pty: _pgrep_session_id(%s) unavailable: %s", session_id, exc)
        return []
    # pgrep exits 1 with no stdout when there are no matches — not an error.
    if out.returncode not in (0, 1):
        return []
    return [int(x) for x in out.stdout.split() if x.strip().isdigit()]


def _fresh_foreign_sidecar(jsonl_path: Optional[Path]) -> Optional[LockSidecar]:
    """Return the lock sidecar IF it exists, is fresh, and is not us.

    Phase-1 authoritative signal. Used by both
    ``_session_conflict_pids`` (to add the lock pid even when pgrep
    misses the cmdline pattern) and by ``PtyManager.ownership`` (to
    surface structured owner metadata to the FE).
    """
    if jsonl_path is None:
        return None
    sc = LockSidecar.read(jsonl_path)
    if sc is None:
        return None
    if sc.is_self() or sc.is_stale():
        return None
    return sc


def _unlink_fresh_foreign_sidecar(jsonl_path: Optional[Path]) -> bool:
    """Remove a fresh foreign sidecar after explicit user takeover.

    Normal acquisition still treats a fresh cross-host sidecar as
    authoritative. This helper is only for ``/api/pty/takeover`` after the
    user has chosen to clear stale ownership metadata.
    """
    sc = _fresh_foreign_sidecar(jsonl_path)
    if sc is None:
        return False
    lp = _lock_path_for(sc.jsonl_path)
    try:
        os.unlink(str(lp))
    except FileNotFoundError:
        return False
    except OSError as exc:
        _log.warning("pty: takeover could not unlink sidecar %s: %s", lp, exc)
        return False
    _log.info(
        "pty: takeover unlinked sidecar for %s held by %s@%s pid %d",
        sc.jsonl_path,
        sc.owner_kind,
        sc.hostname,
        sc.pid,
    )
    return True


def _session_conflict_pids(
    session_id: str,
    jsonl_path: Optional[Path],
) -> list[int]:
    """Union of pgrep + lsof + lock-sidecar signals; foreign claudes
    attached to ``session_id``.

    Returns deduped, sorted PIDs that look like a competing claude
    process for this session, minus:
      - our server pid (``os.getpid()``).
      - any claude we've spawned ourselves (``_OWN_CLAUDE_PIDS``).

    Three signals are unioned, each with known blind spots covered by
    the others:
      - **pgrep** (primary): catches terminal claudes whose cmdline
        contains ``<session_id>``. Misses wrappers that mangle argv.
      - **lsof** (backstop): catches the brief append moment when a
        wrapper-hidden claude actually writes the JSONL. Blind to idle.
      - **lock sidecar** (authoritative for cooperating writers): the
        Phase-1 ``.lock`` file written by agent-decoder and — once
        Phase 2 ships — by ``claude-wrapper``-spawned terminals.
        Cross-host sidecars contribute no pid (we can't signal a
        remote pid); their existence is surfaced via
        ``PtyManager.ownership.foreign_owner`` instead.
    """
    own_pid = os.getpid()
    pids: set[int] = set(_pgrep_session_id(session_id))
    pids.update(_lsof_owners(jsonl_path))
    sc = _fresh_foreign_sidecar(jsonl_path)
    if sc is not None and sc.hostname == socket.gethostname():
        pids.add(sc.pid)
    pids.discard(own_pid)
    pids.difference_update(_OWN_CLAUDE_PIDS)
    return sorted(pids)


async def _pty_env(rows: int, cols: int, bin_name: str) -> dict[str, str]:
    """Build the spawn env. Auth-aware stripping lives in ``_auth_env``;
    here we just layer the PTY-specific overrides on top.
    """
    env = await _spawn_env(bin_name)
    env.pop("NO_COLOR", None)
    env["TERM"] = "xterm-256color"
    env["COLORTERM"] = "truecolor"
    env["FORCE_COLOR"] = "1"
    env["CLICOLOR_FORCE"] = "1"
    env["COLUMNS"] = str(cols)
    env["LINES"] = str(rows)
    return env


def _now_ms() -> int:
    return int(time.time() * 1000)


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    """Apply window size to ``fd`` via ``TIOCSWINSZ``.

    Must be called on the SLAVE fd before spawning, so the child inherits
    a correctly-sized terminal — claude's TUI inspects ``ws_row``/``ws_col``
    at startup and lays out its frame against those numbers.
    """
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


# The controlling-TTY claim (setsid + TIOCSCTTY) happens in the spawned
# wrapper process at ``agent_decoder/_pty_preexec.py`` — we cannot use
# ``preexec_fn`` because uvloop's subprocess implementation refuses to run
# it. See that module's docstring for the full rationale.


# ---------------------------------------------------------------------------
# PtyChannel
# ---------------------------------------------------------------------------


@dataclass
class _ChannelState:
    """Per-channel mutable state. Separate from PtyChannel to keep the
    public-API surface free of low-level field clutter."""

    proc: Optional[asyncio.subprocess.Process] = None
    master_fd: int = -1
    slave_fd: int = -1
    reader_registered: bool = False
    ring: bytearray = field(default_factory=bytearray)
    ring_complete: bool = True
    last_input_ms: int = 0
    last_pty_output_ms: int = 0
    last_pty_output_seq: int = 0
    dead: bool = False
    # Per-channel guard for the ``auth_required`` SSE: claude prints
    # ``Not logged in`` once at startup and again on every redraw of the
    # error frame. We emit exactly once per channel lifetime; a respawn
    # gets a fresh channel state and may re-emit.
    auth_required_emitted: bool = False

    # Phase 2 — /btw ephemeral capture state.
    # ``expecting_btw_response`` is set by PtyManager.submit() when a /btw
    # input is detected.  While True every drained chunk is appended to
    # ``btw_buffer`` (separate from the bounded ring — must be unbounded to
    # hold a full /btw response).  When ``BTW_RESPONSE_COMPLETE_MARKER``
    # appears in the buffer, PtyChannel.finalize_btw_capture() is scheduled
    # as a fire-and-forget task.  The state is reset after finalization.
    expecting_btw_response: bool = False
    btw_buffer: bytearray = field(default_factory=bytearray)
    btw_input_row_id: Optional[int] = None


class PtyChannel:
    """One PTY + one claude subprocess. Created on demand by PtyManager."""

    def __init__(
        self,
        session_id: str,
        argv: list[str],
        cwd: str,
        env: dict[str, str],
        rows: int = DEFAULT_ROWS,
        cols: int = DEFAULT_COLS,
        on_chunk: Optional[Callable[["PtyChannel", bytes], None]] = None,
        on_dead: Optional[Callable[["PtyChannel"], None]] = None,
        jsonl_path: Optional[Path] = None,
        ui_endpoint: Optional[str] = None,
        db: Optional["Database"] = None,
        bus: Optional["EventBroadcaster"] = None,
    ) -> None:
        self.session_id = session_id
        self._argv = list(argv)
        self._cwd = cwd
        self._env = dict(env)
        self._rows = rows
        self._cols = cols
        self._state = _ChannelState()
        # Sync callback invoked after each drained chunk is appended to
        # the ring buffer. PtyManager uses this to pattern-match the TUI's
        # output for HITL signals ("Not logged in" → auth_required SSE).
        # Sync (not async) so it runs inside the drain callback without
        # scheduling a task per chunk; implementations MUST not block.
        self._on_chunk = on_chunk
        self._on_dead = on_dead
        # ``last_activity_ms`` is the max of input + output timestamps and
        # is computed on read; the underlying fields live in ``_state``.

        # Phase 1 — lock sidecar (pty-ownership-plan.md). Acquired in
        # start() and released in kill(). ``jsonl_path=None`` skips the
        # sidecar entirely (e.g. brand-new sessions whose JSONL doesn't
        # exist yet — though the lock file's create is fine even then,
        # we just don't track ui_endpoint for those callers).
        self._jsonl_path = jsonl_path
        self._ui_endpoint = ui_endpoint
        self._lock_sidecar: Optional["LockSidecar"] = None

        # Phase 2 — /btw ephemeral capture. ``db`` is optional so channels
        # created without it (e.g. bare PtyChannel unit tests, recap runner)
        # still work — the btw marker check is a no-op when db is None.
        self._db = db
        # ``bus`` is also optional; when set, finalize/timeout publish an
        # ``ephemeral_pair_persisted`` SSE event so the FE can refresh the
        # ChatView inline. Recap channel + bare unit tests pass None.
        self._bus = bus

        # Stuck-modal timeout in seconds. Stored as an instance attribute so
        # tests can override it per-channel (e.g. ``channel._btw_stuck_timeout_s
        # = 1.0``) without patching the module-level constant.
        self._btw_stuck_timeout_s: float = _BTW_STUCK_TIMEOUT_S

        # Fire-and-forget task slots: one for the finalize coroutine (marker
        # triggered) and one for the stuck-modal timeout (180 s backstop).
        # Only one of each is live at a time per channel.
        self._btw_finalize_task: Optional[asyncio.Task] = None
        self._btw_timeout_task: Optional[asyncio.Task] = None

    def _publish_submit_completed(
        self,
        *,
        kind: str,
        status: str,
        input_row_id: Optional[int],
        response_row_id: Optional[int],
    ) -> None:
        """Publish a terminal submit lifecycle event for UI optimistic state."""
        if self._bus is None:
            return
        try:
            self._bus.publish(
                {
                    "type": "pty_submit_completed",
                    "session_id": self.session_id,
                    "kind": kind,
                    "status": status,
                    "input_id": input_row_id,
                    "response_id": response_row_id,
                }
            )
        except Exception as exc:
            _log.warning(
                "pty: submit completion publish raised (session %s): %s",
                self.session_id,
                exc,
            )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Allocate a PTY pair, spawn ``claude``, register the output drain."""
        if self._state.proc is not None:
            raise RuntimeError(f"channel for {self.session_id} already started")

        # Phase 1 — acquire the lock sidecar BEFORE spawning. Raising
        # here means we never start a subprocess that would race a
        # foreign owner. PtyManager.focus surfaces ``LockAlreadyHeld``
        # as a ``PtyOwnershipConflict`` 409 with the foreign pid.
        if self._jsonl_path is not None:
            try:
                self._lock_sidecar = LockSidecar.acquire(
                    self._jsonl_path,
                    owner_kind="agent-decoder",
                    ui_endpoint=self._ui_endpoint,
                )
                self._lock_sidecar.start_heartbeat()
            except LockAlreadyHeld:
                raise

        master_fd, slave_fd = pty.openpty()
        try:
            _set_winsize(slave_fd, self._rows, self._cols)
            os.set_blocking(master_fd, False)

            _log.info(
                "pty: spawning %s (session %s, cwd=%s, rows=%d, cols=%d)",
                " ".join(self._argv),
                self.session_id,
                self._cwd,
                self._rows,
                self._cols,
            )

            # We spawn through a tiny wrapper module that performs
            # ``os.setsid()`` + ``TIOCSCTTY`` in the child before exec'ing the
            # target binary. This works under both the default asyncio loop
            # and uvloop (uvloop deliberately does not run ``preexec_fn``).
            wrapper_argv = [
                sys.executable,
                "-m",
                "agent_decoder._pty_preexec",
                *self._argv,
            ]
            proc = await asyncio.create_subprocess_exec(
                *wrapper_argv,
                cwd=self._cwd,
                env=self._env,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
            )
        except BaseException:
            # Spawn failed — release both fds so we don't leak.
            try:
                os.close(master_fd)
            except OSError:
                pass
            try:
                os.close(slave_fd)
            except OSError:
                pass
            # Also drop the lock sidecar so a follow-up retry isn't
            # blocked by our orphaned lock.
            if self._lock_sidecar is not None:
                try:
                    self._lock_sidecar.release()
                except Exception as exc:  # pragma: no cover — defensive
                    _log.debug(
                        "pty: lock release after spawn failure raised (session %s): %s",
                        self.session_id,
                        exc,
                    )
                self._lock_sidecar = None
            raise

        # Parent doesn't need the slave fd once the child has inherited it.
        # Keeping it open would prevent EOF on master when the child exits.
        try:
            os.close(slave_fd)
        except OSError as exc:
            _log.warning(
                "pty: failed to close slave fd (session %s): %s",
                self.session_id,
                exc,
            )

        self._state.proc = proc
        self._state.master_fd = master_fd
        self._state.slave_fd = -1  # closed; resize() reapplies via master fd

        # Register the spawned pid so the Phase-0 ownership check doesn't
        # treat our own claudes as foreign owners. Track the wrapper-proc
        # pid: ``_pty_preexec`` does ``execvp`` to claude, which retains
        # the pid, so this single registration covers both program states.
        _OWN_CLAUDE_PIDS.add(proc.pid)

        loop = asyncio.get_running_loop()
        loop.add_reader(master_fd, self._on_readable)
        self._state.reader_registered = True

    async def await_ready(self, timeout_s: float = 3.0, settle_s: float = 0.15) -> bool:
        """Block until the TUI signals it's ready to accept input.

        Phase 0 verified that ``claude`` v2.1.143 emits the bracketed-paste
        enable escape ``\\x1b[?2004h`` once its TUI is mounted and ready to
        consume stdin. We poll the bounded output ring for that sequence
        with a backstop timeout — without this, model-switch respawns lose
        the first write because ``channel.start()`` returns when the
        subprocess is launched but well before the TUI's input handler is
        attached.

        The marker is emitted just before the TUI finishes installing raw
        input handling. Settle briefly after seeing it so a cold first
        submit's trailing CR is interpreted by the TUI, not by the terminal
        line discipline as a newline. Returns True if the marker was seen,
        False on timeout (caller can still write; the write may just race
        the bootstrap).
        """
        marker = b"\x1b[?2004h"
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout_s
        while loop.time() < end:
            if self._state.dead:
                return False
            if marker in self._state.ring:
                if settle_s > 0:
                    await asyncio.sleep(settle_s)
                return not self._state.dead
            await asyncio.sleep(0.05)
        return False

    # ------------------------------------------------------------------
    # I/O
    # ------------------------------------------------------------------

    def write(self, data: bytes) -> None:
        """Non-blocking write to master_fd. No busy lock."""
        if self._state.dead or self._state.master_fd < 0:
            raise RuntimeError(f"channel for {self.session_id} is not alive")
        if not data:
            return
        try:
            # PTY master is non-blocking; a partial write here would be
            # unusual for the message-sized payloads we send, but handle
            # it defensively by retrying the tail synchronously since the
            # whole operation is best-effort (no per-session busy lock).
            view = memoryview(data)
            while view:
                n = os.write(self._state.master_fd, view)
                if n <= 0:
                    break
                view = view[n:]
        except (BlockingIOError, InterruptedError):
            # Extremely unlikely for a message-sized write; surface so
            # the caller can decide whether to retry.
            raise
        except OSError as exc:
            _log.warning(
                "pty: write failed (session %s): %s",
                self.session_id,
                exc,
            )
            self._state.dead = True
            raise
        self._state.last_input_ms = _now_ms()

    def _on_readable(self) -> None:
        """``loop.add_reader`` callback — drain available bytes into the ring.

        Must never raise: an exception here would propagate to the event
        loop's default handler and could destabilize the whole server.
        """
        fd = self._state.master_fd
        if fd < 0:
            return
        try:
            chunk = os.read(fd, _DRAIN_CHUNK)
        except (BlockingIOError, InterruptedError):
            return
        except OSError as exc:
            # EIO is normal when the slave side closes (child exited);
            # treat any read error as terminal for this channel.
            if exc.errno not in (errno.EIO, errno.EBADF):
                _log.warning(
                    "pty: read error (session %s): %s",
                    self.session_id,
                    exc,
                )
            self._mark_dead_from_drain()
            return
        except Exception as exc:  # pragma: no cover — belt and suspenders
            _log.warning(
                "pty: unexpected drain error (session %s): %s",
                self.session_id,
                exc,
            )
            self._mark_dead_from_drain()
            return

        if not chunk:
            # EOF on master — child has fully closed the slave side.
            self._mark_dead_from_drain()
            return

        # Append to bounded ring. bytearray + slice trim is allocation-light
        # for the small chunks we expect and gives us a single contiguous
        # buffer that later phases can pattern-match against directly.
        ring = self._state.ring
        ring.extend(chunk)
        overflow = len(ring) - OUTPUT_RING_BYTES
        if overflow > 0:
            del ring[:overflow]
            self._state.ring_complete = False
        self._state.last_pty_output_ms = _now_ms()
        self._state.last_pty_output_seq += 1

        # Fire the per-chunk hook AFTER the ring write so the callback
        # sees a consistent view. The hook is best-effort: any exception
        # is logged and swallowed because this runs inside the asyncio
        # reader callback (an unhandled exception would propagate to the
        # default loop exception handler and could destabilize the server).
        if self._on_chunk is not None:
            try:
                self._on_chunk(self, chunk)
            except Exception as exc:  # pragma: no cover — defensive
                _log.warning(
                    "pty: on_chunk hook raised (session %s): %s",
                    self.session_id,
                    exc,
                )

        # Phase 2 — /btw ephemeral output capture. While expecting a btw
        # response, accumulate every chunk into the growable btw_buffer
        # (separate from the bounded ring so it can hold a full response).
        # Check for the response-complete marker after each append; if
        # found, schedule finalize_btw_capture() as a fire-and-forget task.
        # This runs synchronously inside the add_reader callback — the marker
        # check (bytes.find, C-level) is cheap on a sub-100 KB buffer.
        if self._state.expecting_btw_response and self._db is not None:
            self._state.btw_buffer.extend(chunk)
            if find_response_complete(self._state.btw_buffer) >= 0:
                # Schedule finalize — idempotent: skip if already pending.
                if self._btw_finalize_task is None or self._btw_finalize_task.done():
                    try:
                        loop = asyncio.get_running_loop()
                        self._btw_finalize_task = loop.create_task(
                            self._finalize_btw_capture()
                        )
                        _log.info(
                            "pty: btw response-complete marker detected — "
                            "finalize scheduled (session %s, buffer=%d bytes)",
                            self.session_id,
                            len(self._state.btw_buffer),
                        )
                    except RuntimeError:  # pragma: no cover — loop closed
                        pass

    def _mark_dead_from_drain(self) -> None:
        """Remove the loop reader and mark the channel dead.

        Called from inside the reader callback when EOF or a fatal read
        error indicates the subprocess has gone away. ``kill()`` handles
        the formal teardown — this is just the drain-side bookkeeping.
        """
        if self._state.reader_registered and self._state.master_fd >= 0:
            try:
                loop = asyncio.get_running_loop()
                loop.remove_reader(self._state.master_fd)
            except (RuntimeError, ValueError):
                pass
            self._state.reader_registered = False
        if self._state.dead:
            return
        self._state.dead = True
        if self._on_dead is not None:
            try:
                self._on_dead(self)
            except Exception as exc:  # pragma: no cover — defensive
                _log.warning(
                    "pty: on_dead hook raised (session %s): %s",
                    self.session_id,
                    exc,
                )

    # ------------------------------------------------------------------
    # Phase 2 — /btw ephemeral capture
    # ------------------------------------------------------------------

    async def _finalize_btw_capture(self) -> None:
        """Run after BTW_RESPONSE_COMPLETE_MARKER hits the btw_buffer.

        Sequence:
          1. Send BTW_DISMISS_SEQUENCE (ESC) to the PTY — also resets the
             idle timer since it's an input event.
          2. await asyncio.sleep(_BTW_DISMISS_SETTLE_S)  # TUI redraw window
          3. extract_btw_response(bytes(btw_buffer))
          4. If extracted text is non-None and btw_input_row_id is not None:
             await db.record_ephemeral_response(btw_input_row_id, extracted)
          5. Reset state: expecting_btw_response=False, btw_buffer.clear(),
             btw_input_row_id=None.  Cancel the stuck-modal timeout task.
          6. Return (input_row_id, response_row_id) — both may be None on
             partial failure.

        Errors are logged and swallowed (fire-and-forget task).
        """
        input_row_id = self._state.btw_input_row_id
        response_row_id: Optional[int] = None
        status: Optional[str] = None
        try:
            # Cancel the stuck-modal timeout — we got the marker.
            if self._btw_timeout_task is not None and not self._btw_timeout_task.done():
                self._btw_timeout_task.cancel()
                self._btw_timeout_task = None

            # 1. Dismiss the modal with ESC.  Also resets last_input_ms,
            #    which the idle-timer watchdog uses — an ESC is an input.
            try:
                self.write(BTW_DISMISS_SEQUENCE)
            except Exception as exc:
                _log.warning(
                    "pty: btw finalize — ESC write failed (session %s): %s",
                    self.session_id,
                    exc,
                )

            # 2. Allow the TUI to complete its modal tear-down.
            await asyncio.sleep(_BTW_DISMISS_SETTLE_S)

            # 3. Extract the response text.
            raw = bytes(self._state.btw_buffer)
            extracted = extract_btw_response(raw)

            # 4. Persist the response row if we have both sides.
            if (
                extracted is not None
                and input_row_id is not None
                and self._db is not None
            ):
                try:
                    response_row_id = await self._db.record_ephemeral_response(
                        input_row_id, extracted
                    )
                except Exception as exc:
                    _log.warning(
                        "pty: btw finalize — record_ephemeral_response failed "
                        "(session %s, input_row_id=%s): %s",
                        self.session_id,
                        input_row_id,
                        exc,
                    )
                else:
                    _log.info(
                        "pty: btw response persisted (session %s, input_row=%s, "
                        "response_row=%s, extracted_len=%d)",
                        self.session_id,
                        input_row_id,
                        response_row_id,
                        len(extracted),
                    )
                    if self._bus is not None and response_row_id is not None:
                        self._bus.publish(
                            {
                                "type": "ephemeral_pair_persisted",
                                "session_id": self.session_id,
                                "input_id": input_row_id,
                                "response_id": response_row_id,
                                "kind": "btw",
                            }
                        )
                    status = "completed"
            elif extracted is None:
                # Promoted from DEBUG to WARNING — extraction failure is a
                # real bug worth surfacing in ops logs. The raw buffer is the
                # live PTY output and may contain on-screen secrets, so we only
                # dump it to disk when AGENT_DECODER_DEBUG is set (opt-in); by
                # default we just log its length.
                dump_path: Optional[Path] = None
                if os.environ.get("AGENT_DECODER_DEBUG"):
                    dump_path = Path("/tmp") / f"btw-extract-fail-{self.session_id}.bin"
                    try:
                        dump_path.write_bytes(raw)
                    except Exception:
                        dump_path = None
                _log.warning(
                    "pty: btw finalize — extraction yielded None "
                    "(session %s, buffer_len=%d%s)",
                    self.session_id,
                    len(raw),
                    f", dumped to {dump_path}"
                    if dump_path
                    else " (set AGENT_DECODER_DEBUG to dump raw bytes)",
                )
                status = "failed"
            else:
                status = "failed"

        except asyncio.CancelledError:
            # Stuck-timeout or shutdown cancelled us — let the timeout handler
            # (or kill) clean up state.
            raise
        except Exception as exc:
            _log.warning(
                "pty: btw finalize raised unexpectedly (session %s): %s",
                self.session_id,
                exc,
            )
            status = "failed"
        finally:
            if status is not None:
                self._publish_submit_completed(
                    kind="btw",
                    status=status,
                    input_row_id=input_row_id,
                    response_row_id=response_row_id,
                )
            # 5. Reset channel state regardless of success / failure.
            self._state.expecting_btw_response = False
            self._state.btw_buffer.clear()
            self._state.btw_input_row_id = None

        _log.debug(
            "pty: btw capture complete (session %s, input_row=%s, response_row=%s)",
            self.session_id,
            input_row_id,
            response_row_id,
        )

    async def _btw_stuck_timeout(self) -> None:
        """Backstop: if the response-complete marker never arrives, give up.

        After ``self._btw_stuck_timeout_s`` seconds (default
        :data:`_BTW_STUCK_TIMEOUT_S`), log a warning, attempt extraction on
        whatever's in the buffer (may yield None), persist any extracted
        text, and reset state.  Cancelled if _finalize_btw_capture() fires
        first (marker arrived normally).

        Override ``channel._btw_stuck_timeout_s`` in tests to shorten the
        wait without patching the module-level constant.
        """
        try:
            await asyncio.sleep(self._btw_stuck_timeout_s)
        except asyncio.CancelledError:
            return

        _log.warning(
            "pty: btw stuck-modal timeout after %.0fs (session %s); "
            "buffer=%d bytes — forcing finalize",
            self._btw_stuck_timeout_s,
            self.session_id,
            len(self._state.btw_buffer),
        )

        # Cancel any pending finalize (shouldn't exist if timeout fires,
        # but be safe).
        if self._btw_finalize_task is not None and not self._btw_finalize_task.done():
            self._btw_finalize_task.cancel()
            self._btw_finalize_task = None

        input_row_id = self._state.btw_input_row_id
        raw = bytes(self._state.btw_buffer)
        extracted = extract_btw_response(raw)

        response_row_id: Optional[int] = None
        if extracted is not None and input_row_id is not None and self._db is not None:
            try:
                response_row_id = await self._db.record_ephemeral_response(
                    input_row_id, extracted
                )
            except Exception as exc:
                _log.warning(
                    "pty: btw timeout — record_ephemeral_response failed "
                    "(session %s): %s",
                    self.session_id,
                    exc,
                )
            else:
                if self._bus is not None and response_row_id is not None:
                    self._bus.publish(
                        {
                            "type": "ephemeral_pair_persisted",
                            "session_id": self.session_id,
                            "input_id": input_row_id,
                            "response_id": response_row_id,
                            "kind": "btw",
                        }
                    )

        self._publish_submit_completed(
            kind="btw",
            status="timed_out",
            input_row_id=input_row_id,
            response_row_id=response_row_id,
        )

        # Reset state.
        self._state.expecting_btw_response = False
        self._state.btw_buffer.clear()
        self._state.btw_input_row_id = None

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def is_alive(self) -> bool:
        """True iff the subprocess is running and the master fd is open."""
        proc = self._state.proc
        if proc is None or self._state.dead:
            return False
        return proc.returncode is None

    def last_activity_ms(self) -> int:
        """Max of last input and last output timestamps."""
        return max(self._state.last_input_ms, self._state.last_pty_output_ms)

    def last_input_ms(self) -> int:
        """Timestamp of the most recent ``write()``, in ms since epoch."""
        return self._state.last_input_ms

    def last_pty_output_ms(self) -> int:
        """Timestamp of the most recent drained chunk, in ms since epoch."""
        return self._state.last_pty_output_ms

    def last_pty_output_seq(self) -> int:
        """Monotonic count of drained PTY output chunks."""
        return self._state.last_pty_output_seq

    def output_snapshot(self) -> bytes:
        """Copy of the bounded PTY output ring for Native View hydration."""
        return bytes(self._state.ring)

    def output_snapshot_complete(self) -> bool:
        """True iff the output ring has not dropped earlier terminal bytes."""
        return self._state.ring_complete

    def dimensions(self) -> tuple[int, int]:
        """Current terminal dimensions as ``(rows, cols)``."""
        return self._rows, self._cols

    # ------------------------------------------------------------------
    # Window size
    # ------------------------------------------------------------------

    def resize(self, rows: int, cols: int) -> None:
        """Update terminal window size on the live PTY.

        ``TIOCSWINSZ`` applied to the master fd propagates to the child
        as a SIGWINCH — the TUI re-lays-out automatically.
        """
        if rows <= 0 or cols <= 0:
            raise ValueError(f"invalid window size rows={rows} cols={cols}")
        # Only a COLUMN change can garble a later ring replay: the captured
        # bytes were wrapped at the old width and xterm would re-wrap them
        # wrong. A ROW change is lossless — xterm reflows scrollback on
        # height changes — so the ring must survive it. Clearing on every
        # dimension change was wiping history precisely when it mattered:
        # the FE's post-spawn resize always bumps the row count (spawn is
        # DEFAULT_ROWS → the fitted viewport height) ~1s into claude's
        # multi-second history render, so the clear discarded the oldest
        # history (the top of the conversation) from the ring — the
        # "scroll up but never reach the top" bug on re-attach.
        cols_changed = self._cols != cols
        self._rows = rows
        self._cols = cols
        if cols_changed:
            self._state.ring.clear()
            self._state.ring_complete = True
        if self._state.master_fd < 0:
            return
        try:
            _set_winsize(self._state.master_fd, rows, cols)
        except OSError as exc:
            _log.warning(
                "pty: resize failed (session %s): %s",
                self.session_id,
                exc,
            )

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    async def kill(self) -> None:
        """Tear the channel down: SIGINT, double-Ctrl-C, SIGKILL.

        Order matters per Phase 0 finding:
            1. ``loop.remove_reader(master_fd)``
            2. SIGINT, wait up to 3s
            3. write ``\\x03\\x03`` to master, wait up to 3s
            4. SIGKILL
            5. ``os.close(master_fd)``
            6. ``await proc.wait()``
        Closing the master fd BEFORE the final ``wait()`` prevents a
        shutdown hang where claude blocks on a write to its now-orphaned
        controlling tty.
        """
        if self._state.proc is None:
            # start() never completed (or kill() already ran).
            self._close_master_fd()
            return

        # 1. Stop draining first — we're about to tear the fd down and any
        # in-flight callback would race the close.
        if self._state.reader_registered and self._state.master_fd >= 0:
            try:
                loop = asyncio.get_running_loop()
                loop.remove_reader(self._state.master_fd)
            except (RuntimeError, ValueError):
                pass
            self._state.reader_registered = False

        proc = self._state.proc

        # 2. SIGINT — claude's TUI handles this cleanly (single Ctrl-C
        # cancels in-flight tool calls; second sends the goodbye).
        if proc.returncode is None:
            try:
                proc.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(
                    asyncio.shield(proc.wait()), timeout=_KILL_SIGINT_WAIT_S
                )
            except asyncio.TimeoutError:
                pass

        # 3. Double Ctrl-C through the PTY — bypasses signal handling and
        # tells the TUI's input layer "I really mean it".
        if proc.returncode is None and self._state.master_fd >= 0:
            try:
                os.write(self._state.master_fd, b"\x03\x03")
            except OSError:
                pass
            try:
                await asyncio.wait_for(
                    asyncio.shield(proc.wait()), timeout=_KILL_CTRLC_WAIT_S
                )
            except asyncio.TimeoutError:
                pass

        # 4. SIGKILL — nothing left to be polite about.
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass

        # 5. Close master fd BEFORE the final wait().
        self._close_master_fd()

        # 6. Reap.
        try:
            await proc.wait()
        except Exception as exc:  # pragma: no cover — defensive
            _log.warning(
                "pty: wait after kill raised (session %s): %s",
                self.session_id,
                exc,
            )

        # Drop ourselves from the ownership-allowlist now that the pid
        # is reaped. Discard (not remove) — start() may not have added
        # it if the spawn errored early.
        _OWN_CLAUDE_PIDS.discard(proc.pid)

        # Phase 1 — release the lock sidecar AFTER the subprocess is
        # reaped. Releasing earlier would let a second ring
        # acquire while our claude is still partway through its
        # tear-down JSONL writes.
        if self._lock_sidecar is not None:
            try:
                self._lock_sidecar.release()
            except Exception as exc:  # pragma: no cover — defensive
                _log.debug(
                    "pty: lock release at kill raised (session %s): %s",
                    self.session_id,
                    exc,
                )
            self._lock_sidecar = None

        self._state.dead = True
        _log.info("pty: killed (session %s, rc=%s)", self.session_id, proc.returncode)

    def _close_master_fd(self) -> None:
        if self._state.master_fd >= 0:
            try:
                os.close(self._state.master_fd)
            except OSError:
                pass
            self._state.master_fd = -1


# ---------------------------------------------------------------------------
# PtyManager
# ---------------------------------------------------------------------------


def _build_argv(
    *,
    bin_name: str,
    session_id: str,
    model: str,
    permission_mode: str,
    new_chat: bool,
) -> list[str]:
    """Compose argv per the plan's session-lifecycle rules.

    new_chat=True  -> [bin, --session-id, sid, --permission-mode, mode,
                       (--model, model)?]
    new_chat=False -> [bin, --resume, sid, --permission-mode, mode,
                       (--model, model)?]

    For new chats we pass ``--session-id`` so claude writes its JSONL at the
    id we minted in ``pending_sessions``; without this claude would mint its
    own UUID and the sidebar would show both our pending placeholder and the
    real session as separate entries.
    """
    argv: list[str] = [bin_name]
    if new_chat:
        argv.extend(["--session-id", session_id])
    else:
        argv.extend(["--resume", session_id])
    argv.extend(["--permission-mode", permission_mode])
    if model:
        argv.extend(["--model", model])
    return argv


@dataclass
class _FocusParams:
    """Cached focus arguments so ``submit()`` can auto-respawn a dead channel.

    A channel can die for two reasons in Phase 1:
      - idle-timer kill (planned, after 5 minutes of inactivity)
      - subprocess crash / unexpected exit (unplanned)
    Both want the next ``submit()`` on the session to silently respawn
    with the parameters most recently provided to ``focus()``. We cache
    them here rather than asking the caller to re-supply on every submit.
    Auto-respawn always uses ``new_chat=False`` (we ``--resume`` an
    existing session id); the original ``new_chat`` value is recorded
    only for completeness.
    """

    cwd: str
    bin_name: str
    model: str
    permission_mode: str
    new_chat: bool
    # Initial PTY row count. The Native view fits the terminal to the pane
    # BEFORE spawning and passes the fitted rows here, so claude renders at the
    # final height from the first frame — no spawn-at-40-then-resize-to-N grow,
    # which left stale content in the revealed rows (smear) and stranded
    # claude's footer mid-screen (the bottom gap). Cols stay native_pty_cols
    # (already the spawn width). Defaults to DEFAULT_ROWS for non-native callers.
    rows: int = DEFAULT_ROWS
    # Absolute JSONL path for ownership conflict detection. ``None`` for
    # brand-new sessions whose JSONL doesn't exist yet (no one to
    # conflict with) and for any caller that hasn't plumbed the path
    # through — detection is skipped in that case.
    jsonl_path: Optional[Path] = None


@dataclass
class _ManagedChannel:
    """PtyManager's per-session bookkeeping around a single ``PtyChannel``."""

    channel: PtyChannel
    focus_params: _FocusParams
    idle_warn_handle: Optional[asyncio.TimerHandle] = None
    idle_kill_handle: Optional[asyncio.TimerHandle] = None
    idle_warn_at_ms: int = 0
    idle_kill_at_ms: int = 0


class PtyManager:
    """Singleton attached to ``app.state.pty_manager``.

    Phase 1 responsibilities:
      - lazy-spawn a ``PtyChannel`` on ``focus()`` (or auto on ``submit()``)
      - idle-timer with SSE warn at ``idle_warn_s`` and kill at
        ``idle_timeout_s``
      - clean ``shutdown()`` in the FastAPI lifespan ``finally`` block

    Phase 3 added a per-session ``asyncio.Lock`` keyed on session id.
    ``focus``/``submit``/``kill``/``unfocus``/``switch_model`` acquire
    it first, then the global ``self._lock``. Two ops on the same sid
    serialize (so two browser tabs submitting at once don't interleave
    PTY writes); cross-session ops stay parallel.

    The ``db`` parameter is stored but unused in Phase 1 — Phase 2 will
    use it for ``/btw`` ephemeral capture (both input and PTY-output
    sides) and Phase 4 for stuck-session detection via
    ``Session.updated_at`` lookup.
    """

    def __init__(
        self,
        db: "Database",
        bus: "EventBroadcaster",
        *,
        idle_timeout_s: float = DEFAULT_IDLE_TIMEOUT_S,
        idle_warn_s: float = DEFAULT_IDLE_WARN_S,
        blurred_idle_timeout_s: float = BLURRED_IDLE_TIMEOUT_S,
        ui_endpoint: Optional[str] = None,
    ) -> None:
        if idle_warn_s >= idle_timeout_s:
            raise ValueError(
                f"idle_warn_s ({idle_warn_s}) must be < "
                f"idle_timeout_s ({idle_timeout_s})"
            )
        self._db = db
        self._bus = bus
        # Phase-1 lock sidecar metadata. ``ui_endpoint`` is plumbed
        # into each PtyChannel so a peer agent-decoder reading the lock
        # can render a "open in UI at …" link in its take-over banner.
        # Falls back to ``None`` for tests / non-HTTP callers.
        self._ui_endpoint = ui_endpoint
        self._idle_timeout_s = idle_timeout_s
        self._idle_warn_s = idle_warn_s
        self._blurred_idle_timeout_s = blurred_idle_timeout_s
        self._channels: dict[str, _ManagedChannel] = {}
        # Most recent focus params per session — outlives the channel so
        # auto-respawn after an idle kill can recover transparently. The
        # cached entry persists until explicit ``kill()`` or ``shutdown()``.
        self._last_focus: dict[str, _FocusParams] = {}
        # Top-level lock guarding channel-table mutations. Acquired
        # INSIDE the per-session lock so the global section is short
        # (dict mutations only). Spawning + PTY writes happen outside.
        self._lock = asyncio.Lock()
        # Phase 3 — per-session asyncio.Lock keyed on session id.
        # Wraps the full focus/submit/kill body so concurrent ops on
        # the same sid serialize while cross-session ops stay parallel.
        # Allocated lazily by ``_session_lock``; entries are not
        # removed on kill (cheap; bounded by sessions ever-touched).
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._shutdown_started = False
        # Per-session input watchdog tasks. After each submit() we spawn a
        # short-lived task that emits SSE pty_input_acknowledged once the
        # TUI has echoed something (~500ms) or pty_input_stalled if
        # nothing came back within ~5s. The frontend uses these to drive
        # the "Thinking" indicator without blind timers. A new submit
        # cancels the prior watchdog so we only ever report on the latest.
        self._input_watchdogs: dict[str, asyncio.Task] = {}
        # Authoritative native PTY width (cols). Single source of truth for
        # both the spawn winsize and what the browser terminal renders at.
        # Seeded from AppConfig.native_pty_cols at startup and refreshed on
        # config update via set_native_cols(); falls back to DEFAULT_COLS.
        self._native_cols: int = DEFAULT_COLS
        # The session currently displayed in the native view. Only this
        # session's PTY output chunks are broadcast to SSE; all others are
        # silently dropped to prevent a JSON.parse firehose on the main thread
        # that scales with live-PTY count. Set by focus() and
        # native_snapshot(); cleared by unfocus().
        self._active_session_id: Optional[str] = None
        # Output coalescing (see _scan_chunk_for_hitl / _flush_output). claude
        # writes a TUI repaint as many tiny PTY writes; we batch the raw reads
        # per session and flush one combined output chunk + at most one state
        # reclassification per _OUTPUT_COALESCE_S window.
        self._pending_output: dict[str, bytearray] = {}
        self._output_flush_handles: dict[str, asyncio.TimerHandle] = {}
        # Last (state, decoded_input_safe) published per session — pty_native_state
        # is emitted only when this changes, so a scroll (state unchanged) emits
        # none instead of one per chunk.
        self._last_native_state: dict[str, tuple[str, bool]] = {}

    def set_native_cols(self, cols: int) -> None:
        """Set the width new PTYs spawn at (from AppConfig.native_pty_cols).

        Affects only channels spawned after this call — live channels keep
        their width until respawn (changing a running PTY's width would
        force Claude to reflow committed scrollback).
        """
        if cols > 0:
            self._native_cols = cols

    # ------------------------------------------------------------------
    # Locking helpers
    # ------------------------------------------------------------------

    def _session_lock(self, session_id: str) -> asyncio.Lock:
        """Phase-3 per-session lock. Lazily allocated; never removed.

        Lock ordering is ALWAYS ``_session_lock`` → ``self._lock``. The
        global lock guards the per-session-dict mutations and timer
        bookkeeping; the per-session lock wraps the whole op so two
        submits on the same sid serialize their PTY writes.

        Entries persist for the manager's lifetime — asyncio.Lock is
        cheap (≈ 200 B), sessions touched in a day are bounded, and
        cleanup-on-kill races with a fresh focus that would re-allocate
        a different Lock instance and silently lose the serialization.
        """
        lock = self._session_locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[session_id] = lock
        return lock

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def focus(
        self,
        session_id: str,
        *,
        cwd: str,
        bin_name: str,
        model: str,
        permission_mode: str,
        new_chat: bool,
        rows: int = DEFAULT_ROWS,
        jsonl_path: Optional[Path] = None,
    ) -> None:
        """Ensure a live PTY channel exists for ``session_id``.

        Idempotency: if a channel is already alive for this session AND
        its spawn-time ``bin_name`` / ``cwd`` / ``model`` / ``permission_mode``
        match the incoming request, this is a true no-op — we don't even
        touch the idle timer. The frontend was observed firing focus()
        eagerly on every input click; respawning (or even just bumping
        timers) on each call contributes to orphan PTY claudes and resets
        idle bookkeeping the user didn't ask to reset.

        If the channel is dead (or absent), or if any spawn-relevant
        param differs from the live channel's, fall through to the
        existing spawn/swap logic. ``new_chat`` is intentionally NOT
        part of the match — once a session is live the id is real on
        disk and ``--resume`` semantics apply regardless.

        A model mismatch on an already-live channel is intentionally
        deferred to ``submit()``, which has the in-flight guard needed to
        decide whether it is safe to send claude's ``/model`` command.
        """
        async with self._session_lock(session_id):
            async with self._lock:
                if self._shutdown_started:
                    raise RuntimeError("PtyManager is shutting down")

                # Fast-path: live channel with matching spawn params → no
                # respawn. We still reset the idle timer so a revisit after
                # the FE-initiated short blur-kill window restores the full
                # ``idle_timeout_s`` lease. Skipping the reset would leave a
                # previously-blurred channel scheduled to die ~30s after
                # blur, even if the user has navigated back to it.
                existing = self._channels.get(session_id)
                if (
                    existing is not None
                    and existing.channel.is_alive()
                    and existing.focus_params.bin_name == bin_name
                    and existing.focus_params.cwd == cwd
                    and existing.focus_params.model == model
                    and existing.focus_params.permission_mode == permission_mode
                ):
                    foreign = _session_conflict_pids(session_id, jsonl_path)
                    if foreign:
                        raise PtyOwnershipConflict(foreign)
                    self._set_active_session_locked(session_id)
                    self._reset_idle_timer_locked(session_id)
                    return

                # Phase 0 — ownership conflict detection. Runs only when
                # the fast-path above didn't return (i.e. we're about to
                # spawn or respawn). For a foreign-attached session,
                # refuse rather than spawn a racing claude; the FE
                # surfaces this as a take-over banner.
                foreign = _session_conflict_pids(session_id, jsonl_path)
                if foreign:
                    raise PtyOwnershipConflict(foreign)

                params = _FocusParams(
                    cwd=cwd,
                    bin_name=bin_name,
                    model=model,
                    permission_mode=permission_mode,
                    new_chat=new_chat,
                    rows=rows,
                    jsonl_path=jsonl_path,
                )
                self._last_focus[session_id] = params
                await self._ensure_channel_locked(session_id, params)
                self._set_active_session_locked(session_id)
                self._reset_idle_timer_locked(session_id)

    async def unfocus(self, session_id: str) -> None:
        """Note that input lost focus — shorten the idle-kill window.

        The channel stays alive (refocusing within the window costs no
        respawn), but the next idle-kill is brought close so orphan PTYs
        don't accumulate during sidebar navigation. The FE wires this to
        the ChatInput cleanup so every session-switch marks the previous
        session as nav'd-away.

        No-op if the channel is already gone. If a previously-scheduled
        kill was further out (typical: a fresh focus set the 5-minute
        timer), the timer is shortened. If it was already shorter
        (multiple blurs in a row), the existing schedule is preserved
        to avoid pushing the kill further out.
        """
        async with self._session_lock(session_id):
            async with self._lock:
                managed = self._channels.get(session_id)
                if managed is None:
                    return
                # Keep the most-recent (last-focused / on-screen) session's PTY
                # alive so navigating to another session and back doesn't force
                # a respawn. It is reaped only when a different native session
                # supersedes it (_set_active_session_locked) or the tab closes
                # (pagehide → ptyKillKeepalive). Older, non-active sessions
                # still get the fast blurred kill here.
                if self._active_session_id == session_id:
                    return
                self._shorten_idle_kill_locked(session_id)

    def _shorten_idle_kill_locked(self, session_id: str) -> None:
        """Re-schedule the idle-kill timer to ``_blurred_idle_timeout_s``,
        but only if the currently-scheduled kill is further out than that.
        Idempotent across repeated blurs."""
        managed = self._channels.get(session_id)
        if managed is None:
            return
        loop = asyncio.get_running_loop()
        now_ms = _now_ms()
        target_ms = now_ms + int(self._blurred_idle_timeout_s * 1000)
        if managed.idle_kill_at_ms and managed.idle_kill_at_ms <= target_ms:
            # Already scheduled to die sooner; don't extend.
            return
        self._cancel_idle_timers(managed)
        # No warning event for blur — straight to the kill schedule.
        managed.idle_warn_at_ms = 0
        managed.idle_kill_at_ms = target_ms
        managed.idle_kill_handle = loop.call_later(
            self._blurred_idle_timeout_s, self._on_idle_kill, session_id
        )

    def _set_active_session_locked(self, session_id: str) -> None:
        """Mark ``session_id`` as the session currently on screen.

        Reap the PREVIOUS most-recent session's PTY (if different) by
        shortening it to the blurred window, so opening a new native view
        cleans up the one it replaces. The newly-active session is then
        protected from idle-kill (``_on_idle_kill`` re-arms it) until it is
        itself superseded or the tab closes — so navigating to a non-native
        session and back finds the PTY still alive (no respawn).
        """
        prev = self._active_session_id
        if prev is not None and prev != session_id:
            self._shorten_idle_kill_locked(prev)
        self._active_session_id = session_id

    async def submit(
        self,
        session_id: str,
        content: str,
    ) -> None:
        """Write ``content`` to the PTY as a TUI message submission.

        Translation:
          - in-message ``\\n`` characters become Ctrl+J (0x0a) bytes
          - a trailing ``\\r`` (0x0d) is appended to submit

        If no live channel exists (e.g. idle-killed since last activity),
        we transparently respawn one using the focus parameters cached on
        the most recent ``focus()`` call. The respawn uses
        ``new_chat=False`` because the session id already exists on disk;
        callers that need to mint a brand-new session must call
        ``focus(new_chat=True)`` themselves. If no focus has ever happened
        for this session, we raise — the server route is responsible for
        ensuring an initial focus.
        """
        # Phase 2 — /btw ephemeral capture: detect a leading /btw at the
        # TOP of submit(), then persist its user row only after we know no
        # prior /btw capture is in flight. The DB write still commits before
        # bytes reach the PTY; the guard prevents duplicate orphan rows and
        # slash commands being swallowed by the active /btw modal.
        is_btw = is_btw_input(content)
        is_slash = content.lstrip().startswith("/") and not is_btw
        btw_input_row_id: Optional[int] = None

        async with self._session_lock(session_id):
            async with self._lock:
                if self._shutdown_started:
                    raise RuntimeError("PtyManager is shutting down")
                managed = self._channels.get(session_id)
                if managed is None or not managed.channel.is_alive():
                    params = self._last_focus.get(session_id)
                    if params is None:
                        raise RuntimeError(
                            f"no PTY channel and no cached focus params for "
                            f"session {session_id}; call focus() first"
                        )
                    # Auto-respawn: never start a NEW chat on submit — the
                    # session id is real and on disk, so always --resume.
                    respawn = _FocusParams(
                        cwd=params.cwd,
                        bin_name=params.bin_name,
                        model=params.model,
                        permission_mode=params.permission_mode,
                        new_chat=False,
                        rows=params.rows,
                        jsonl_path=params.jsonl_path,
                    )
                    # Phase 0 — same ownership check focus() runs. Submit
                    # auto-respawn is a spawn path; if a foreign claude
                    # attached to the session during our idle-kill window,
                    # refuse.
                    foreign = _session_conflict_pids(session_id, respawn.jsonl_path)
                    if foreign:
                        raise PtyOwnershipConflict(foreign)
                    self._last_focus[session_id] = respawn
                    managed = await self._ensure_channel_locked(session_id, respawn)
                else:
                    foreign = _session_conflict_pids(
                        session_id,
                        managed.focus_params.jsonl_path,
                    )
                    if foreign:
                        raise PtyOwnershipConflict(foreign)
                payload = self._encode_submit(content)
                channel = managed.channel
                self._reset_idle_timer_locked(session_id)

                if channel._state.expecting_btw_response:
                    raise PtySubmitInFlight(
                        "A /btw response is still being captured. Wait for it "
                        "to finish before sending another message."
                    )

                if is_btw:
                    try:
                        btw_input_row_id = await self._db.record_ephemeral_input(
                            session_id, content, kind="btw"
                        )
                        if btw_input_row_id is not None:
                            self._bus.publish(
                                {
                                    "type": "ephemeral_input_persisted",
                                    "session_id": session_id,
                                    "input_id": btw_input_row_id,
                                    "kind": "btw",
                                }
                            )
                    except Exception as exc:
                        _log.warning(
                            "pty: record_ephemeral_input failed (session %s): %s",
                            session_id,
                            exc,
                        )
                        # Proceed: losing the DB row is better than blocking the /btw
                        # flow.  btw_input_row_id stays None so finalize won't try
                        # to persist the response row either.

                # Phase 2 — /btw capture: arm the channel's accumulator
                # state now that we have the live channel reference and
                # hold the global lock (ensuring atomicity with the ring
                # drain).  Clear the buffer first so back-to-back /btw
                # submits don't cross-contaminate.
                if is_btw:
                    channel._state.expecting_btw_response = True
                    channel._state.btw_input_row_id = btw_input_row_id
                    channel._state.btw_buffer.clear()
                    # Cancel any leftover finalize/timeout tasks from a
                    # previous /btw on the same channel (shouldn't happen
                    # in normal flows, but be safe).
                    for task_attr in ("_btw_finalize_task", "_btw_timeout_task"):
                        t = getattr(channel, task_attr)
                        if t is not None and not t.done():
                            t.cancel()
                        setattr(channel, task_attr, None)
                    # Schedule the stuck-modal timeout backstop.
                    try:
                        loop = asyncio.get_running_loop()
                        channel._btw_timeout_task = loop.create_task(
                            channel._btw_stuck_timeout()
                        )
                        _log.info(
                            "pty: btw armed — input_row=%s, timeout_s=%.0f (session %s)",
                            btw_input_row_id,
                            channel._btw_stuck_timeout_s,
                            session_id,
                        )
                    except RuntimeError:  # pragma: no cover — loop closed
                        _log.warning(
                            "pty: btw arm — loop unavailable, no timeout task scheduled (session %s)",
                            session_id,
                        )

            # Inside the per-session lock, outside the global lock. A
            # slow os.write here serializes against other submits on
            # THIS sid (Phase-3 guarantee — no JSONL interleaving) but
            # leaves other sessions unblocked. The channel reference is
            # stable because shutdown() awaits the global lock before
            # tearing channels down.
            submit_ms = _now_ms()
            submit_output_seq = channel.last_pty_output_seq()
            channel.write(payload)
            # Kick off the input watchdog: emits pty_input_acknowledged
            # once the TUI starts producing output, or pty_input_stalled
            # if it doesn't react within ~5s.
            prior = self._input_watchdogs.pop(session_id, None)
            if prior is not None and not prior.done():
                prior.cancel()
            self._input_watchdogs[session_id] = asyncio.create_task(
                self._input_watchdog(
                    session_id,
                    submit_ms,
                    submit_output_seq,
                    submit_kind="slash" if is_slash else None,
                )
            )

    async def write_raw_input(self, session_id: str, data: bytes) -> None:
        """Write raw terminal input bytes to the live PTY.

        This is the Native View input path. It intentionally bypasses
        ``_encode_submit`` so escape sequences, control characters, and
        partial terminal input reach Claude's TUI unchanged.
        """
        async with self._session_lock(session_id):
            async with self._lock:
                if self._shutdown_started:
                    raise RuntimeError("PtyManager is shutting down")
                managed = self._channels.get(session_id)
                if managed is None or not managed.channel.is_alive():
                    raise RuntimeError(f"no live PTY channel for session {session_id}")
                channel = managed.channel
                self._reset_idle_timer_locked(session_id)

            channel.write(data)

    async def resize(self, session_id: str, rows: int, cols: int) -> None:
        """Resize a live PTY channel for Native View."""
        async with self._session_lock(session_id):
            async with self._lock:
                if self._shutdown_started:
                    raise RuntimeError("PtyManager is shutting down")
                managed = self._channels.get(session_id)
                if managed is None or not managed.channel.is_alive():
                    raise RuntimeError(f"no live PTY channel for session {session_id}")
                channel = managed.channel
                self._reset_idle_timer_locked(session_id)

            channel.resize(rows, cols)

    def native_snapshot(self, session_id: str) -> dict[str, Any]:
        """Return Native View hydration state for a live PTY channel."""
        managed = self._channels.get(session_id)
        if managed is None:
            raise RuntimeError(f"no PTY channel for session {session_id}")
        self._set_active_session_locked(session_id)
        ch = managed.channel
        rows, cols = ch.dimensions()
        alive = ch.is_alive()
        if alive:
            screen_text = ch.output_snapshot().decode("utf-8", errors="replace")
            classification = classify_screen(screen_text)
            native_state = classification.state
            decoded_input_safe = classification.decoded_input_safe
        else:
            native_state = "dead"
            decoded_input_safe = False
        return encode_pty_snapshot(
            session_id=session_id,
            ring=ch.output_snapshot(),
            ring_complete=ch.output_snapshot_complete(),
            rows=rows,
            cols=cols,
            alive=alive,
            native_state=native_state,
            decoded_input_safe=decoded_input_safe,
        )

    def _flush_output(self, channel: PtyChannel) -> None:
        """Emit the coalesced output for a channel (scheduled by the on_chunk
        hook). Publishes ONE combined ``pty_output_chunk`` for the active session
        plus, at most, ONE ``pty_native_state`` — and only when the classified
        state actually changed (it stays constant through a scroll, so this drops
        nearly all of the per-chunk state firehose). Runs on the loop; must not
        raise.

        Takes the live ``channel`` directly rather than re-looking it up in
        ``_channels``: output can arrive during spawn/bootstrap BEFORE focus()
        registers the channel, and a ``_channels`` miss would silently drop that
        first state/output. The on_chunk hook always has a valid channel ref.
        """
        session_id = channel.session_id
        self._output_flush_handles.pop(session_id, None)
        pending = self._pending_output.pop(session_id, None)

        if pending and session_id == self._active_session_id:
            try:
                self._bus.publish(encode_pty_output_chunk(session_id, bytes(pending)))
            except Exception as exc:
                _log.warning(
                    "pty: output chunk publish raised (session %s): %s",
                    session_id,
                    exc,
                )

        try:
            # Classify only the TAIL of the ring, never the whole thing. The
            # state we detect (prompts, trust/login banners) is always on the
            # CURRENT screen, which lives in the last screenful of output — 4 MB
            # of scrollback history is both irrelevant and dangerous to classify
            # (stale matches), and classify_screen → _normalize runs three regex
            # passes plus a char-by-char comprehension over its input, so feeding
            # it the whole ring is O(session length) work. Capping at
            # _CLASSIFY_TAIL_BYTES keeps each call cheap and constant-time.
            ring = channel._state.ring
            tail = (
                ring[-_CLASSIFY_TAIL_BYTES:]
                if len(ring) > _CLASSIFY_TAIL_BYTES
                else ring
            )
            classification = classify_screen(
                bytes(tail).decode("utf-8", errors="replace")
            )
            key = (classification.state, classification.decoded_input_safe)
            if self._last_native_state.get(session_id) != key:
                self._last_native_state[session_id] = key
                self._bus.publish(
                    {
                        "type": "pty_native_state",
                        "session_id": session_id,
                        "state": classification.state,
                        "decoded_input_safe": classification.decoded_input_safe,
                    }
                )
        except Exception as exc:
            _log.warning(
                "pty: native state publish raised (session %s): %s",
                session_id,
                exc,
            )

    def _clear_output_coalesce(self, session_id: str) -> None:
        """Drop a session's output-coalescing state (cancel any pending flush).
        Called when a channel is removed so timers and buffers don't linger."""
        handle = self._output_flush_handles.pop(session_id, None)
        if handle is not None:
            handle.cancel()
        self._pending_output.pop(session_id, None)
        self._last_native_state.pop(session_id, None)

    def _scan_chunk_for_hitl(self, channel: PtyChannel, chunk: bytes) -> None:
        """PtyChannel ``on_chunk`` hook — scans drained bytes for HITL signals.

        Currently watches for the ``Not logged in`` banner that ``claude``
        prints when the user's subscription / auth has lapsed. Emits the
        ``auth_required`` SSE event once per channel lifetime (a respawn
        gets a fresh channel and may re-emit) and matches the payload
        shape the frontend expects (``frontend/src/api/client.ts`` ~L319).

        The check straddles ring boundaries: we concatenate the tail of
        the ring (already written by the caller) with the new chunk only
        when the chunk alone doesn't contain the marker — handles the
        rare case where the pattern is split across two read() calls.

        Sync (runs inside the asyncio reader callback). Must not block.
        """
        # Coalesce the output broadcast AND the state reclassification onto a
        # short timer instead of doing them per raw read. claude writes a single
        # TUI repaint as many tiny (~1 KB) PTY writes, each firing add_reader
        # separately; publishing one SSE event + reclassifying per read turned a
        # single scroll flick into ~360 SSE events and ~180 frontend repaints
        # (measured). We accumulate raw reads per session and flush ONE combined
        # output chunk (+ at most one reclassification) every _OUTPUT_COALESCE_S
        # — so the browser does ~one xterm.write per frame. Faithful: the bytes
        # are delivered in order, only batched.
        self._pending_output.setdefault(channel.session_id, bytearray()).extend(chunk)
        if channel.session_id not in self._output_flush_handles:
            try:
                loop = asyncio.get_running_loop()
                self._output_flush_handles[channel.session_id] = loop.call_later(
                    _OUTPUT_COALESCE_S, self._flush_output, channel
                )
            except RuntimeError:
                # No running loop (should not happen in the reader callback) —
                # flush inline so output is never stranded.
                self._flush_output(channel)

        state = channel._state
        if state.auth_required_emitted:
            return
        if _AUTH_REQUIRED_PATTERN in chunk:
            hit = True
        else:
            # The ring already contains ``chunk`` (caller appended before
            # invoking us). Inspect the trailing window covering the
            # boundary between the previous chunk and this one.
            ring = state.ring
            window_start = max(0, len(ring) - len(chunk) - _AUTH_REQUIRED_CARRYOVER)
            hit = _AUTH_REQUIRED_PATTERN in ring[window_start:]
        if not hit:
            return
        state.auth_required_emitted = True
        try:
            self._bus.publish(
                {
                    "type": "auth_required",
                    "session_id": channel.session_id,
                }
            )
        except Exception as exc:
            _log.warning(
                "pty: auth_required publish raised (session %s): %s",
                channel.session_id,
                exc,
            )

    def _publish_native_dead_state(self, channel: PtyChannel) -> None:
        """PtyChannel ``on_dead`` hook — publish terminal Native View state."""
        try:
            self._bus.publish(
                {
                    "type": "pty_native_state",
                    "session_id": channel.session_id,
                    "state": "dead",
                    "decoded_input_safe": False,
                }
            )
        except Exception as exc:
            _log.warning(
                "pty: native dead-state publish raised (session %s): %s",
                channel.session_id,
                exc,
            )

    async def _input_watchdog(
        self,
        session_id: str,
        submit_ms: int,
        submit_output_seq: int,
        *,
        submit_kind: Optional[str] = None,
        ack_after_s: float = 0.5,
        stall_after_s: float = 5.0,
    ) -> None:
        """Background task: emit pty_input_acknowledged / pty_input_stalled.

        Fires once per submit, lives at most ``stall_after_s`` seconds.
        Cancelled by a subsequent submit on the same session — the
        latest user input is the one we report on.
        """
        try:
            await asyncio.sleep(ack_after_s)
            managed = self._channels.get(session_id)
            if managed is None:
                return
            if managed.channel.last_pty_output_seq() > submit_output_seq:
                self._bus.publish(
                    {
                        "type": "pty_input_acknowledged",
                        "session_id": session_id,
                    }
                )
                if submit_kind == "slash":
                    managed.channel._publish_submit_completed(
                        kind="slash",
                        status="acknowledged",
                        input_row_id=None,
                        response_row_id=None,
                    )
                return
            # No acknowledgement yet — wait the rest of the stall window.
            await asyncio.sleep(stall_after_s - ack_after_s)
            managed = self._channels.get(session_id)
            if managed is None:
                return
            if managed.channel.last_pty_output_seq() <= submit_output_seq:
                self._bus.publish(
                    {
                        "type": "pty_input_stalled",
                        "session_id": session_id,
                        "elapsed_ms": _now_ms() - submit_ms,
                    }
                )
        except asyncio.CancelledError:
            # Newer submit superseded this watchdog — silent exit.
            pass
        except Exception as exc:  # never raise from a background task
            _log.warning(
                "pty: input watchdog crashed (session %s): %s",
                session_id,
                exc,
            )

    async def switch_model(
        self,
        session_id: str,
        model: str,
        *,
        settle_s: float = 0.3,
    ) -> bool:
        """Switch the live TUI's model via claude's ``/model`` slash command.

        Keeps the TUI alive — no kill, no respawn, no JSONL re-read. The slash
        command produces a ``user`` record in JSONL containing
        ``<command-name>/model</command-name>`` plus a ``local-command-stdout``
        confirmation ("Set model to <Name>"); subsequent turns use the new
        model. Verified end-to-end against ``crad`` v2.1.143.

        Returns True if the command was written. False if no live channel
        exists (caller should fall through to the auto-focus path).

        ``settle_s`` is a brief wait that lets the TUI process the slash
        command before the next user message is written. 300ms is enough in
        practice; we don't poll for a specific marker because the slash
        command's local-stdout shows up in JSONL only AFTER the TUI flushes,
        which can race with concurrent reads.
        """
        async with self._session_lock(session_id):
            async with self._lock:
                if self._shutdown_started:
                    raise RuntimeError("PtyManager is shutting down")
                managed = self._channels.get(session_id)
                if managed is None or not managed.channel.is_alive():
                    return False
                channel = managed.channel
                # Update cached focus params so status() and any future
                # respawn (e.g. after an idle-kill) reflect the new
                # model.
                new_params = replace(managed.focus_params, model=model)
                managed.focus_params = new_params
                self._last_focus[session_id] = new_params

            # Inside the per-session lock so the /model write can't
            # interleave with a concurrent submit() on the same sid.
            channel.write(f"/model {model}\r".encode("utf-8"))
            if settle_s > 0:
                await asyncio.sleep(settle_s)
        return True

    async def kill(self, session_id: str) -> None:
        """Tear down the channel for ``session_id`` (debug endpoint).

        Also drops the cached focus params: an explicit kill from the
        UI/debugger is "forget this session" — a follow-up submit MUST
        re-focus before sending. Compare with idle-kill, which keeps
        the cache so auto-respawn just works.
        """
        async with self._session_lock(session_id):
            async with self._lock:
                managed = self._channels.pop(session_id, None)
                self._last_focus.pop(session_id, None)
                self._clear_output_coalesce(session_id)
                self._cancel_idle_timers(managed)
            if managed is not None:
                await managed.channel.kill()

    def status(self, session_id: str) -> dict:
        """Snapshot the channel's liveness + timer state.

        Reads under no lock. The returned values are copied scalars, and
        the worst case is observing a channel entry that has just been
        removed from the map. The server's ``/api/pty/status`` consumer
        is read-only and idempotent on stale answers.
        """
        managed = self._channels.get(session_id)
        if managed is None:
            return {
                "alive": False,
                "last_activity_ms": 0,
                "last_input_ms": 0,
                "last_pty_output_ms": 0,
                "idle_warn_at_ms": 0,
                "idle_kill_at_ms": 0,
                "model": "",
                "permission_mode": "",
            }
        ch = managed.channel
        return {
            "alive": ch.is_alive(),
            "last_activity_ms": ch.last_activity_ms(),
            "last_input_ms": ch.last_input_ms(),
            "last_pty_output_ms": ch.last_pty_output_ms(),
            "idle_warn_at_ms": managed.idle_warn_at_ms,
            "idle_kill_at_ms": managed.idle_kill_at_ms,
            # Expose the current channel model + permission mode so callers
            # can detect picker drift and request a safe switch on submit.
            "model": managed.focus_params.model,
            "permission_mode": managed.focus_params.permission_mode,
        }

    def ownership(
        self,
        session_id: str,
        jsonl_path: Optional[Path],
    ) -> dict:
        """Report who owns ``session_id``'s JSONL, without spawning.

        Returns:
          - ``status="ours"`` — a live PtyChannel for this sid exists here.
          - ``status="terminal"`` — another process holds the JSONL open.
          - ``status="idle"`` — no one (we don't have a channel and no
            foreign signal fires).

        Used by ``GET /api/pty/ownership/{sid}`` to drive the FE badge
        without triggering ``focus()`` (which would spawn or refuse).
        Returns a ``foreign_owner`` dict when a Phase-1 lock sidecar
        names the foreign owner (kind / pid / hostname / ui_endpoint /
        heartbeat_at) so the FE tooltip can render structured detail
        instead of just a pid number.
        """
        pids = _session_conflict_pids(session_id, jsonl_path)
        sc = _fresh_foreign_sidecar(jsonl_path)
        foreign_owner: Optional[dict] = None
        if sc is not None:
            foreign_owner = {
                "kind": sc.owner_kind,
                "pid": sc.pid,
                "hostname": sc.hostname,
                "ui_endpoint": sc.ui_endpoint,
                "heartbeat_at": sc.heartbeat_at.isoformat(),
            }
        managed = self._channels.get(session_id)
        if (
            managed is not None
            and managed.channel.is_alive()
            and not pids
            and foreign_owner is None
        ):
            return {
                "status": "ours",
                "foreign_pids": [],
                "foreign_owner": None,
                "jsonl_path": str(jsonl_path) if jsonl_path else None,
            }
        return {
            "status": "terminal" if (pids or foreign_owner is not None) else "idle",
            "foreign_pids": pids,
            "foreign_owner": foreign_owner,
            "jsonl_path": str(jsonl_path) if jsonl_path else None,
        }

    async def shutdown(self) -> None:
        """Tear down every live channel. Idempotent."""
        async with self._lock:
            if self._shutdown_started:
                return
            self._shutdown_started = True
            items = list(self._channels.items())
            self._channels.clear()
            self._last_focus.clear()
            for _, managed in items:
                self._cancel_idle_timers(managed)
            # Cancel pending input watchdogs so shutdown doesn't leak tasks.
            for task in self._input_watchdogs.values():
                if not task.done():
                    task.cancel()
            self._input_watchdogs.clear()

            # Cancel pending /btw capture tasks on all channels.
            for _, managed in items:
                ch = managed.channel
                for task_attr in ("_btw_finalize_task", "_btw_timeout_task"):
                    t = getattr(ch, task_attr, None)
                    if t is not None and not t.done():
                        t.cancel()

        for session_id, managed in items:
            try:
                await managed.channel.kill()
            except Exception as exc:
                _log.warning(
                    "pty: shutdown kill raised (session %s): %s",
                    session_id,
                    exc,
                )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _ensure_channel_locked(
        self,
        session_id: str,
        params: _FocusParams,
    ) -> _ManagedChannel:
        """Spawn a channel if none alive. Caller must hold ``self._lock``."""
        managed = self._channels.get(session_id)
        if managed is not None and managed.channel.is_alive():
            return managed

        # Replace any dead channel record before spawning a new one.
        if managed is not None:
            self._cancel_idle_timers(managed)
            # Best-effort cleanup of the dead channel's fds — kill() is
            # idempotent and safe to call after the drain has already
            # marked it dead.
            try:
                await managed.channel.kill()
            except Exception as exc:
                _log.warning(
                    "pty: cleanup of dead channel raised (session %s): %s",
                    session_id,
                    exc,
                )
            self._channels.pop(session_id, None)
            self._clear_output_coalesce(session_id)

        argv = _build_argv(
            bin_name=params.bin_name,
            session_id=session_id,
            model=params.model,
            permission_mode=params.permission_mode,
            new_chat=params.new_chat,
        )
        env = await _pty_env(params.rows, self._native_cols, params.bin_name)
        channel = PtyChannel(
            session_id=session_id,
            argv=argv,
            cwd=params.cwd,
            env=env,
            rows=params.rows,
            cols=self._native_cols,
            on_chunk=self._scan_chunk_for_hitl,
            on_dead=self._publish_native_dead_state,
            jsonl_path=params.jsonl_path,
            ui_endpoint=self._ui_endpoint,
            db=self._db,
            bus=self._bus,
        )
        try:
            await channel.start()
        except LockAlreadyHeld as exc:
            # Lock race lost between focus()'s pre-spawn detector and
            # the actual acquire. Promote to PtyOwnershipConflict so the
            # HTTP layer returns 409 with the foreign pid.
            sc = exc.existing
            foreign = [sc.pid] if sc.hostname == socket.gethostname() else []
            _log.info(
                "pty: lock race lost (session %s) — held by %s@%s pid %d",
                session_id,
                sc.owner_kind,
                sc.hostname,
                sc.pid,
            )
            raise PtyOwnershipConflict(foreign)
        except Exception as exc:
            _log.warning(
                "pty: spawn failed (session %s, argv=%s): %s",
                session_id,
                " ".join(argv),
                exc,
            )
            raise

        # Wait for the TUI to mount its input handler before letting the
        # caller proceed. Without this, a submit that piggy-backs onto the
        # focus call (e.g. model-switch respawn during ``pty_submit``)
        # writes bytes into a bootstrapping TUI and they get silently
        # dropped. Phase 0 verified the bracketed-paste enable escape
        # ``\\x1b[?2004h`` reliably signals "ready" for claude v2.1.143.
        ready = await channel.await_ready(timeout_s=5.0)
        if not ready:
            _log.warning(
                "pty: TUI did not signal ready within 5s (session %s); "
                "proceeding anyway — first write may race bootstrap",
                session_id,
            )

        managed = _ManagedChannel(channel=channel, focus_params=params)
        self._channels[session_id] = managed
        return managed

    def _encode_submit(self, content: str) -> bytes:
        """Translate a content string into PTY-submit bytes.

        In-message newlines become Ctrl+J (0x0a); a trailing CR (0x0d)
        terminates the message per Phase 0. UTF-8 errors are tolerated
        because the input has already gone through JSON encode/decode at
        the HTTP layer.
        """
        # In-message newlines submit as Ctrl+J (0x0a). Python's str
        # already encodes "\n" as 0x0a in UTF-8, so direct encoding is
        # equivalent — but we surface the intent explicitly via the
        # _INLINE_NEWLINE constant so the Phase 0 contract is greppable.
        body = content.encode("utf-8", errors="replace").replace(b"\n", _INLINE_NEWLINE)
        # Strip any user-provided trailing CR/LF so we don't double-submit.
        body = body.rstrip(b"\r\n")
        return body + _SUBMIT_BYTE

    # --- idle timer ---------------------------------------------------

    def _reset_idle_timer_locked(self, session_id: str) -> None:
        """(Re)schedule warn + kill callbacks for ``session_id``.

        Caller must hold ``self._lock``. Cancelling a TimerHandle is
        cheap and safe even if it already fired, so we don't guard on
        existence.
        """
        managed = self._channels.get(session_id)
        if managed is None:
            return
        self._cancel_idle_timers(managed)

        loop = asyncio.get_running_loop()
        now_ms = _now_ms()
        managed.idle_warn_at_ms = now_ms + int(self._idle_warn_s * 1000)
        managed.idle_kill_at_ms = now_ms + int(self._idle_timeout_s * 1000)
        managed.idle_warn_handle = loop.call_later(
            self._idle_warn_s, self._on_idle_warn, session_id
        )
        managed.idle_kill_handle = loop.call_later(
            self._idle_timeout_s, self._on_idle_kill, session_id
        )

    def _cancel_idle_timers(self, managed: Optional[_ManagedChannel]) -> None:
        if managed is None:
            return
        for handle_attr in ("idle_warn_handle", "idle_kill_handle"):
            handle = getattr(managed, handle_attr)
            if handle is not None:
                try:
                    handle.cancel()
                except Exception:
                    pass
                setattr(managed, handle_attr, None)

    def _on_idle_warn(self, session_id: str) -> None:
        """Fired at T+idle_warn_s. SSE-only; doesn't touch the channel."""
        managed = self._channels.get(session_id)
        if managed is None or not managed.channel.is_alive():
            return
        kill_in = int(self._idle_timeout_s - self._idle_warn_s)
        event: dict[str, Any] = {
            "type": "pty_idle_warn",
            "session_id": session_id,
            "kill_in_seconds": kill_in,
        }
        try:
            self._bus.publish(event)
        except Exception as exc:
            _log.warning(
                "pty: idle_warn publish raised (session %s): %s",
                session_id,
                exc,
            )

    def _on_idle_kill(self, session_id: str) -> None:
        """Fired at T+idle_timeout_s. Schedules the actual kill coroutine.

        ``loop.call_later`` callbacks are synchronous, so we hand the
        async kill off via ``create_task``. We pop the channel here
        synchronously so a follow-up ``submit()`` sees no live channel
        and triggers auto-respawn via ``focus()``.

        Phase 2: if a /btw capture is mid-flight
        (``expecting_btw_response=True``), defer the idle-kill by one
        full ``idle_timeout_s`` window.  Killing now would destroy the
        ``btw_buffer`` + the pending ``_btw_timeout_task`` before the
        180 s stuck-modal backstop could persist whatever was captured
        — exactly the Phase 2 live-smoke failure mode.  The backstop
        will reset ``expecting_btw_response`` itself, so a subsequent
        firing will proceed normally.
        """
        managed = self._channels.get(session_id)
        if managed is None:
            return
        # The session currently on screen (being viewed) is never idle-killed —
        # re-arm a fresh lease instead. It only becomes kill-eligible once the
        # user navigates away: unfocus() clears _active_session_id and shortens
        # the timer to the blurred window, so the next firing has
        # session_id != _active_session_id and proceeds with the kill.
        if session_id == self._active_session_id:
            try:
                loop = asyncio.get_running_loop()
                managed.idle_kill_handle = loop.call_later(
                    self._idle_timeout_s, self._on_idle_kill, session_id
                )
            except RuntimeError:
                pass
            return
        if managed.channel._state.expecting_btw_response:
            _log.info(
                "pty: idle-kill deferred — /btw capture in flight (session %s)",
                session_id,
            )
            try:
                loop = asyncio.get_running_loop()
                managed.idle_kill_handle = loop.call_later(
                    self._idle_timeout_s, self._on_idle_kill, session_id
                )
            except RuntimeError:
                pass
            return
        # No /btw in flight — proceed with the actual kill.
        self._channels.pop(session_id, None)
        self._clear_output_coalesce(session_id)
        self._cancel_idle_timers(managed)
        _log.info("pty: idle-killing (session %s)", session_id)

        async def _kill_then_log() -> None:
            try:
                await managed.channel.kill()
            except Exception as exc:
                _log.warning(
                    "pty: idle kill raised (session %s): %s",
                    session_id,
                    exc,
                )

        try:
            asyncio.get_running_loop().create_task(_kill_then_log())
        except RuntimeError:
            # Loop already closed (e.g. during interpreter shutdown).
            # The OS will reap the orphaned child; nothing more to do.
            pass
