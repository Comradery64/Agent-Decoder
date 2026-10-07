"""Phase 4b — DriverManager lifecycle against the deterministic fake CLI.

Gated on a real tmux; uses fake_cli.py (no codex auth/network). Verifies the
manager owns drivers, fans output onto the bus, serves Native snapshots, and —
critically — has NO idle reaper (tmux-backed sessions survive idle).
"""

from __future__ import annotations

import asyncio
import base64
import os
import sys
import uuid

import pytest

from agent_decoder.db import Database
from agent_decoder.driver_manager import DriverManager
from agent_decoder.drivers import DriverState, TmuxDriver
from agent_decoder.events_bus import EventBroadcaster
from agent_decoder import driver_manager as dm_mod

import shutil
import subprocess


def _terminal_supports_clear() -> bool:
    """tmux-backed drivers bridge via a `tmux attach` client that needs a
    terminfo with `clear`. Headless CI has tmux but no usable terminal (TERM
    unset/dumb) → attach fails ("terminal does not support clear"). Probe that
    capability so these integration tests skip cleanly there instead of failing."""
    try:
        return (
            subprocess.run(["tput", "clear"], capture_output=True, timeout=5).returncode
            == 0
        )
    except Exception:
        return False


requires_tmux = pytest.mark.skipif(
    shutil.which("tmux") is None or not _terminal_supports_clear(),
    reason="needs tmux + a terminal with `clear` (TERM); skipped on headless CI",
)

FAKE_CLI = os.path.join(os.path.dirname(__file__), "fake_cli.py")


def _patch_fake_build(monkeypatch):
    """Make DriverManager build TmuxDrivers wired to the fake CLI."""

    def _build(
        provider, session_id, cwd, *, model=None, resume_uuid=None, fresh=False, **kw
    ):
        # `fresh`/`resume_uuid`/`model` only shape the real spawn argv; the fake
        # CLI ignores them. Consume them so they don't leak into TmuxDriver.
        return TmuxDriver(
            session_id,
            cwd,
            [sys.executable, FAKE_CLI],
            socket_name=f"clau-decode-dm-{uuid.uuid4().hex[:8]}",
            **kw,
        )

    monkeypatch.setattr(dm_mod, "build_driver", _build)


async def _make_manager(tmp_path) -> tuple[DriverManager, EventBroadcaster, Database]:
    db = await Database(tmp_path / "dm.db").__aenter__()
    await db.init_schema()
    bus = EventBroadcaster()
    return DriverManager(db, bus), bus, db


class _StubDriver:
    """Minimal driver stand-in for poller unit tests.

    The poller only touches ``is_alive()`` + ``capture_state()``; this stub
    scripts the returned state so we can exercise the poll loop deterministically
    without a real tmux capture-pane (and without the fake CLI, which never
    lands on an approval marker). ``capture_calls`` lets a test assert the loop
    kept ticking through a deduped steady state.
    """

    def __init__(self, state: DriverState) -> None:
        self.state = state
        self.capture_calls = 0

    def is_alive(self) -> bool:
        return True

    async def capture_state(self) -> DriverState:
        self.capture_calls += 1
        return self.state


async def _wait_for_native_state(q, session_id: str, *, timeout: float = 2.0):
    """Drain ``q`` until a ``pty_native_state`` event for ``session_id`` lands."""
    deadline = _now_loops() + timeout
    while _now_loops() < deadline:
        try:
            ev = q.get_nowait()
        except asyncio.QueueEmpty:
            await asyncio.sleep(0.02)
            continue
        if ev.get("type") == "pty_native_state" and ev.get("session_id") == session_id:
            return ev
    return None


def _drain(q) -> int:
    """Empty the queue now; returns how many native-state events were dropped."""
    dropped = 0
    while True:
        try:
            ev = q.get_nowait()
        except asyncio.QueueEmpty:
            break
        if ev.get("type") == "pty_native_state":
            dropped += 1
    return dropped


def _now_loops() -> float:
    return asyncio.get_event_loop().time()


@requires_tmux
async def test_focus_spawns_and_tracks(tmp_path, monkeypatch):
    _patch_fake_build(monkeypatch)
    dm, _bus, db = await _make_manager(tmp_path)
    sid = "codex-1"
    try:
        await dm.focus(sid, provider="codex", cwd=os.getcwd())
        assert dm.has(sid)
        assert dm.is_alive(sid)
        assert dm.status(sid)["alive"] is True
        assert dm.status(sid)["backend"] == "tmux"
    finally:
        await dm.shutdown()
        await db.__aexit__(None, None, None)
    assert not dm.is_alive(sid)


@requires_tmux
async def test_native_snapshot_carries_ring_and_state(tmp_path, monkeypatch):
    _patch_fake_build(monkeypatch)
    dm, _bus, db = await _make_manager(tmp_path)
    sid = "codex-2"
    try:
        await dm.focus(sid, provider="codex", cwd=os.getcwd())
        snap = None
        for _ in range(40):
            snap = await dm.native_snapshot(sid)
            ring = base64.b64decode(snap["ring_b64"])
            if b"Context 100% left" in ring:
                break
            await asyncio.sleep(0.1)
        assert snap is not None
        assert snap["alive"] is True
        assert b"Context 100% left" in base64.b64decode(snap["ring_b64"])
        # Idle composer → native_state maps to the FE's idle vocabulary.
        assert snap["native_state"] in ("idle_chat_input", "running")
    finally:
        await dm.shutdown()
        await db.__aexit__(None, None, None)


@requires_tmux
async def test_output_fans_out_to_bus_for_active_session(tmp_path, monkeypatch):
    _patch_fake_build(monkeypatch)
    dm, bus, db = await _make_manager(tmp_path)
    q = bus.subscribe()
    sid = "codex-3"
    try:
        await dm.focus(sid, provider="codex", cwd=os.getcwd())
        got_chunk = False
        for _ in range(40):
            try:
                ev = q.get_nowait()
            except asyncio.QueueEmpty:
                await asyncio.sleep(0.1)
                continue
            if ev.get("type") == "pty_output_chunk" and ev.get("session_id") == sid:
                got_chunk = True
                break
        assert got_chunk, "expected pty_output_chunk on the bus for active session"
    finally:
        bus.unsubscribe(q)
        await dm.shutdown()
        await db.__aexit__(None, None, None)


@requires_tmux
async def test_submit_drives_the_session(tmp_path, monkeypatch):
    _patch_fake_build(monkeypatch)
    dm, _bus, db = await _make_manager(tmp_path)
    sid = "codex-4"
    try:
        await dm.focus(sid, provider="codex", cwd=os.getcwd())
        # Let the fake reach idle, then submit; the fake echoes "reply: <text>".
        await asyncio.sleep(1.0)
        await dm.submit(sid, "ping123")
        echoed = False
        for _ in range(60):
            snap = await dm.native_snapshot(sid)
            if b"reply: ping123" in base64.b64decode(snap["ring_b64"]):
                echoed = True
                break
            await asyncio.sleep(0.1)
        assert echoed
    finally:
        await dm.shutdown()
        await db.__aexit__(None, None, None)


@requires_tmux
async def test_no_idle_reaper_session_survives_idle(tmp_path, monkeypatch):
    """The whole point of the tmux backend: a focused-then-idle session is NOT
    killed. DriverManager has no idle timer, so it must still be alive after a
    quiet period far longer than the old 5-minute Claude reaper would model."""
    _patch_fake_build(monkeypatch)
    dm, _bus, db = await _make_manager(tmp_path)
    sid = "codex-5"
    try:
        await dm.focus(sid, provider="codex", cwd=os.getcwd())
        assert dm.is_alive(sid)
        await asyncio.sleep(1.5)  # idle; nothing should reap it
        assert dm.is_alive(sid), "driver session was killed while idle — regression"
        # No idle-timer bookkeeping exists on the manager at all.
        assert not hasattr(dm, "_idle_kill_handle")
        assert not hasattr(dm, "_on_idle_kill")
    finally:
        await dm.shutdown()
        await db.__aexit__(None, None, None)


@requires_tmux
async def test_dead_driver_is_dropped_so_session_can_reopen(tmp_path, monkeypatch):
    """Regression (Phase 4f): when a driver's attach client dies out-of-band
    (codex /quit, crash, tmux killed) — NOT via dm.kill — the manager must drop
    the spent driver so a later focus() rebuilds a fresh one. Before the fix,
    on_dead left the dead driver in _drivers and focus() called spawn() on it,
    raising 'already spawned' (HTTP 500), leaving the session unreopenable until
    a server restart."""
    _patch_fake_build(monkeypatch)
    dm, _bus, db = await _make_manager(tmp_path)
    sid = "codex-dead-reopen"
    try:
        await dm.focus(sid, provider="codex", cwd=os.getcwd())
        assert dm.is_alive(sid)
        driver = dm._drivers[sid]
        # Out-of-band death: kill the tmux session so the attach client EOFs and
        # the driver's on_dead callback fires.
        await driver._tmux("kill-session", "-t", driver._tmux_session)
        # Let the reader callback observe EOF and run on_dead → drop the driver.
        for _ in range(50):
            if not dm.has(sid):
                break
            await asyncio.sleep(0.1)
        assert not dm.has(sid), "spent driver was not dropped after out-of-band death"
        # Reopening must succeed (rebuild + respawn), not raise 'already spawned'.
        await dm.focus(sid, provider="codex", cwd=os.getcwd())
        assert dm.is_alive(sid)
    finally:
        await dm.shutdown()
        await db.__aexit__(None, None, None)


@requires_tmux
async def test_rekey_relabels_live_driver_in_place(tmp_path, monkeypatch):
    """Adoption: a brand-new Codex chat is driven under a placeholder id, then
    re-keyed to the real rollout id once codex creates it. rekey() must move the
    SAME live driver to the new id (tmux renamed, no respawn) so focus(new) reuses
    it."""
    _patch_fake_build(monkeypatch)
    dm, _bus, db = await _make_manager(tmp_path)
    placeholder = "codex-placeholder"
    real = "019eaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    try:
        await dm.focus(placeholder, provider="codex", cwd=os.getcwd())
        assert dm.is_alive(placeholder)
        driver = dm._drivers[placeholder]

        ok = await dm.rekey(placeholder, real)
        assert ok is True
        assert not dm.has(placeholder)
        assert dm.has(real)
        assert dm.is_alive(real)
        # Same driver object, now under the real id with a renamed tmux session.
        assert dm._drivers[real] is driver
        assert driver.session_id == real
        assert await driver.has_session()  # the renamed tmux session exists

        # Re-keying a missing id is a no-op (returns False).
        assert await dm.rekey("nope", "whatever") is False
    finally:
        await dm.kill(real)
        await dm.shutdown()
        await db.__aexit__(None, None, None)


@requires_tmux
async def test_kill_removes_tracking(tmp_path, monkeypatch):
    _patch_fake_build(monkeypatch)
    dm, _bus, db = await _make_manager(tmp_path)
    sid = "codex-6"
    try:
        await dm.focus(sid, provider="codex", cwd=os.getcwd())
        await dm.kill(sid)
        assert not dm.has(sid)
        assert not dm.is_alive(sid)
        # Double kill is safe.
        await dm.kill(sid)
    finally:
        await dm.shutdown()
        await db.__aexit__(None, None, None)


# ---------------------------------------------------------------------------
# State poller (native-input-required-plan.md, Part A)
#
# These are pure unit tests of the poll loop — no tmux, no native_snapshot()
# call. A codex session at a NEEDS_APPROVAL prompt in Decoded-only has no
# Native pane mounted, so native_snapshot() never runs; the poller alone must
# surface the blocked state. We speed the loop with a tiny interval.
# ---------------------------------------------------------------------------


async def test_state_poller_emits_for_active_session_with_dedup(tmp_path, monkeypatch):
    """A driver at NEEDS_APPROVAL emits pty_native_state via the poller — without
    any native_snapshot() call — gated to the active session and deduped on a
    steady state, re-emitting only when the state actually changes."""
    monkeypatch.setattr(dm_mod, "STATE_POLL_INTERVAL_S", 0.05)
    dm, bus, db = await _make_manager(tmp_path)
    q = bus.subscribe()
    sid = "codex-approval"
    try:
        # Inject a stub driver and mark it active — no focus(), no snapshot.
        driver = _StubDriver(DriverState.NEEDS_APPROVAL)
        dm._drivers[sid] = driver  # type: ignore[assignment]
        dm._active_session_id = sid
        dm._ensure_state_poller()

        # First tick emits the mapped native state for NEEDS_APPROVAL.
        ev = await _wait_for_native_state(q, sid, timeout=2.0)
        assert ev is not None, "poller did not emit pty_native_state for active session"
        assert ev["state"] == "permission_prompt"
        assert ev["decoded_input_safe"] is False

        # Steady state: keep polling but dedup — no second emit.
        calls_after_first = driver.capture_calls
        _drain(q)
        await asyncio.sleep(0.2)  # several intervals elapse
        assert driver.capture_calls > calls_after_first, "poller stopped ticking"
        assert await _wait_for_native_state(q, sid, timeout=0.15) is None, (
            "steady state re-emitted (dedup regression)"
        )

        # State change → emits the new mapped state.
        driver.state = DriverState.IDLE
        ev = await _wait_for_native_state(q, sid, timeout=2.0)
        assert ev is not None
        assert ev["state"] == "idle_chat_input"
        assert ev["decoded_input_safe"] is True
    finally:
        await dm.shutdown()
        await db.__aexit__(None, None, None)


async def test_state_poller_skips_inactive_and_dead_sessions(tmp_path, monkeypatch):
    """The poller is gated to the one active session: a driver that is not
    active (or not alive) is never captured, so its state never leaks onto the
    bus. Death is already emitted by _make_on_dead."""
    monkeypatch.setattr(dm_mod, "STATE_POLL_INTERVAL_S", 0.05)
    dm, bus, db = await _make_manager(tmp_path)
    q = bus.subscribe()
    bg = "codex-background"
    try:
        # Background driver at NEEDS_APPROVAL, but NO active session set.
        driver = _StubDriver(DriverState.NEEDS_APPROVAL)
        dm._drivers[bg] = driver  # type: ignore[assignment]
        assert dm._active_session_id is None
        dm._ensure_state_poller()
        await asyncio.sleep(0.25)
        assert driver.capture_calls == 0, "inactive session was polled"
        assert await _wait_for_native_state(q, bg, timeout=0.15) is None

        # Now mark it active — it should start emitting.
        dm._active_session_id = bg
        ev = await _wait_for_native_state(q, bg, timeout=2.0)
        assert ev is not None
        assert ev["state"] == "permission_prompt"

        # A dead active driver is skipped (capture never called again after
        # it reports dead) — death is surfaced via _make_on_dead, not here.
        driver.state = DriverState.DEAD

        class _DeadDriver(_StubDriver):
            def is_alive(self) -> bool:
                return False

        dead = _DeadDriver(DriverState.NEEDS_APPROVAL)
        dm._drivers[bg] = dead  # type: ignore[assignment]
        prior = dead.capture_calls
        _drain(q)
        await asyncio.sleep(0.25)
        assert dead.capture_calls == prior, "dead driver was polled"
    finally:
        await dm.shutdown()
        await db.__aexit__(None, None, None)
