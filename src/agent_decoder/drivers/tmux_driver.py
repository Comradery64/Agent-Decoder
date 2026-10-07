"""tmux-backed ``ProviderDriver`` — the v1 POSIX driving backend.

A **generic** CLI driver over an isolated tmux server, parameterised by a
``spawn_command`` (built per-provider). It is NOT Codex-specific — the same
class would drive ``claude`` if the optional convergence ever lands.

Why tmux (validated in the Phase-4 spike, 2026-06-21 against codex 0.137.0):
  * Persistence / idle-survival for free — the tmux session outlives the app
    process and any disconnect, so the 5-minute reaper can no longer kill a
    long-running task. Reattach = re-``attach`` to the same session.
  * Generic across CLIs — no per-provider TUI state machine to rebuild.

Mechanics (each validated in the spike):
  * Isolated server socket ``tmux -L clau-decode`` — never the user's default.
  * One tmux session per driven clau-decode session: ``cd_<sanitised-id>``.
  * Output: a thin ``tmux attach`` client runs inside a Python pty; we stream
    its raw master-fd bytes to the Native transport (same shape as the Claude
    PTY). Multiple clients may attach, so reconnect is just another attach.
  * Input (structured submit): ``load-buffer`` + ``paste-buffer -p`` (bracketed
    paste, so multi-line bodies don't submit early) + a *separate* ``send-keys
    Enter`` after a short settle. The spike proved an immediate text+Enter is
    lossy — the first Enter is eaten — so the settle is load-bearing.
  * Input (raw interactive): write bytes straight to the attached client's pty
    master, which forwards them to the pane (arrows, Esc, Ctrl-C).
  * State: poll ``capture-pane -p`` and match the real Codex TUI markers.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import pty
import re
import shutil
from collections.abc import Callable

from ..pty_runner import (
    DEFAULT_COLS,
    DEFAULT_ROWS,
    OUTPUT_RING_BYTES,
    _DRAIN_CHUNK,
    _now_ms,
    _set_winsize,
)
from .base import DriverAvailability, DriverState, ProviderDriver

_log = logging.getLogger(__name__)

# Isolated tmux server socket — keeps every driven session off the user's
# default tmux server so we never list, resize, or kill their windows.
DEFAULT_SOCKET = "clau-decode"

# Seconds to let the TUI register a bracketed paste before the submitting
# Enter. The spike showed text+Enter sent back-to-back drops the Enter.
SUBMIT_SETTLE_S = 0.4

# tmux session names forbid "." and ":" and must be otherwise shell-safe.
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_-]")

# Device-Attributes *responses* that xterm.js auto-emits when it sees a DA
# query in the output stream (Primary ``ESC[?...c`` / Secondary ``ESC[>...c``;
# xterm's DA2 identity is ``ESC[>0;276;0c``). Over a normal terminal the app
# consumes its own DA reply inline, but through the web bridge the reply
# round-trips too slowly and lands in the pane as literal input (``0;276;0c``
# junk in Codex's composer — one per attach/resize). tmux already answers the
# pane's DA on its behalf, so these client replies are pure cruft: drop them
# from the raw-input path. A real keystroke never contains a DA response.
_TERMINAL_REPORT_RE = re.compile(rb"\x1b\[[?>][0-9;]*c")

# ---------------------------------------------------------------------------
# Codex TUI markers — captured from the real binary in the Phase-4 spike.
# Matched against clean ``capture-pane -p`` (no SGR). Order in capture_state
# matters: blocking dialogs are checked before running/idle.
# ---------------------------------------------------------------------------
_MARKER_TRUST = "Do you trust the contents of this directory?"
_MARKER_APPROVAL = "Would you like to run the following command?"
_MARKER_UPDATE = "Update available!"
_MARKER_RUNNING = "esc to interrupt"
# Best-effort login markers — the spike machine was ChatGPT-authed so these
# could not be captured live; kept conservative to avoid false positives.
_MARKERS_LOGIN = ("Sign in with ChatGPT", "Sign in to use Codex", "Not logged in")


def codex_spawn_builder(
    *,
    resume_uuid: str | None = None,
    model: str | None = None,
    sandbox: str | None = None,
) -> list[str]:
    """Build the ``codex`` argv for ``TmuxDriver``.

    The v1 happy path is *resume*: ``codex resume <uuid>`` where ``<uuid>`` is
    exactly the value ``CodexAdapter`` stores as ``Session.id`` (the rollout's
    ``session_meta.payload.id``), so no id translation is needed. Omitting
    ``resume_uuid`` yields a fresh interactive session.

    ``sandbox`` is left ``None`` by default so codex uses the user's own
    ``config.toml`` policy — driving behaves exactly like the user's normal
    ``codex resume`` from a terminal.
    """
    argv = ["codex"]
    if resume_uuid:
        argv += ["resume", resume_uuid]
    if model:
        argv += ["--model", model]
    if sandbox:
        argv += ["--sandbox", sandbox]
    return argv


class TmuxDriver(ProviderDriver):
    """Drive a CLI inside an isolated tmux server; bridge its pane to bytes."""

    def __init__(
        self,
        session_id: str,
        cwd: str,
        spawn_command: list[str],
        *,
        socket_name: str = DEFAULT_SOCKET,
        rows: int = DEFAULT_ROWS,
        cols: int = DEFAULT_COLS,
        on_chunk: Callable[[bytes], None] | None = None,
        on_dead: Callable[[], None] | None = None,
    ) -> None:
        self.session_id = session_id
        self._cwd = cwd
        self._spawn_command = list(spawn_command)
        self._socket = socket_name
        self._rows = rows
        self._cols = cols
        self._on_chunk = on_chunk
        self._on_dead = on_dead

        # tmux session + buffer names, sanitised so the UUID's dashes survive
        # but nothing shell-hostile slips through.
        safe = _SAFE_NAME.sub("_", session_id)
        self._tmux_session = f"cd_{safe}"
        self._buffer = f"cd_{safe}"

        # Attached-client pty (our output window into the pane).
        self._master_fd = -1
        self._attach_proc: asyncio.subprocess.Process | None = None
        self._reader_registered = False
        self._ring = bytearray()
        self._ring_complete = True
        self._last_output_ms = 0
        self._dead = False

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    @classmethod
    def availability(cls) -> DriverAvailability:
        """True iff a ``tmux`` binary is on PATH (the only backend requirement).

        Provider-binary presence (e.g. ``codex``) is checked by the drivers
        registry, which composes it with this probe.
        """
        if shutil.which("tmux") is None:
            return DriverAvailability(
                available=False,
                reason="live driving unavailable: tmux not found on PATH",
            )
        return DriverAvailability(available=True, reason=None)

    # ------------------------------------------------------------------
    # tmux helpers
    # ------------------------------------------------------------------

    def _tmux_argv(self, *args: str) -> list[str]:
        return ["tmux", "-L", self._socket, *args]

    async def _tmux(self, *args: str, stdin: bytes | None = None) -> tuple[int, bytes]:
        """Run a one-shot tmux control command on our isolated server."""
        proc = await asyncio.create_subprocess_exec(
            *self._tmux_argv(*args),
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate(input=stdin)
        return proc.returncode or 0, out

    async def has_session(self) -> bool:
        """Authoritative async check: does the tmux session still exist?"""
        rc, _ = await self._tmux("has-session", "-t", self._tmux_session)
        return rc == 0

    async def rename(self, new_session_id: str) -> None:
        """Re-label this driver's tmux session in place (no process restart).

        Used to adopt the real Codex rollout UUID once codex creates it on the
        first message: the fresh-spawned ``codex`` keeps running and the attach
        client / fd stay live — only the identity (tmux session + buffer name,
        ``session_id``) flips from the placeholder to the real id.
        """
        safe = _SAFE_NAME.sub("_", new_session_id)
        new_tmux = f"cd_{safe}"
        if new_tmux != self._tmux_session:
            await self._tmux("rename-session", "-t", self._tmux_session, new_tmux)
            self._tmux_session = new_tmux
            self._buffer = new_tmux
        self.session_id = new_session_id

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def spawn(self, *, cols: int, rows: int) -> None:
        if self._attach_proc is not None:
            raise RuntimeError(f"driver for {self.session_id} already spawned")
        self._cols, self._rows = cols, rows

        # Create the session detached at the requested size. ``--`` ends tmux
        # option parsing; the spawn command is passed as already-split argv so
        # tmux execs it directly (no shell, no quoting hazards).
        rc, out = await self._tmux(
            "new-session",
            "-d",
            "-s",
            self._tmux_session,
            "-x",
            str(cols),
            "-y",
            str(rows),
            "-c",
            self._cwd,
            "--",
            *self._spawn_command,
        )
        if rc != 0:
            raise RuntimeError(
                f"tmux new-session failed for {self.session_id}: "
                f"{out.decode('utf-8', 'replace').strip()}"
            )
        # Pin the size so a transient attach client can't shrink the pane.
        await self._tmux(
            "set-option", "-t", self._tmux_session, "window-size", "manual"
        )
        await self.attach()

    async def attach(self) -> None:
        """Attach an output client inside a pty and start draining its bytes.

        Used both by ``spawn`` and on reconnect to an already-live session.
        """
        if self._attach_proc is not None and self._attach_proc.returncode is None:
            return  # already attached
        if not await self.has_session():
            raise RuntimeError(f"cannot attach: no tmux session for {self.session_id}")

        # Hide tmux's own status bar — the Native view shows only the driven
        # TUI, never tmux chrome. Set here (not just at spawn) so reconnecting
        # to a session created before this fix also drops the bar. Idempotent.
        await self._tmux("set-option", "-t", self._tmux_session, "status", "off")

        master_fd, slave_fd = pty.openpty()
        try:
            _set_winsize(slave_fd, self._rows, self._cols)
            os.set_blocking(master_fd, False)
            proc = await asyncio.create_subprocess_exec(
                *self._tmux_argv("attach-session", "-t", self._tmux_session),
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
            )
        except BaseException:
            for fd in (master_fd, slave_fd):
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise
        try:
            os.close(slave_fd)
        except OSError:
            pass

        self._master_fd = master_fd
        self._attach_proc = proc
        self._dead = False
        loop = asyncio.get_running_loop()
        loop.add_reader(master_fd, self._on_readable)
        self._reader_registered = True

    async def kill(self) -> None:
        """Tear down the tmux session and the attach client. Idempotent."""
        # Kill the session first so the attach client sees EOF and exits.
        try:
            await self._tmux("kill-session", "-t", self._tmux_session)
        except Exception as exc:  # pragma: no cover — defensive
            _log.debug("tmux kill-session raised (%s): %s", self.session_id, exc)
        self._detach_reader()
        proc = self._attach_proc
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except (ProcessLookupError, asyncio.TimeoutError):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
        self._close_master()
        self._attach_proc = None
        self._dead = True

    def is_alive(self) -> bool:
        """Cheap sync liveness — is our attach client still running?

        Reflects *our window* into the session, not the session itself (use
        ``has_session`` for that). False once the client EOFs or we kill it.
        """
        proc = self._attach_proc
        return proc is not None and proc.returncode is None and not self._dead

    # ------------------------------------------------------------------
    # Input
    # ------------------------------------------------------------------

    async def send_text(self, text: str) -> None:
        """Submit ``text`` as one composer message (bracketed paste + Enter)."""
        if not await self.has_session():
            raise RuntimeError(f"cannot send: no tmux session for {self.session_id}")
        # Load the body into a named buffer, then bracketed-paste it so a
        # multi-line body lands in the composer without an embedded newline
        # triggering an early submit. ``-d`` deletes the buffer after paste.
        await self._tmux("load-buffer", "-b", self._buffer, "-", stdin=text.encode())
        await self._tmux(
            "paste-buffer", "-p", "-d", "-b", self._buffer, "-t", self._tmux_session
        )
        await asyncio.sleep(SUBMIT_SETTLE_S)
        await self._tmux("send-keys", "-t", self._tmux_session, "Enter")

    async def write_input(self, data: bytes) -> None:
        """Forward raw bytes to the pane via the attached client's pty.

        Strips xterm's spurious Device-Attributes replies first (see
        ``_TERMINAL_REPORT_RE``) so they don't corrupt the pane's input line.
        """
        if self._dead or self._master_fd < 0:
            raise RuntimeError(f"driver for {self.session_id} is not attached")
        data = _TERMINAL_REPORT_RE.sub(b"", data)
        if not data:
            return
        try:
            view = memoryview(data)
            while view:
                n = os.write(self._master_fd, view)
                if n <= 0:
                    break
                view = view[n:]
        except (BlockingIOError, InterruptedError):
            raise
        except OSError as exc:
            _log.warning("tmux driver: raw write failed (%s): %s", self.session_id, exc)

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    def output_snapshot(self) -> bytes:
        return bytes(self._ring)

    def output_snapshot_complete(self) -> bool:
        return self._ring_complete

    def last_output_ms(self) -> int:
        return self._last_output_ms

    def dimensions(self) -> tuple[int, int]:
        """Return the driven pane's ``(rows, cols)``."""
        return self._rows, self._cols

    def set_on_chunk(self, cb: Callable[[bytes], None] | None) -> None:
        self._on_chunk = cb

    def _on_readable(self) -> None:
        """``add_reader`` callback — drain pane bytes into the bounded ring.

        Mirrors ``PtyChannel._on_readable``: must never raise, treats EOF/EIO
        as terminal, trims the ring to ``OUTPUT_RING_BYTES``.
        """
        fd = self._master_fd
        if fd < 0:
            return
        try:
            chunk = os.read(fd, _DRAIN_CHUNK)
        except (BlockingIOError, InterruptedError):
            return
        except OSError as exc:
            if exc.errno not in (errno.EIO, errno.EBADF):
                _log.warning("tmux driver: read error (%s): %s", self.session_id, exc)
            self._mark_dead_from_drain()
            return
        except Exception as exc:  # pragma: no cover — belt and suspenders
            _log.warning("tmux driver: drain error (%s): %s", self.session_id, exc)
            self._mark_dead_from_drain()
            return

        if not chunk:
            self._mark_dead_from_drain()
            return

        self._ring.extend(chunk)
        overflow = len(self._ring) - OUTPUT_RING_BYTES
        if overflow > 0:
            del self._ring[:overflow]
            self._ring_complete = False
        self._last_output_ms = _now_ms()

        if self._on_chunk is not None:
            try:
                self._on_chunk(chunk)
            except Exception as exc:  # pragma: no cover — defensive
                _log.warning(
                    "tmux driver: on_chunk hook raised (%s): %s", self.session_id, exc
                )

    def _mark_dead_from_drain(self) -> None:
        self._detach_reader()
        if self._dead:
            return
        self._dead = True
        if self._on_dead is not None:
            try:
                self._on_dead()
            except Exception as exc:  # pragma: no cover — defensive
                _log.warning(
                    "tmux driver: on_dead hook raised (%s): %s", self.session_id, exc
                )

    def _detach_reader(self) -> None:
        if self._reader_registered and self._master_fd >= 0:
            try:
                asyncio.get_running_loop().remove_reader(self._master_fd)
            except (RuntimeError, ValueError):
                pass
            self._reader_registered = False

    def _close_master(self) -> None:
        if self._master_fd >= 0:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = -1

    # ------------------------------------------------------------------
    # Sizing & state
    # ------------------------------------------------------------------

    async def resize(self, *, cols: int, rows: int) -> None:
        self._cols, self._rows = cols, rows
        if self._master_fd >= 0:
            try:
                _set_winsize(self._master_fd, rows, cols)
            except OSError as exc:  # pragma: no cover — defensive
                _log.debug("tmux driver: winsize failed (%s): %s", self.session_id, exc)
        # window-size manual means the pane follows resize-window, not the
        # client, so set it explicitly.
        await self._tmux(
            "resize-window", "-t", self._tmux_session, "-x", str(cols), "-y", str(rows)
        )

    async def capture_state(self) -> DriverState:
        """Scrape the pane and classify it. Blocking dialogs win over run/idle."""
        rc, out = await self._tmux("capture-pane", "-p", "-t", self._tmux_session)
        if rc != 0:
            return DriverState.DEAD
        text = out.decode("utf-8", "replace")
        if _MARKER_TRUST in text:
            return DriverState.NEEDS_TRUST
        if _MARKER_APPROVAL in text:
            return DriverState.NEEDS_APPROVAL
        if any(m in text for m in _MARKERS_LOGIN):
            return DriverState.NEEDS_LOGIN
        if _MARKER_UPDATE in text:
            return DriverState.NEEDS_UPDATE
        if _MARKER_RUNNING in text:
            return DriverState.RUNNING
        if not text.strip():
            return DriverState.STARTING
        return DriverState.IDLE
