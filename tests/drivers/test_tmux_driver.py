"""Tests for ``agent_decoder.drivers`` — base/registry pure units plus tmux
mechanics against a deterministic fake CLI.

Layering of skips (so CI without tmux just skips the integration block):
  * Pure-unit tests (spawn-builder, registry wiring, availability degrade) run
    everywhere — they monkeypatch ``shutil.which`` and never touch tmux.
  * Mechanics tests gate on a real ``tmux`` and drive ``fake_cli.py`` — fast,
    deterministic, no ``codex`` auth/network needed.
  * The opt-in ``AGENT_CODEX_LIVE`` smoke spawns the *real* ``codex`` (auth +
    network) and is skipped unless tmux+codex are present and the env is set.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid

import pytest

from agent_decoder.drivers import (
    DriverAvailability,
    DriverState,
    TmuxDriver,
    availability_for,
    build_driver,
    codex_spawn_builder,
    supports_driving,
)
from agent_decoder.drivers import registry as drv_registry
from agent_decoder.drivers import tmux_driver as tmux_mod

_HAS_TMUX = shutil.which("tmux") is not None
_HAS_CODEX = shutil.which("codex") is not None


def _terminal_supports_clear() -> bool:
    """The driver bridges tmux via a `tmux attach` client, which needs a
    terminfo that has the `clear` capability. Headless CI has tmux on PATH but
    no usable terminal (TERM unset/dumb), so attach fails with
    "terminal does not support clear". Probe that exact capability (`tput
    clear`) so these integration tests run where a real terminal exists and
    skip where it doesn't, instead of hard-failing."""
    try:
        return (
            subprocess.run(["tput", "clear"], capture_output=True, timeout=5).returncode
            == 0
        )
    except Exception:
        return False


_TMUX_USABLE = _HAS_TMUX and _terminal_supports_clear()

requires_tmux = pytest.mark.skipif(
    not _TMUX_USABLE,
    reason="needs tmux + a terminal with `clear` (TERM); skipped on headless CI",
)

FAKE_CLI = os.path.join(os.path.dirname(__file__), "fake_cli.py")


# ---------------------------------------------------------------------------
# Pure units — no tmux required
# ---------------------------------------------------------------------------


def test_spawn_builder_fresh():
    assert codex_spawn_builder() == ["codex"]


def test_spawn_builder_resume_with_model():
    argv = codex_spawn_builder(resume_uuid="abc-123", model="gpt-5.5")
    assert argv == ["codex", "resume", "abc-123", "--model", "gpt-5.5"]


def test_build_driver_fresh_vs_resume():
    """fresh=True spawns a brand-new codex (no resume target); the default
    resumes by session_id. The `fresh` flag is required because resume_uuid=None
    alone is ambiguous (it falls back to session_id) — this is what lets a NEW
    Codex chat spawn fresh instead of trying to resume a non-existent rollout."""
    fresh = build_driver("codex", "sid-x", "/tmp", fresh=True)
    assert fresh._spawn_command == ["codex"]
    resume_default = build_driver("codex", "sid-x", "/tmp")
    assert resume_default._spawn_command == ["codex", "resume", "sid-x"]
    resume_explicit = build_driver("codex", "sid-x", "/tmp", resume_uuid="real-y")
    assert resume_explicit._spawn_command == ["codex", "resume", "real-y"]


def test_spawn_builder_sandbox_omitted_by_default():
    assert "--sandbox" not in codex_spawn_builder(resume_uuid="x")


def test_spawn_builder_sandbox_when_set():
    argv = codex_spawn_builder(resume_uuid="x", sandbox="read-only")
    assert argv[-2:] == ["--sandbox", "read-only"]


def test_supports_driving_codex_only():
    assert supports_driving("codex") is True
    assert supports_driving("claude") is False
    assert supports_driving("nonexistent") is False


def test_availability_true_when_both_present(monkeypatch):
    monkeypatch.setattr(drv_registry.shutil, "which", lambda b: f"/usr/bin/{b}")
    monkeypatch.setattr(tmux_mod.shutil, "which", lambda b: f"/usr/bin/{b}")
    avail = availability_for("codex")
    assert avail.available is True
    assert avail.reason is None


def test_availability_degrades_without_tmux(monkeypatch):
    # tmux absent → backend unavailable, never spawns.
    monkeypatch.setattr(tmux_mod.shutil, "which", lambda b: None)
    avail = availability_for("codex")
    assert avail.available is False
    assert "tmux" in (avail.reason or "")


def test_availability_degrades_without_codex(monkeypatch):
    # tmux present but codex CLI missing → still not drivable.
    monkeypatch.setattr(tmux_mod.shutil, "which", lambda b: "/usr/bin/tmux")
    monkeypatch.setattr(
        drv_registry.shutil,
        "which",
        lambda b: "/usr/bin/tmux" if b == "tmux" else None,
    )
    avail = availability_for("codex")
    assert avail.available is False
    assert "codex" in (avail.reason or "")


def test_availability_unknown_provider():
    avail = availability_for("claude")
    assert avail.available is False
    assert isinstance(avail, DriverAvailability)


def test_tmux_driver_availability_classmethod(monkeypatch):
    monkeypatch.setattr(tmux_mod.shutil, "which", lambda b: None)
    assert TmuxDriver.availability().available is False
    monkeypatch.setattr(tmux_mod.shutil, "which", lambda b: "/usr/bin/tmux")
    assert TmuxDriver.availability().available is True


def test_build_driver_defaults_resume_to_session_id():
    d = build_driver("codex", "sess-uuid-9", "/tmp")
    assert isinstance(d, TmuxDriver)
    assert d._spawn_command == ["codex", "resume", "sess-uuid-9"]


def test_build_driver_unknown_provider_raises():
    with pytest.raises(KeyError):
        build_driver("claude", "x", "/tmp")


def test_session_name_sanitised():
    d = build_driver("codex", "weird/id:with.dots", "/tmp")
    assert d._tmux_session == "cd_weird_id_with_dots"
    # tmux session names must not contain '.' or ':'
    assert "." not in d._tmux_session and ":" not in d._tmux_session


async def test_write_input_strips_device_attribute_replies(monkeypatch):
    """xterm's DA replies (ESC[>0;276;0c / ESC[?1;2c) must be dropped before
    reaching the pane — otherwise they land as literal cruft in Codex's input
    line. Real keystrokes around them pass through untouched."""
    d = build_driver("codex", "sid", "/tmp")
    written: list[bytes] = []
    monkeypatch.setattr(
        tmux_mod.os, "write", lambda fd, buf: written.append(bytes(buf)) or len(buf)
    )
    d._master_fd = 7  # any non-negative fd; os.write is stubbed
    d._dead = False
    await d.write_input(b"\x1b[>0;276;0c")  # pure DA2 reply → fully stripped → no write
    assert written == []
    await d.write_input(b"hi\x1b[?1;2cthere")  # DA1 reply embedded in real text
    assert b"".join(written) == b"hithere"


# ---------------------------------------------------------------------------
# Mechanics — real tmux, fake CLI
# ---------------------------------------------------------------------------


def _fake_driver(**kw) -> TmuxDriver:
    """A TmuxDriver wired to the deterministic fake CLI on its own socket."""
    sid = f"test-{uuid.uuid4()}"
    return TmuxDriver(
        sid,
        cwd=os.getcwd(),
        spawn_command=[sys.executable, FAKE_CLI],
        socket_name=f"agent-decoder-test-{uuid.uuid4().hex[:8]}",
        rows=24,
        cols=80,
        **kw,
    )


async def _wait_state(driver: TmuxDriver, target: DriverState, timeout=8.0):
    import asyncio

    end = asyncio.get_event_loop().time() + timeout
    last = None
    while asyncio.get_event_loop().time() < end:
        last = await driver.capture_state()
        if last == target:
            return last
        await asyncio.sleep(0.1)
    return last


@requires_tmux
async def test_spawn_capture_idle_then_kill():
    d = _fake_driver()
    try:
        await d.spawn(cols=80, rows=24)
        assert d.is_alive()
        assert await d.has_session()
        state = await _wait_state(d, DriverState.IDLE)
        assert state == DriverState.IDLE
    finally:
        await d.kill()
    assert not d.is_alive()
    assert not await d.has_session()


@requires_tmux
async def test_spawn_disables_tmux_status_bar():
    """The Native view shows only the driven TUI, never tmux's status bar."""
    d = _fake_driver()
    try:
        await d.spawn(cols=80, rows=24)
        rc, out = await d._tmux("show-options", "-t", d._tmux_session, "status")
        assert rc == 0
        assert out.decode().strip().split() == ["status", "off"]
    finally:
        await d.kill()


@requires_tmux
async def test_send_text_running_then_idle():
    d = _fake_driver()
    try:
        await d.spawn(cols=80, rows=24)
        await _wait_state(d, DriverState.IDLE)
        await d.send_text("hello driver")
        assert await _wait_state(d, DriverState.RUNNING) == DriverState.RUNNING
        assert await _wait_state(d, DriverState.IDLE) == DriverState.IDLE
    finally:
        await d.kill()


@requires_tmux
async def test_output_streams_to_ring_and_chunk_hook():
    import asyncio

    chunks: list[bytes] = []
    d = _fake_driver(on_chunk=chunks.append)
    try:
        await d.spawn(cols=80, rows=24)
        # Let the attach client render the idle frame.
        for _ in range(40):
            if d.output_snapshot():
                break
            await asyncio.sleep(0.1)
        assert d.output_snapshot(), "expected pane bytes in the output ring"
        assert chunks, "expected on_chunk to fire for streamed output"
        assert b"Context 100% left" in d.output_snapshot()
    finally:
        await d.kill()


@requires_tmux
async def test_reattach_finds_live_session():
    d = _fake_driver()
    try:
        await d.spawn(cols=80, rows=24)
        await _wait_state(d, DriverState.IDLE)
        # A second driver pointed at the same socket+session re-attaches
        # instead of spawning — this is the persistence/reconnect path.
        d2 = TmuxDriver(
            d.session_id,
            cwd=os.getcwd(),
            spawn_command=[sys.executable, FAKE_CLI],
            socket_name=d._socket,
            rows=24,
            cols=80,
        )
        assert await d2.has_session()
        await d2.attach()
        try:
            assert d2.is_alive()
        finally:
            d2._detach_reader()
            d2._close_master()
            if d2._attach_proc is not None:
                d2._attach_proc.terminate()
                await d2._attach_proc.wait()
    finally:
        await d.kill()


@requires_tmux
async def test_resize_does_not_crash_and_pane_follows():
    d = _fake_driver()
    try:
        await d.spawn(cols=80, rows=24)
        await _wait_state(d, DriverState.IDLE)
        await d.resize(cols=120, rows=40)
        # Session still alive and capturable after a resize.
        assert await d.has_session()
        assert (await d.capture_state()) != DriverState.DEAD
    finally:
        await d.kill()


@requires_tmux
async def test_double_kill_is_safe():
    d = _fake_driver()
    await d.spawn(cols=80, rows=24)
    await d.kill()
    # Second kill must be a no-op, not an error.
    await d.kill()
    assert not d.is_alive()


@requires_tmux
async def test_spawn_twice_raises():
    d = _fake_driver()
    try:
        await d.spawn(cols=80, rows=24)
        with pytest.raises(RuntimeError):
            await d.spawn(cols=80, rows=24)
    finally:
        await d.kill()


@requires_tmux
async def test_capture_state_dead_when_no_session():
    d = _fake_driver()
    # Never spawned → no session → DEAD.
    assert await d.capture_state() == DriverState.DEAD
    assert not await d.has_session()


# ---------------------------------------------------------------------------
# Opt-in live smoke against the real codex binary
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not (_HAS_TMUX and _HAS_CODEX and os.environ.get("AGENT_CODEX_LIVE")),
    reason="set AGENT_CODEX_LIVE=1 with tmux+codex present to run the live smoke",
)
async def test_live_codex_spawn_and_capture():
    """Spawn the real codex TUI in tmux; confirm it comes up non-DEAD."""
    sid = f"live-{uuid.uuid4()}"
    d = TmuxDriver(
        sid,
        cwd=os.getcwd(),
        spawn_command=codex_spawn_builder(sandbox="read-only"),
        socket_name=f"agent-decoder-live-{uuid.uuid4().hex[:8]}",
    )
    try:
        await d.spawn(cols=120, rows=40)
        assert d.is_alive()
        # Real codex may land on idle, a trust dialog, or an update menu —
        # all are valid non-DEAD states proving spawn+capture work live.
        state = await _wait_state(d, DriverState.IDLE, timeout=15.0)
        assert state != DriverState.DEAD
    finally:
        await d.kill()
