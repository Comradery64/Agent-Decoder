"""Codex execution policy used by the shared ExecutionManager.

This mixin owns no independent manager lifecycle or locks. The shared owner
initializes the provider records and dispatches operations; Claude policy
continues to live in pty_runner to preserve its acknowledgment/capture hooks.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .drivers import DriverState, ProviderDriver, build_driver
from .pty_native import encode_pty_output_chunk, encode_pty_snapshot
from .pty_runner import DEFAULT_ROWS

_log = logging.getLogger(__name__)


# How often the background state poller scrapes the active driver's screen.
# Only the ONE active session is polled, so this is ~60 tmux capture-pane
# forks/min at worst (~2–8 ms each) — cheap relative to the SSE round-trip.
# Codex's TUI doesn't fire an output chunk when it lands on an approval prompt
# it drew on its own (it repaints in place), so unlike Claude we can't lean on
# the output fan-out to reclassify state: we have to poll. See
# docs/native-input-required-plan.md (Part A).
STATE_POLL_INTERVAL_S = 1.0


# DriverState → the native_state vocabulary the FE already understands
# (see pty_screen_state.classify_screen). Keeps the Native view's existing
# state handling working for Codex with zero FE changes.
_STATE_TO_NATIVE: dict[DriverState, str] = {
    DriverState.IDLE: "idle_chat_input",
    DriverState.RUNNING: "running",
    DriverState.NEEDS_TRUST: "trust_prompt",
    DriverState.NEEDS_APPROVAL: "permission_prompt",
    DriverState.NEEDS_LOGIN: "login_required",
    DriverState.NEEDS_UPDATE: "permission_prompt",
    DriverState.STARTING: "idle_chat_input",
    DriverState.DEAD: "dead",
}


class DriverSessionPolicy:
    """Provider-specific Codex operations on the shared owner's records.

    Common ownership, locks, focus selection, dispatch and lifecycle live on
    ExecutionManager. These helpers retain Codex's terminal state vocabulary.
    """

    def _init_driver_sessions(self) -> None:
        self._drivers: dict[str, ProviderDriver] = {}
        self._providers: dict[str, str] = {}
        self._last_driver_state: dict[str, tuple[str, bool]] = {}
        self._state_poll_lock = asyncio.Lock()
        self._state_poll_task: asyncio.Task[None] | None = None

    def has(self, session_id: str) -> bool:
        return session_id in self._drivers

    def is_alive(self, session_id: str) -> bool:
        d = self._drivers.get(session_id)
        return d is not None and d.is_alive()

    # ------------------------------------------------------------------
    # State poller
    # ------------------------------------------------------------------

    def _ensure_state_poller(self) -> None:
        """Start the background state poller once (idempotent).

        Without this, codex's blocked state (approval/trust/login prompt) is
        only classified inside ``native_snapshot()`` — which the FE calls only
        when the Native pane mounts. A codex session sitting at an approval
        prompt in Decoded-only is therefore invisible. The poller reuses
        ``capture_state()`` + ``_STATE_TO_NATIVE`` and emits ``pty_native_state``
        on change, gated to the single active session — mirroring how
        ``PtyManager`` already classifies claude on every output chunk.
        """
        if self._state_poll_task is not None and not self._state_poll_task.done():
            return
        self._state_poll_task = asyncio.create_task(self._state_poll_loop())

    async def _state_poll_loop(self) -> None:
        """Poll the active driver's screen and emit ``pty_native_state`` on change.

        Only the active session is polled; a None/missing/dead active driver is
        skipped (death is already emitted by ``_make_on_dead``). Emits are
        deduped on ``(state, decoded_input_safe)`` so a steady prompt emits
        once. The ``capture_state`` call runs under ``_state_poll_lock`` so its
        tmux fork can't overlap itself, but NOT under ``_session_lock`` so it
        can't stall submit/resize.
        """
        while True:
            await asyncio.sleep(STATE_POLL_INTERVAL_S)
            sid = self._active_session_id
            if sid is None:
                continue
            driver = self._drivers.get(sid)
            if driver is None or not driver.is_alive():
                continue
            try:
                async with self._state_poll_lock:
                    state = await driver.capture_state()
                native_state = _STATE_TO_NATIVE.get(state, "idle_chat_input")
                key = (native_state, state == DriverState.IDLE)
                if self._last_driver_state.get(sid) == key:
                    continue
                self._last_driver_state[sid] = key
                self._bus.publish(
                    {
                        "type": "pty_native_state",
                        "session_id": sid,
                        "state": native_state,
                        "decoded_input_safe": key[1],
                    }
                )
            except Exception as exc:  # pragma: no cover — defensive
                _log.warning("driver: state poll raised (session %s): %s", sid, exc)

    # ------------------------------------------------------------------
    # Output fanout
    # ------------------------------------------------------------------

    def _make_on_chunk(self, session_id: str):
        def _cb(chunk: bytes) -> None:
            # Drop output for backgrounded sessions — only the active session's
            # bytes reach the browser.
            if session_id != self._active_session_id:
                return
            try:
                self._bus.publish(encode_pty_output_chunk(session_id, chunk))
            except Exception as exc:  # pragma: no cover — defensive
                _log.warning(
                    "driver: output publish raised (session %s): %s", session_id, exc
                )

        return _cb

    def _make_on_dead(self, session_id: str):
        def _cb() -> None:
            try:
                self._bus.publish(
                    {
                        "type": "pty_native_state",
                        "session_id": session_id,
                        "state": "dead",
                        "decoded_input_safe": False,
                    }
                )
            except Exception as exc:  # pragma: no cover — defensive
                _log.warning(
                    "driver: dead publish raised (session %s): %s", session_id, exc
                )
            # Drop the spent driver so a later focus() rebuilds a fresh one.
            # An out-of-band death (codex /quit, crash, tmux killed) leaves the
            # driver with a closed attach client but a stale _attach_proc; if we
            # keep it in _drivers, focus()'s respawn branch calls spawn() on it
            # and raises "already spawned" (HTTP 500) — the session then stays
            # unreopenable until the server restarts. Guard on is_alive() so a
            # concurrently re-spawned driver for the same id is never evicted.
            driver = self._drivers.get(session_id)
            if driver is not None and not driver.is_alive():
                self._drivers.pop(session_id, None)
                self._providers.pop(session_id, None)
                self._last_driver_state.pop(session_id, None)
                if self._active_session_id == session_id:
                    self._active_session_id = None
                # Release the leaked master fd / reap the dead attach client
                # off the reader callback without terminating a surviving pane.
                try:
                    asyncio.get_running_loop().create_task(driver.detach())
                except RuntimeError:  # pragma: no cover — no running loop
                    pass

        return _cb

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _driver_focus(
        self,
        session_id: str,
        *,
        provider: str,
        cwd: str,
        model: str | None = None,
        resume_uuid: str | None = None,
        new_chat: bool = False,
        rows: int | None = None,
    ) -> None:
        """Ensure a live driver for ``session_id`` and mark it active.

        Spawns on first focus; reattaches if the tmux session outlived the app
        (persistence/reconnect); otherwise re-uses the live driver. ``new_chat``
        spawns the CLI fresh (no resume) — for a brand-new chat whose real
        rollout id the CLI mints itself.
        """
        cols = self._native_cols
        eff_rows = rows if rows is not None else DEFAULT_ROWS
        async with self._session_lock(session_id):
            if self._shutdown_started:
                raise RuntimeError("ExecutionManager is shutting down")
            driver = self._drivers.get(session_id)
            if driver is None:
                driver = build_driver(
                    provider,
                    session_id,
                    cwd,
                    model=model,
                    resume_uuid=resume_uuid,
                    fresh=new_chat,
                    on_chunk=self._make_on_chunk(session_id),
                    on_dead=self._make_on_dead(session_id),
                    rows=eff_rows,
                    cols=cols,
                    socket_name=self._tmux_socket,
                )
                self._drivers[session_id] = driver
                self._providers[session_id] = provider
            if not driver.is_alive():
                # Reattach to a surviving tmux session, else spawn fresh.
                if hasattr(driver, "has_session") and await driver.has_session():
                    await driver.attach()  # type: ignore[attr-defined]
                else:
                    await driver.spawn(cols=cols, rows=eff_rows)
            self._active_session_id = session_id
        # Start (idempotent) the active-session state poller so codex's
        # blocked state is surfaced even when the Native pane never mounts
        # (Decoded-only / Split). No-op if already running.
        self._ensure_state_poller()

    async def _driver_submit(self, session_id: str, content: str) -> None:
        async with self._session_lock(session_id):
            driver = self._drivers.get(session_id)
            if driver is None or not driver.is_alive():
                raise RuntimeError(f"no live driver for session {session_id}")
            await driver.send_text(content)

    async def _driver_write_raw_input(self, session_id: str, data: bytes) -> None:
        driver = self._drivers.get(session_id)
        if driver is None:
            raise RuntimeError(f"no driver for session {session_id}")
        await driver.write_input(data)

    async def _driver_resize(self, session_id: str, rows: int, cols: int) -> None:
        driver = self._drivers.get(session_id)
        if driver is None:
            raise RuntimeError(f"no driver for session {session_id}")
        await driver.resize(cols=cols, rows=rows)

    async def _driver_native_snapshot(self, session_id: str) -> dict[str, Any]:
        driver = self._drivers.get(session_id)
        if driver is None:
            raise RuntimeError(f"no driver for session {session_id}")
        self._active_session_id = session_id
        alive = driver.is_alive()
        if alive:
            state = await driver.capture_state()
        else:
            state = DriverState.DEAD
        native_state = _STATE_TO_NATIVE.get(state, "idle_chat_input")
        rows, cols = driver.dimensions()  # type: ignore[attr-defined]
        return encode_pty_snapshot(
            session_id=session_id,
            ring=driver.output_snapshot(),
            ring_complete=driver.output_snapshot_complete(),  # type: ignore[attr-defined]
            rows=rows,
            cols=cols,
            alive=alive,
            native_state=native_state,
            decoded_input_safe=(state == DriverState.IDLE),
        )

    def _driver_status(self, session_id: str) -> dict[str, Any]:
        driver = self._drivers.get(session_id)
        if driver is None:
            return {"alive": False, "provider": self._providers.get(session_id)}
        return {
            "alive": driver.is_alive(),
            "provider": self._providers.get(session_id),
            "backend": "tmux",
        }

    async def _driver_kill(self, session_id: str) -> None:
        async with self._session_lock(session_id):
            driver = self._drivers.pop(session_id, None)
            self._providers.pop(session_id, None)
            self._last_driver_state.pop(session_id, None)
            if self._active_session_id == session_id:
                self._active_session_id = None
        if driver is not None:
            await driver.kill()

    async def rekey(self, old_session_id: str, new_session_id: str) -> bool:
        """Re-key a live driver from a placeholder id to its real id, in place.

        Adopts the real Codex rollout UUID once codex creates it on the first
        message: the running tmux session is renamed and the driver moves to the
        new key, so a later focus(new_session_id) reuses the SAME live process
        (no respawn, no double-codex-on-one-rollout conflict). Returns True if a
        driver was re-keyed.
        """
        if old_session_id == new_session_id:
            return False
        # Stable lock ordering prevents reciprocal adoption deadlocks.
        first, second = sorted((old_session_id, new_session_id))
        async with self._session_lock(first), self._session_lock(second):
            driver = self._drivers.get(old_session_id)
            if driver is None:
                return False
            if new_session_id in self._drivers or new_session_id in self._channels:
                raise RuntimeError("cannot adopt an already managed session")
            if hasattr(driver, "rename"):
                await driver.rename(new_session_id)
            self._drivers.pop(old_session_id)
            provider = self._providers.pop(old_session_id, None)
            self._last_driver_state.pop(old_session_id, None)
            self._drivers[new_session_id] = driver
            driver.set_on_chunk(self._make_on_chunk(new_session_id))
            driver.set_on_dead(self._make_on_dead(new_session_id))
            if provider is not None:
                self._providers[new_session_id] = provider
            if self._active_session_id == old_session_id:
                self._active_session_id = new_session_id
        return True

    async def _driver_shutdown(self) -> None:
        # Stop the background poller first so it can't capture_state() a
        # driver mid-kill and publish a stale state after shutdown begins.
        task = self._state_poll_task
        self._state_poll_task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # pragma: no cover
                pass
        for session_id in list(self._drivers.keys()):
            try:
                async with self._session_lock(session_id):
                    driver = self._drivers.pop(session_id, None)
                    if driver is not None:
                        await driver.detach()
            except Exception as exc:  # pragma: no cover — defensive
                _log.warning("driver: shutdown detach raised (%s): %s", session_id, exc)


def __getattr__(name: str):
    # Compatibility for extensions/tests importing the former owner. There is
    # only one implementation and one instance in the server.
    if name == "DriverManager":
        from .execution_manager import ExecutionManager

        return ExecutionManager
    raise AttributeError(name)
