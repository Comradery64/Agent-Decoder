"""Tests for ``agent_decoder.pty_runner`` (PtyChannel + PtyManager).

Binary injection strategy
-------------------------
The runner spawns ``bin_name`` via ``asyncio.create_subprocess_exec``,
resolved via ``$PATH``.  Each fixture creates a thin shim named ``claude``
in a tmp dir and prepends that dir to ``$PATH``; the shim execs
``fake_claude_tui.py`` which requires a real TTY on stdin (the PTY slave).

pytest-asyncio auto mode is active (pyproject.toml: asyncio_mode = "auto").
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import AsyncIterator

import pytest

from agent_decoder import pty_runner as pr_mod
from agent_decoder.pty_runner import (
    DEFAULT_IDLE_TIMEOUT_S,
    DEFAULT_IDLE_WARN_S,
    PtyChannel,
    PtyManager,
    PtyOwnershipConflict,
    _subscription_env,
)
from agent_decoder.events_bus import EventBroadcaster

FAKE_TUI = (Path(__file__).parent / "fixtures" / "fake_claude_tui.py").resolve()


# ---------------------------------------------------------------------------
# Shim helpers
# ---------------------------------------------------------------------------


def _write_tui_shim(bin_dir: Path, extra_argv: list[str] | None = None) -> Path:
    """Create an executable shim that execs ``fake_claude_tui.py``.

    ``extra_argv`` is a list of extra args to prepend before the caller's
    sys.argv[1:].  This is rendered as a Python literal so no quoting issues.
    """
    import sys

    shim = bin_dir / "claude"
    python = sys.executable
    extra_repr = repr(extra_argv or [])
    shim.write_text(
        f"#!/usr/bin/env python3\n"
        f"import os, sys\n"
        f"_extra = {extra_repr}\n"
        f"args = ['{python}', '{FAKE_TUI}'] + _extra + sys.argv[1:]\n"
        f"os.execv('{python}', args)\n"
    )
    shim.chmod(0o755)
    return shim


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _StubDB:
    """Minimal DB stub — PtyManager Phase 1 doesn't call any DB methods."""

    pass


@pytest.fixture
def tui_shim_path(monkeypatch, tmp_path):
    """Place a ``claude`` shim on PATH delegating to fake_claude_tui.py.

    Sets CLAUDE_CONFIG_DIR to a clean tmp dir so JSONL output stays
    sandboxed.  Returns the bin dir Path.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_tui_shim(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude_config"))
    return bin_dir


@pytest.fixture
async def manager(tmp_path) -> AsyncIterator[PtyManager]:
    """Fresh PtyManager with default timers wired to a real EventBroadcaster."""
    bus = EventBroadcaster()
    m = PtyManager(_StubDB(), bus)
    yield m
    await m.shutdown()


@pytest.fixture
async def fast_manager(tmp_path) -> AsyncIterator[PtyManager]:
    """PtyManager with shrunk idle timers for idle-timer tests."""
    bus = EventBroadcaster()
    m = PtyManager(_StubDB(), bus, idle_timeout_s=0.7, idle_warn_s=0.4)
    yield m
    await m.shutdown()


# ---------------------------------------------------------------------------
# Helper: wait until a PtyChannel becomes alive
# ---------------------------------------------------------------------------


async def _wait_alive(channel: PtyChannel, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not channel.is_alive():
        if time.monotonic() > deadline:
            raise AssertionError(f"channel not alive after {timeout}s")
        await asyncio.sleep(0.02)


async def _wait_dead(channel: PtyChannel, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while channel.is_alive():
        if time.monotonic() > deadline:
            raise AssertionError(f"channel still alive after {timeout}s")
        await asyncio.sleep(0.05)


async def _wait_pty_output(channel: PtyChannel, *, timeout: float = 5.0) -> None:
    """Poll until the channel has received at least one byte of PTY output."""
    deadline = time.monotonic() + timeout
    while channel.last_pty_output_ms() == 0:
        if time.monotonic() > deadline:
            raise AssertionError(f"no PTY output received after {timeout}s")
        await asyncio.sleep(0.02)


async def _wait_bus_event_type(
    q: asyncio.Queue,
    event_type: str,
    *,
    timeout: float = 3.0,
) -> dict:
    """Read bus events until the requested type appears."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(f"{event_type} event not received within {timeout}s")
        event = await asyncio.wait_for(q.get(), timeout=remaining)
        if event.get("type") == event_type:
            return event


def _drain_events(q: asyncio.Queue) -> list[dict]:
    """Pop all currently-queued bus events (non-blocking).

    Since v0.3.1.3 the runner coalesces PTY output on a per-frame flush and
    emits ``pty_native_state`` alongside chunks, so tests can no longer assume a
    fixed event order off the queue — drain and filter for the event of interest.
    """
    out: list[dict] = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


async def test_input_watchdog_acknowledges_same_millisecond_output(tmp_path):
    """A PTY response in the same ms as submit still acknowledges the input."""
    bus = EventBroadcaster()
    q = bus.subscribe()
    m = PtyManager(_StubDB(), bus)
    session_id = "sess-watchdog-same-ms"
    submit_ms = pr_mod._now_ms()

    channel = PtyChannel(
        session_id=session_id,
        argv=["claude"],
        cwd=str(tmp_path),
        env={},
        bus=bus,
    )
    channel._state.last_pty_output_ms = submit_ms
    channel._state.last_pty_output_seq = 1
    m._channels[session_id] = pr_mod._ManagedChannel(
        channel=channel,
        focus_params=pr_mod._FocusParams(
            cwd=str(tmp_path),
            bin_name="claude",
            model="",
            permission_mode="default",
            new_chat=False,
        ),
    )

    try:
        await m._input_watchdog(
            session_id,
            submit_ms,
            submit_output_seq=0,
            submit_kind="slash",
            ack_after_s=0,
            stall_after_s=0.01,
        )

        ack = await _wait_bus_event_type(q, "pty_input_acknowledged")
        complete = await _wait_bus_event_type(q, "pty_submit_completed")
        assert ack["session_id"] == session_id
        assert complete["session_id"] == session_id
        assert complete["kind"] == "slash"
        assert complete["status"] == "acknowledged"
    finally:
        bus.unsubscribe(q)
        await m.shutdown()


# ---------------------------------------------------------------------------
# Test 1 — Pure unit: _subscription_env strips API key vars
# ---------------------------------------------------------------------------


def test_subscription_env_strips_api_key_vars(monkeypatch):
    """Pure unit: API-key vars must not appear in the returned env dict."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-leaked")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "bearer-leaked")
    monkeypatch.setenv("UNRELATED_VAR", "passthrough-value")

    env = _subscription_env()

    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert env.get("UNRELATED_VAR") == "passthrough-value"
    # HOME and PATH must pass through (needed by the spawned binary)
    assert "PATH" in env


def test_pgrep_session_id_uses_flag_qualified_regex(monkeypatch):
    """The pgrep detector must not search for a bare session-id substring."""
    calls = []

    class Result:
        returncode = 0
        stdout = "123\n456\n"

    def fake_run(args, *, capture_output, text, timeout):
        calls.append(
            {
                "args": args,
                "capture_output": capture_output,
                "text": text,
                "timeout": timeout,
            }
        )
        return Result()

    monkeypatch.setattr(pr_mod.subprocess, "run", fake_run)

    assert pr_mod._pgrep_session_id("abc-123") == [123, 456]
    assert calls == [
        {
            "args": ["pgrep", "-f", r"(--resume|--session-id|-r)[= ]abc-123"],
            "capture_output": True,
            "text": True,
            "timeout": 2.0,
        }
    ]


async def test_pty_env_forces_color_capable_terminal(monkeypatch):
    """Hidden PTYs must not inherit NO_COLOR from the server process."""
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.delenv("CLICOLOR_FORCE", raising=False)
    monkeypatch.delenv("COLORTERM", raising=False)

    env = await pr_mod._pty_env(pr_mod.DEFAULT_ROWS, pr_mod.DEFAULT_COLS, "claude")

    assert env["TERM"] == "xterm-256color"
    assert env["COLORTERM"] == "truecolor"
    assert env["FORCE_COLOR"] == "1"
    assert env["CLICOLOR_FORCE"] == "1"
    assert "NO_COLOR" not in env


async def test_ownership_reports_foreign_even_when_local_channel_alive(
    monkeypatch,
    tmp_path,
):
    """A local hidden PTY must not hide a second Terminal Claude race."""
    bus = EventBroadcaster()
    manager = PtyManager(_StubDB(), bus)
    session_id = "sess-foreign-with-local"
    jsonl_path = tmp_path / f"{session_id}.jsonl"
    channel = PtyChannel(
        session_id=session_id,
        argv=["claude"],
        cwd=str(tmp_path),
        env={},
    )
    channel._state.proc = SimpleNamespace(returncode=None)
    manager._channels[session_id] = pr_mod._ManagedChannel(
        channel=channel,
        focus_params=pr_mod._FocusParams(
            cwd=str(tmp_path),
            bin_name="claude",
            model="",
            permission_mode="default",
            new_chat=False,
            jsonl_path=jsonl_path,
        ),
    )
    monkeypatch.setattr(pr_mod, "_session_conflict_pids", lambda sid, path: [43210])

    ownership = manager.ownership(session_id, jsonl_path)

    assert ownership["status"] == "terminal"
    assert ownership["foreign_pids"] == [43210]


async def test_focus_fast_path_rejects_foreign_terminal_owner(
    monkeypatch,
    tmp_path,
):
    """Repeated focus calls must still detect a newly opened Terminal Claude."""
    bus = EventBroadcaster()
    manager = PtyManager(_StubDB(), bus)
    session_id = "sess-focus-foreign"
    jsonl_path = tmp_path / f"{session_id}.jsonl"
    channel = PtyChannel(
        session_id=session_id,
        argv=["claude"],
        cwd=str(tmp_path),
        env={},
    )
    channel._state.proc = SimpleNamespace(returncode=None)
    manager._channels[session_id] = pr_mod._ManagedChannel(
        channel=channel,
        focus_params=pr_mod._FocusParams(
            cwd=str(tmp_path),
            bin_name="claude",
            model="",
            permission_mode="default",
            new_chat=False,
            jsonl_path=jsonl_path,
        ),
    )
    monkeypatch.setattr(pr_mod, "_session_conflict_pids", lambda sid, path: [43210])

    with pytest.raises(PtyOwnershipConflict) as exc:
        await manager.focus(
            session_id,
            cwd=str(tmp_path),
            bin_name="claude",
            model="",
            permission_mode="default",
            new_chat=False,
            jsonl_path=jsonl_path,
        )

    assert exc.value.foreign_pids == [43210]


async def test_submit_rejects_foreign_terminal_owner_on_live_channel(
    monkeypatch,
    tmp_path,
):
    """A live local channel must not keep sending after a Terminal race appears."""
    bus = EventBroadcaster()
    manager = PtyManager(_StubDB(), bus)
    session_id = "sess-submit-foreign"
    jsonl_path = tmp_path / f"{session_id}.jsonl"
    channel = PtyChannel(
        session_id=session_id,
        argv=["claude"],
        cwd=str(tmp_path),
        env={},
    )
    channel._state.proc = SimpleNamespace(returncode=None)
    manager._channels[session_id] = pr_mod._ManagedChannel(
        channel=channel,
        focus_params=pr_mod._FocusParams(
            cwd=str(tmp_path),
            bin_name="claude",
            model="",
            permission_mode="default",
            new_chat=False,
            jsonl_path=jsonl_path,
        ),
    )
    monkeypatch.setattr(pr_mod, "_session_conflict_pids", lambda sid, path: [43210])

    with pytest.raises(PtyOwnershipConflict) as exc:
        await manager.submit(session_id, "hello")

    assert exc.value.foreign_pids == [43210]


# ---------------------------------------------------------------------------
# Test 2 — PtyChannel: spawn, is_alive, master-fd write, PTY output drain
# ---------------------------------------------------------------------------


async def test_pty_spawn_produces_live_process_and_working_master_fd(
    tui_shim_path, tmp_path
):
    """Spawn a bare PtyChannel, verify liveness + master-fd write + drain."""
    from agent_decoder.pty_runner import _pty_env, DEFAULT_ROWS, DEFAULT_COLS

    session_id = "test-spawn-01"
    # _pty_env is async now; the fake shim has no `auth status` subcommand
    # so the probe returns "" (treated as strip-defensively path).
    env = await _pty_env(DEFAULT_ROWS, DEFAULT_COLS, "claude")
    env["CLAUDE_CONFIG_DIR"] = str(tmp_path / "claude_config")

    channel = PtyChannel(
        session_id=session_id,
        argv=["claude"],
        cwd=str(tmp_path),
        env=env,
    )

    try:
        await channel.start()

        # Channel must be alive immediately after start()
        assert channel.is_alive(), "channel should be alive right after start()"

        # master_fd must be a valid, open file descriptor
        master_fd = channel._state.master_fd
        assert master_fd >= 0, "master_fd should be non-negative"
        # A write of a printable byte must not raise
        try:
            os.write(master_fd, b"x")
        except OSError as exc:
            pytest.fail(f"os.write to master_fd raised OSError: {exc}")

        # The drain reader should receive the fake's banner bytes within 3s
        await _wait_pty_output(channel, timeout=5.0)
        assert channel.last_pty_output_ms() > 0, (
            "expected PTY output timestamp to advance after banner"
        )
        assert channel._state.ring, "ring buffer should contain banner bytes"

    finally:
        await channel.kill()

    assert not channel.is_alive(), "channel should be dead after kill()"


# ---------------------------------------------------------------------------
# Test 3 — PtyManager: auto-respawn after explicit kill
# ---------------------------------------------------------------------------


async def test_submit_after_kill_auto_respawns(tui_shim_path, tmp_path, monkeypatch):
    """After explicit kill (which clears _last_focus), re-focus then submit
    triggers auto-respawn when the channel is dead.

    Sequence:
      1. focus() → spawns channel A
      2. kill() → tears down A AND drops _last_focus
      3. focus() again → spawns channel B (new _last_focus)
      4. kill channel B directly (simulating an idle-kill or crash)
      5. submit() → sees no live channel, finds _last_focus, auto-respawns
    """
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude_config"))

    bus = EventBroadcaster()
    m = PtyManager(_StubDB(), bus)

    session_id = "sess-respawn-01"
    cwd = str(tmp_path)

    try:
        # Step 1: focus() spawns channel A
        await m.focus(
            session_id,
            cwd=cwd,
            bin_name="claude",
            model="",
            permission_mode="dontAsk",
            new_chat=True,
        )
        managed_a = m._channels.get(session_id)
        assert managed_a is not None
        channel_a = managed_a.channel
        await _wait_alive(channel_a)

        # Step 2: explicit kill() — also clears _last_focus
        await m.kill(session_id)
        assert not channel_a.is_alive()
        assert session_id not in m._last_focus, (
            "kill() should clear _last_focus so a bare submit() raises"
        )

        # Step 3: focus() again — repopulates _last_focus, spawns channel B
        await m.focus(
            session_id,
            cwd=cwd,
            bin_name="claude",
            model="",
            permission_mode="dontAsk",
            new_chat=False,
        )
        managed_b = m._channels.get(session_id)
        assert managed_b is not None
        channel_b = managed_b.channel
        await _wait_alive(channel_b)
        assert channel_b is not channel_a, "should have spawned a fresh channel"

        # Step 4: kill channel B at the raw level (mimics idle-kill internal path):
        # pop from channels dict directly (same as _on_idle_kill does), but
        # keep _last_focus intact — that's what idle-kill does.
        async with m._lock:
            m._cancel_idle_timers(m._channels.pop(session_id, None))
        await channel_b.kill()

        assert session_id in m._last_focus, (
            "_last_focus should still be set after direct channel removal"
        )

        # Step 5: submit() must auto-respawn and not raise
        await m.submit(session_id, "hello after respawn")

        managed_c = m._channels.get(session_id)
        assert managed_c is not None, "submit() should have auto-spawned a new channel"
        channel_c = managed_c.channel
        # channel identities must differ (truly new spawn)
        assert channel_c is not channel_b, (
            "submit auto-respawn must create a new channel"
        )
        await _wait_alive(channel_c)
        assert channel_c.is_alive()

    finally:
        await m.shutdown()


# ---------------------------------------------------------------------------
# Test 4 — Idle timer: warn SSE event fires, then kill
# ---------------------------------------------------------------------------


async def test_superseded_session_idle_killed_at_blurred_window(
    tui_shim_path, tmp_path, monkeypatch
):
    """A SUPERSEDED (no longer on-screen) session is idle-killed at the blurred
    window; its _last_focus survives for auto-respawn, and the on-screen session
    is NOT killed.

    Since v0.3.1.3 the on-screen session (_active_session_id) is never idle-killed
    — kept alive for scrollback. A session becomes kill-eligible only once another
    supersedes it: _set_active_session_locked shortens the previous active session
    to blurred_idle_timeout_s, after which _on_idle_kill reaps it.
    """
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude_config"))

    bus = EventBroadcaster()
    # Long primary window so only the blurred shortening can kill within the test.
    m = PtyManager(
        _StubDB(),
        bus,
        idle_timeout_s=60.0,
        idle_warn_s=50.0,
        blurred_idle_timeout_s=0.4,
    )

    sid_a, sid_b = "sess-idle-a", "sess-idle-b"
    cwd = str(tmp_path)

    try:
        await m.focus(
            sid_a,
            cwd=cwd,
            bin_name="claude",
            model="",
            permission_mode="dontAsk",
            new_chat=True,
        )
        channel_a = m._channels[sid_a].channel
        await _wait_alive(channel_a)
        assert m._active_session_id == sid_a

        # Navigate to B → A is superseded and shortened to the blurred window.
        await m.focus(
            sid_b,
            cwd=cwd,
            bin_name="claude",
            model="",
            permission_mode="dontAsk",
            new_chat=True,
        )
        await _wait_alive(m._channels[sid_b].channel)

        # A (now off-screen) is reaped within blurred_idle_timeout_s.
        await _wait_dead(channel_a, timeout=3.0)
        assert not channel_a.is_alive()
        assert sid_a in m._last_focus, (
            "_last_focus must survive idle-kill so auto-respawn can work"
        )
        # B is on-screen now and must NOT be idle-killed.
        assert m._channels[sid_b].channel.is_alive()

    finally:
        await m.shutdown()


# ---------------------------------------------------------------------------
# Test 5 — Auto-respawn after idle kill
# ---------------------------------------------------------------------------


async def test_resubmit_after_idle_kill_respawns(tui_shim_path, tmp_path, monkeypatch):
    """After a superseded session is idle-killed, submit() transparently
    auto-respawns it from cached focus params."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude_config"))

    bus = EventBroadcaster()
    m = PtyManager(
        _StubDB(),
        bus,
        idle_timeout_s=60.0,
        idle_warn_s=50.0,
        blurred_idle_timeout_s=0.4,
    )

    sid_a, sid_b = "sess-respawn-a", "sess-respawn-b"
    cwd = str(tmp_path)

    try:
        await m.focus(
            sid_a,
            cwd=cwd,
            bin_name="claude",
            model="",
            permission_mode="dontAsk",
            new_chat=True,
        )
        original_channel = m._channels[sid_a].channel
        await _wait_alive(original_channel)

        # Supersede A with B → A shortened to the blurred window and reaped.
        await m.focus(
            sid_b,
            cwd=cwd,
            bin_name="claude",
            model="",
            permission_mode="dontAsk",
            new_chat=True,
        )
        await _wait_alive(m._channels[sid_b].channel)
        await _wait_dead(original_channel, timeout=3.0)
        assert sid_a in m._last_focus

        # Now submit to A — should auto-respawn a fresh channel without raising.
        await m.submit(sid_a, "hello after idle kill")

        new_managed = m._channels.get(sid_a)
        assert new_managed is not None, (
            "submit() should have auto-spawned a new channel"
        )
        new_channel = new_managed.channel
        assert new_channel is not original_channel, (
            "auto-respawn must create a fresh PtyChannel"
        )
        await _wait_alive(new_channel)
        assert new_channel.is_alive()

    finally:
        await m.shutdown()


# ---------------------------------------------------------------------------
# Test — switch_model uses /model slash command, keeps the live channel
# ---------------------------------------------------------------------------


async def test_switch_model_uses_slash_command_no_respawn(
    tui_shim_path, tmp_path, monkeypatch
):
    """switch_model() writes ``/model <name>\\r`` to the live PTY and
    updates focus_params + status WITHOUT respawning the channel.

    Verified end-to-end against ``crad`` v2.1.143 that the TUI processes
    the slash command and emits ``Set model to <Name>`` to JSONL stdout;
    that side-effect doesn't reach the fake_claude_tui shim, so we assert
    the in-process effects only: channel identity unchanged, status.model
    updated, _last_focus[sid].model updated.
    """
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude_config"))

    bus = EventBroadcaster()
    m = PtyManager(_StubDB(), bus)

    session_id = "sess-switch-model"
    cwd = str(tmp_path)

    try:
        await m.focus(
            session_id,
            cwd=cwd,
            bin_name="claude",
            model="",  # initially no --model (uses claude's default)
            permission_mode="dontAsk",
            new_chat=True,
        )
        managed = m._channels.get(session_id)
        assert managed is not None
        original_channel = managed.channel
        await _wait_alive(original_channel)

        # Pre-condition: status reports the spawn-time model (empty).
        assert m.status(session_id)["model"] == ""

        # Act: switch via slash command.
        ok = await m.switch_model(session_id, "claude-haiku-4-5", settle_s=0.05)
        assert ok is True, "switch_model should return True for a live channel"

        # Channel must not have been respawned — same instance.
        managed_after = m._channels.get(session_id)
        assert managed_after is not None
        assert managed_after.channel is original_channel, (
            "switch_model must NOT respawn the channel"
        )
        assert managed_after.channel.is_alive()

        # Status + cached focus params must reflect the new model.
        assert m.status(session_id)["model"] == "claude-haiku-4-5"
        assert m._last_focus[session_id].model == "claude-haiku-4-5"
        assert managed_after.focus_params.model == "claude-haiku-4-5"

    finally:
        await m.shutdown()


async def test_switch_model_returns_false_when_no_live_channel(tmp_path):
    """switch_model() must short-circuit (return False) when there's no
    live channel — caller's responsibility to fall through to focus()."""
    bus = EventBroadcaster()
    m = PtyManager(_StubDB(), bus)
    try:
        ok = await m.switch_model("nonexistent-session-id", "claude-haiku-4-5")
        assert ok is False
    finally:
        await m.shutdown()


# ---------------------------------------------------------------------------
# Test 6 — E2E env hygiene: no API key leaks into the spawned process
# ---------------------------------------------------------------------------


async def test_spawned_subprocess_does_not_inherit_api_key(monkeypatch, tmp_path):
    """End-to-end: ANTHROPIC_API_KEY/AUTH_TOKEN set in the parent's environ
    must not reach the child process spawned by PtyManager/PtyChannel.

    Strategy: place a shim with ``--capture-env FILE`` on PATH; focus()
    a session; wait for the shim to write its env JSON; read it and assert
    the blocked vars are absent.
    """
    # Build a shim that captures its env to a file on startup
    env_capture = tmp_path / "child_env.json"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    import sys

    shim = bin_dir / "claude"
    python = sys.executable
    capture_path = str(env_capture)
    shim.write_text(
        f"#!/usr/bin/env python3\n"
        f"import os, sys\n"
        f"args = ['{python}', '{FAKE_TUI}', '--capture-env', '{capture_path}'] + sys.argv[1:]\n"
        f"os.execv('{python}', args)\n"
    )
    shim.chmod(0o755)

    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude_config"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-shouldnotleak")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "bearer-shouldnotleak")

    bus = EventBroadcaster()
    m = PtyManager(_StubDB(), bus)

    session_id = "sess-env-e2e"
    cwd = str(tmp_path)

    try:
        await m.focus(
            session_id,
            cwd=cwd,
            bin_name="claude",
            model="",
            permission_mode="dontAsk",
            new_chat=True,
        )

        # Wait for the capture file to appear AND its content to stabilise.
        # PtyManager's auth probe (``<bin> auth status``) invokes the same
        # shim before the real spawn does, so the shim writes the env file
        # twice — first from the probe (which inherits the full test env)
        # then from the real spawn (which uses the stripped env). The probe
        # write happens first; the spawn's overwrite is what we want to
        # inspect. Poll for stable size to ensure both writes are done.
        deadline = time.monotonic() + 5.0
        last_size = -1
        stable_since: float | None = None
        while time.monotonic() < deadline:
            if env_capture.exists():
                size = env_capture.stat().st_size
                if size == last_size and stable_since is not None:
                    if time.monotonic() - stable_since > 0.3:
                        break
                else:
                    last_size = size
                    stable_since = time.monotonic()
            await asyncio.sleep(0.05)

        assert env_capture.exists(), (
            "child_env.json not written within 5s (shim may have failed to start)"
        )

        child_env = json.loads(env_capture.read_text())

        # --- Core assertions ---
        assert "ANTHROPIC_API_KEY" not in child_env, (
            "ANTHROPIC_API_KEY must not reach the spawned child"
        )
        assert "ANTHROPIC_AUTH_TOKEN" not in child_env, (
            "ANTHROPIC_AUTH_TOKEN must not reach the spawned child"
        )
        # PATH must be present (the binary needs it)
        assert "PATH" in child_env, "PATH must pass through to the child"

    finally:
        await m.shutdown()


# ---------------------------------------------------------------------------
# Additional unit-level tests
# ---------------------------------------------------------------------------


def test_constants_sanity():
    """DEFAULT_IDLE_WARN_S < DEFAULT_IDLE_TIMEOUT_S (required invariant)."""
    assert DEFAULT_IDLE_WARN_S < DEFAULT_IDLE_TIMEOUT_S


def test_pty_manager_rejects_bad_timer_config():
    """PtyManager must raise if idle_warn_s >= idle_timeout_s."""
    bus = EventBroadcaster()
    with pytest.raises(ValueError, match="idle_warn_s"):
        PtyManager(_StubDB(), bus, idle_timeout_s=1.0, idle_warn_s=1.0)
    with pytest.raises(ValueError, match="idle_warn_s"):
        PtyManager(_StubDB(), bus, idle_timeout_s=1.0, idle_warn_s=2.0)


def test_encode_submit_translates_newlines():
    """_encode_submit: \\n becomes 0x0a, trailing CR is not doubled, ends with CR."""
    bus = EventBroadcaster()
    m = PtyManager(_StubDB(), bus)
    result = m._encode_submit("hello\nworld")
    # In-message \n stays as 0x0a (Ctrl+J)
    assert b"\x0a" in result
    # Must end with CR (0x0d)
    assert result.endswith(b"\r")
    # No double-CR (input has no trailing newline; strip then append)
    assert not result.endswith(b"\r\r")


def test_encode_submit_strips_trailing_crlf():
    """_encode_submit must strip user-provided trailing CR/LF before appending."""
    bus = EventBroadcaster()
    m = PtyManager(_StubDB(), bus)
    result = m._encode_submit("hello\r\n")
    # Only one trailing CR
    assert result == b"hello\r"


def test_status_returns_dead_snapshot_when_no_channel():
    """status() for an unknown session_id must return alive=False."""
    bus = EventBroadcaster()
    m = PtyManager(_StubDB(), bus)
    snap = m.status("nonexistent-session")
    assert snap["alive"] is False
    assert snap["last_activity_ms"] == 0


async def test_submit_raises_without_prior_focus():
    """submit() without any prior focus() must raise RuntimeError."""
    bus = EventBroadcaster()
    m = PtyManager(_StubDB(), bus)
    try:
        with pytest.raises(RuntimeError, match="focus"):
            await m.submit("sess-no-focus", "hello")
    finally:
        await m.shutdown()


async def test_await_ready_settles_after_marker(tmp_path):
    """Ready marker waits briefly so cold submits don't race raw input setup."""
    channel = PtyChannel(
        "sess-ready-settle",
        argv=["claude"],
        cwd=str(tmp_path),
        env={},
    )
    channel._state.ring.extend(b"\x1b[?2004h")

    start = time.monotonic()
    assert await channel.await_ready(timeout_s=1.0, settle_s=0.02) is True
    assert time.monotonic() - start >= 0.015


async def test_shutdown_is_idempotent():
    """Calling shutdown() twice must not raise."""
    bus = EventBroadcaster()
    m = PtyManager(_StubDB(), bus)
    await m.shutdown()
    await m.shutdown()  # must not raise


async def test_focus_raises_after_shutdown():
    """focus() after shutdown() must raise RuntimeError."""
    bus = EventBroadcaster()
    m = PtyManager(_StubDB(), bus)
    await m.shutdown()
    with pytest.raises(RuntimeError, match="shutting down"):
        await m.focus(
            "s",
            cwd="/tmp",
            bin_name="claude",
            model="",
            permission_mode="dontAsk",
            new_chat=True,
        )


# ---------------------------------------------------------------------------
# Test — focus() is a no-op when an alive channel matches the requested params
# ---------------------------------------------------------------------------


async def test_focus_is_noop_when_params_match_alive_channel(
    tui_shim_path, tmp_path, monkeypatch
):
    """A second focus() with the same bin_name/cwd/model/permission_mode
    on an already-alive channel must NOT respawn the PTY and must leave
    the channel identity untouched. It DOES reset the idle timer — this
    is required so a previously-blurred session (short kill window) gets
    a fresh full-length lease on refocus.

    Frontend was observed eagerly calling focus() on every input click;
    bumping the channel each time produced orphan claude PTYs.
    """
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude_config"))

    bus = EventBroadcaster()
    m = PtyManager(_StubDB(), bus)

    session_id = "sess-focus-noop"
    cwd = str(tmp_path)
    kwargs = dict(
        cwd=cwd,
        bin_name="claude",
        model="",
        permission_mode="dontAsk",
    )

    try:
        # First focus — spawns channel A.
        await m.focus(session_id, new_chat=True, **kwargs)
        managed_a = m._channels.get(session_id)
        assert managed_a is not None
        channel_a = managed_a.channel
        await _wait_alive(channel_a)

        original_kill_handle = managed_a.idle_kill_handle
        original_kill_at_ms = managed_a.idle_kill_at_ms
        assert original_kill_handle is not None

        # Small wait so reset would observably advance the kill timestamp.
        await asyncio.sleep(0.05)

        # Second focus — identical spawn params, new_chat flipped to False
        # (the FE will toggle this once the session is live on disk).
        # Channel must be reused, but idle timer is refreshed.
        await m.focus(session_id, new_chat=False, **kwargs)

        managed_after = m._channels.get(session_id)
        assert managed_after is managed_a, "managed wrapper must not change"
        assert managed_after.channel is channel_a, (
            "second focus with matching params must NOT respawn"
        )
        assert channel_a.is_alive()
        # Timer IS reset — old handle cancelled, new handle scheduled.
        assert managed_after.idle_kill_handle is not original_kill_handle, (
            "no-op focus should refresh the idle timer to restore full lease"
        )
        assert managed_after.idle_kill_at_ms > original_kill_at_ms

        # Differing param (model) — must fall through to the existing
        # path. We assert it does NOT short-circuit by observing the
        # idle timer being reset (the kill timestamp advances).
        await asyncio.sleep(0.05)
        await m.focus(
            session_id,
            new_chat=False,
            cwd=cwd,
            bin_name="claude",
            model="claude-haiku-4-5",  # changed
            permission_mode="dontAsk",
        )
        managed_after_change = m._channels.get(session_id)
        assert managed_after_change is not None
        # Note: with the current _ensure_channel_locked logic, a live
        # channel is reused even when params differ — the test asserts
        # only that the no-op fast path was NOT taken (timer was reset
        # and _last_focus was updated).
        assert m._last_focus[session_id].model == "claude-haiku-4-5"
        assert managed_after_change.idle_kill_at_ms > original_kill_at_ms, (
            "differing-param focus must reset the idle timer (fall-through path)"
        )

    finally:
        await m.shutdown()


# ---------------------------------------------------------------------------
# Test — auth_required SSE emitted on "Not logged in" pattern in PTY output
# ---------------------------------------------------------------------------


async def test_auth_required_emitted_on_not_logged_in_pattern(tmp_path):
    """When the TUI prints ``Not logged in``, PtyManager emits a single
    ``auth_required`` SSE event so the FE can show the login modal
    (``frontend/src/api/client.ts`` ~L319 already handles the event).

    The hook fires from inside PtyChannel's drain callback; we drive it
    by feeding the channel via its master-fd hook directly so the test
    doesn't depend on a spawned subprocess. The PtyManager singleton
    that owns the channel is the bus publisher.
    """
    from agent_decoder.pty_runner import PtyChannel

    bus = EventBroadcaster()
    q = bus.subscribe()
    m = PtyManager(_StubDB(), bus)

    session_id = "sess-auth-req-01"

    # Build a PtyChannel wired to the manager's hook but without spawning
    # — we'll invoke the hook directly with a synthetic chunk.
    channel = PtyChannel(
        session_id=session_id,
        argv=["claude"],
        cwd=str(tmp_path),
        env={},
        on_chunk=m._scan_chunk_for_hitl,
    )

    try:
        # _scan_chunk_for_hitl emits classification (pty_native_state) + the
        # one-shot auth_required; output chunks are coalesced on a separate
        # per-frame flush path, so assert on auth_required rather than a fixed
        # event sequence.
        # 1. Innocuous chunk → no auth_required.
        m._scan_chunk_for_hitl(channel, b"\x1b[?2004h\xe2\x9c\xb3 welcome\r\n> ")
        assert not [e for e in _drain_events(q) if e["type"] == "auth_required"], (
            "no auth_required event should fire before the pattern appears"
        )
        assert channel._state.auth_required_emitted is False

        # 2. Chunk containing the marker → exactly one auth_required event.
        m._scan_chunk_for_hitl(channel, b"Not logged in \xc2\xb7 Please run /login\r\n")
        assert [e for e in _drain_events(q) if e["type"] == "auth_required"] == [
            {"type": "auth_required", "session_id": session_id}
        ]
        assert channel._state.auth_required_emitted is True

        # 3. Subsequent chunks with the marker MUST NOT re-emit (guard).
        m._scan_chunk_for_hitl(channel, b"Not logged in (frame redraw)\r\n")
        m._scan_chunk_for_hitl(channel, b"Not logged in\r\n")
        assert not [e for e in _drain_events(q) if e["type"] == "auth_required"], (
            "auth_required must fire at most once per channel"
        )

    finally:
        bus.unsubscribe(q)
        await m.shutdown()


async def test_auth_required_detects_pattern_split_across_chunks(tmp_path):
    """Carryover logic: the substring may straddle the boundary between
    two read() chunks. The hook scans a window of (chunk + last N bytes
    of ring) so a split is still caught.

    Real-world repro: TUI emits the banner mid-redraw and OS read() may
    return the head and tail in separate calls.
    """
    from agent_decoder.pty_runner import PtyChannel

    bus = EventBroadcaster()
    q = bus.subscribe()
    m = PtyManager(_StubDB(), bus)

    channel = PtyChannel(
        session_id="sess-auth-split",
        argv=["claude"],
        cwd=str(tmp_path),
        env={},
        on_chunk=m._scan_chunk_for_hitl,
    )

    try:
        # Simulate the drain: ring is what the channel would have written
        # before calling the hook. Hook contract: caller appends to ring
        # BEFORE invoking on_chunk (see PtyChannel._on_readable).
        chunk_a = b"some prelude bytes\r\nNot logg"
        channel._state.ring.extend(chunk_a)
        m._scan_chunk_for_hitl(channel, chunk_a)
        assert not [e for e in _drain_events(q) if e["type"] == "auth_required"], (
            "partial marker must NOT fire auth_required alone"
        )
        assert channel._state.auth_required_emitted is False

        chunk_b = b"ed in \xc2\xb7 Please run /login\r\n"
        channel._state.ring.extend(chunk_b)
        m._scan_chunk_for_hitl(channel, chunk_b)
        auth = [e for e in _drain_events(q) if e["type"] == "auth_required"]
        assert auth and auth[0]["session_id"] == "sess-auth-split"

    finally:
        bus.unsubscribe(q)
        await m.shutdown()


# ---------------------------------------------------------------------------
# Test — unfocus shortens the idle-kill window (orphan-PTY prevention)
# ---------------------------------------------------------------------------


async def test_unfocus_shortens_idle_kill_window(tui_shim_path, tmp_path, monkeypatch):
    """``unfocus`` (called by /api/pty/blur) brings the idle-kill in for a session
    that is no longer on screen.

    Since v0.3.1.3 unfocus() is a no-op for the on-screen session
    (_active_session_id, kept alive for scrollback) — only a non-active session
    gets the shortened blurred window. We make the session non-active (as if its
    native view/tab closed, clearing _active_session_id) before blurring, then
    verify the kill is moved forward and the channel is reaped.
    """
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude_config"))

    bus = EventBroadcaster()
    # Long primary window so the difference is unambiguous.
    m = PtyManager(
        _StubDB(),
        bus,
        idle_timeout_s=60.0,
        idle_warn_s=50.0,
        blurred_idle_timeout_s=0.4,
    )

    session_id = "sess-blur-01"
    cwd = str(tmp_path)

    try:
        await m.focus(
            session_id,
            cwd=cwd,
            bin_name="claude",
            model="",
            permission_mode="dontAsk",
            new_chat=True,
        )
        managed = m._channels.get(session_id)
        assert managed is not None
        channel = managed.channel
        await _wait_alive(channel)

        long_kill_at = managed.idle_kill_at_ms
        # Sanity: focus scheduled the kill ~60s out.
        assert long_kill_at > 0

        # No longer the on-screen session — unfocus is a no-op while active.
        m._active_session_id = None
        await m.unfocus(session_id)
        assert managed.idle_kill_at_ms < long_kill_at, (
            "unfocus must move a non-active session's kill earlier, not preserve "
            "the full timer"
        )

        # The channel should die within blurred_idle_timeout_s + margin.
        await _wait_dead(channel, timeout=2.0)
        assert not channel.is_alive(), (
            "channel should have been idle-killed within blurred_idle_timeout_s"
        )

    finally:
        await m.shutdown()


async def test_repeated_unfocus_does_not_push_kill_further_out(
    tui_shim_path, tmp_path, monkeypatch
):
    """Calling unfocus twice must not extend the kill schedule.

    Guards against an obvious naive implementation where each blur resets
    the timer to ``now + blurred_idle_timeout_s`` — that would let an
    eager BlurredFocus loop keep PTYs alive indefinitely.
    """
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude_config"))

    bus = EventBroadcaster()
    m = PtyManager(
        _StubDB(),
        bus,
        idle_timeout_s=60.0,
        idle_warn_s=50.0,
        blurred_idle_timeout_s=10.0,
    )
    session_id = "sess-blur-02"

    try:
        await m.focus(
            session_id,
            cwd=str(tmp_path),
            bin_name="claude",
            model="",
            permission_mode="dontAsk",
            new_chat=True,
        )
        await _wait_alive(m._channels[session_id].channel)

        await m.unfocus(session_id)
        first_kill_at = m._channels[session_id].idle_kill_at_ms
        # Small delay so a naive reschedule would observably advance the timer.
        await asyncio.sleep(0.15)
        await m.unfocus(session_id)
        second_kill_at = m._channels[session_id].idle_kill_at_ms

        assert second_kill_at == first_kill_at, (
            f"second blur extended kill from {first_kill_at} to {second_kill_at}"
        )

    finally:
        await m.shutdown()


async def test_focus_after_unfocus_restores_long_timer(
    tui_shim_path, tmp_path, monkeypatch
):
    """If the user comes back (re-focus), the kill window goes back to the
    standard idle_timeout_s — orphan-prevention shouldn't penalise revisit."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude_config"))

    bus = EventBroadcaster()
    m = PtyManager(
        _StubDB(),
        bus,
        idle_timeout_s=30.0,
        idle_warn_s=25.0,
        blurred_idle_timeout_s=1.0,
    )
    session_id = "sess-blur-03"

    try:
        await m.focus(
            session_id,
            cwd=str(tmp_path),
            bin_name="claude",
            model="",
            permission_mode="dontAsk",
            new_chat=True,
        )
        await _wait_alive(m._channels[session_id].channel)

        await m.unfocus(session_id)
        blurred_kill_at = m._channels[session_id].idle_kill_at_ms

        # Refocus same params; channel is reused (idempotency from earlier test)
        # but the idle timer must be reset to the long window.
        await m.focus(
            session_id,
            cwd=str(tmp_path),
            bin_name="claude",
            model="",
            permission_mode="dontAsk",
            new_chat=False,
        )
        refocused_kill_at = m._channels[session_id].idle_kill_at_ms
        assert refocused_kill_at > blurred_kill_at, (
            "refocus should push the kill back to the long idle window"
        )

    finally:
        await m.shutdown()


# ---------------------------------------------------------------------------
# Phase 3 — per-session asyncio.Lock
# ---------------------------------------------------------------------------


async def test_session_lock_identity_per_sid(tmp_path):
    """Same sid → same Lock instance; different sids → different Locks.

    The lock IS the serialization primitive. If two calls on the same
    sid get different Lock instances they can't serialize.
    """
    bus = EventBroadcaster()
    m = PtyManager(_StubDB(), bus)
    try:
        a = m._session_lock("sid-x")
        b = m._session_lock("sid-x")
        c = m._session_lock("sid-y")
        assert a is b, "same sid must return the same Lock"
        assert a is not c, "different sids must get different Locks"
    finally:
        await m.shutdown()


async def test_submit_blocks_on_per_session_lock(tui_shim_path, tmp_path, monkeypatch):
    """A pre-acquired ``_session_lock(sid)`` blocks ``submit(sid)``.

    Proves the lock encompasses submit's PTY-write critical section —
    if it didn't, the submit would race through and the JSONL writes
    from two concurrent tabs would interleave.
    """
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude_config"))
    bus = EventBroadcaster()
    m = PtyManager(_StubDB(), bus)
    sid = "sess-phase3-block"
    try:
        await m.focus(
            sid,
            cwd=str(tmp_path),
            bin_name="claude",
            model="",
            permission_mode="dontAsk",
            new_chat=True,
        )
        await _wait_alive(m._channels[sid].channel)

        sid_lock = m._session_lock(sid)
        await sid_lock.acquire()
        try:
            submit_task = asyncio.create_task(m.submit(sid, "hi"))
            # Give the loop a few ticks; submit must be parked on the lock.
            await asyncio.sleep(0.15)
            assert not submit_task.done(), (
                "submit on the same sid should block while the per-session lock is held"
            )
        finally:
            sid_lock.release()

        await asyncio.wait_for(submit_task, timeout=5.0)
    finally:
        await m.shutdown()


async def test_submit_does_not_block_across_sids(tui_shim_path, tmp_path, monkeypatch):
    """A held lock on sid X does NOT delay a submit on sid Y.

    The whole point of the per-session lock is fine-grained
    serialization. A slow / blocked op on one session must not gate
    progress on another.
    """
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude_config"))
    bus = EventBroadcaster()
    m = PtyManager(_StubDB(), bus)
    sid_a = "sess-phase3-a"
    sid_b = "sess-phase3-b"
    try:
        for sid in (sid_a, sid_b):
            await m.focus(
                sid,
                cwd=str(tmp_path),
                bin_name="claude",
                model="",
                permission_mode="dontAsk",
                new_chat=True,
            )
            await _wait_alive(m._channels[sid].channel)

        lock_a = m._session_lock(sid_a)
        await lock_a.acquire()
        try:
            # submit on B must complete despite A's lock being held.
            await asyncio.wait_for(m.submit(sid_b, "ping b"), timeout=5.0)
        finally:
            lock_a.release()
    finally:
        await m.shutdown()


async def test_write_raw_input_writes_bytes_without_submit_encoding(
    tui_shim_path, tmp_path, monkeypatch
):
    """Native View writes raw terminal bytes without chat-submit encoding."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude_config"))
    bus = EventBroadcaster()
    m = PtyManager(_StubDB(), bus, idle_timeout_s=30, idle_warn_s=20)

    try:
        await m.focus(
            "sess-native",
            cwd=str(tmp_path),
            bin_name="claude",
            model="",
            permission_mode="default",
            new_chat=True,
        )

        await m.write_raw_input("sess-native", b"\x1b[A")

        managed = m._channels["sess-native"]
        assert managed.channel.last_input_ms() > 0
    finally:
        await m.shutdown()


async def test_native_snapshot_reports_ring_dimensions_and_alive(
    tui_shim_path, tmp_path, monkeypatch
):
    """Native snapshots expose a bounded output ring and terminal dimensions."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude_config"))
    bus = EventBroadcaster()
    m = PtyManager(_StubDB(), bus, idle_timeout_s=30, idle_warn_s=20)

    try:
        await m.focus(
            "sess-native-snap",
            cwd=str(tmp_path),
            bin_name="claude",
            model="",
            permission_mode="default",
            new_chat=True,
        )

        snap = m.native_snapshot("sess-native-snap")

        assert snap["session_id"] == "sess-native-snap"
        assert snap["alive"] is True
        assert snap["rows"] > 0
        assert snap["cols"] > 0
        assert "ring_b64" in snap
        assert snap["ring_complete"] is True
    finally:
        await m.shutdown()


async def test_native_snapshot_reports_incomplete_ring_after_output_overflow(
    tui_shim_path, tmp_path, monkeypatch
):
    """Native snapshots mark bounded rings unsafe once old bytes were dropped."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude_config"))
    bus = EventBroadcaster()
    m = PtyManager(_StubDB(), bus, idle_timeout_s=30, idle_warn_s=20)

    try:
        await m.focus(
            "sess-native-overflow",
            cwd=str(tmp_path),
            bin_name="claude",
            model="",
            permission_mode="default",
            new_chat=True,
        )

        channel = m._channels["sess-native-overflow"].channel
        channel._state.ring.extend(b"x" * (pr_mod.OUTPUT_RING_BYTES + 1))
        overflow = len(channel._state.ring) - pr_mod.OUTPUT_RING_BYTES
        del channel._state.ring[:overflow]
        channel._state.ring_complete = False

        snap = m.native_snapshot("sess-native-overflow")

        assert snap["ring_complete"] is False
    finally:
        await m.shutdown()


async def test_focus_spawns_at_requested_rows(tui_shim_path, tmp_path, monkeypatch):
    """focus(rows=N) spawns the PTY at N rows, not the default.

    The Native view fits the terminal to the pane BEFORE spawning and passes
    the fitted rows, so claude renders at its final height from the first frame
    — no spawn-at-DEFAULT_ROWS-then-resize grow that smears the revealed rows
    and strands claude's footer mid-pane.
    """
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude_config"))
    bus = EventBroadcaster()
    m = PtyManager(_StubDB(), bus, idle_timeout_s=30, idle_warn_s=20)

    try:
        await m.focus(
            "sess-spawn-rows",
            cwd=str(tmp_path),
            bin_name="claude",
            model="",
            permission_mode="default",
            new_chat=True,
            rows=72,
        )
        channel = m._channels["sess-spawn-rows"].channel
        rows, _cols = channel.dimensions()
        assert rows == 72
        # And the cached focus params remember it (auto-respawn fidelity).
        assert m._last_focus["sess-spawn-rows"].rows == 72
    finally:
        await m.shutdown()


async def test_resize_updates_channel_dimensions(tui_shim_path, tmp_path, monkeypatch):
    """Native resize updates the live PTY dimensions."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude_config"))
    bus = EventBroadcaster()
    m = PtyManager(_StubDB(), bus, idle_timeout_s=30, idle_warn_s=20)

    try:
        await m.focus(
            "sess-native-resize",
            cwd=str(tmp_path),
            bin_name="claude",
            model="",
            permission_mode="default",
            new_chat=True,
        )

        channel = m._channels["sess-native-resize"].channel
        channel._state.ring.extend(b"stale wide-frame output")

        await m.resize("sess-native-resize", 24, 80)

        rows, cols = channel.dimensions()
        assert (rows, cols) == (24, 80)
        assert b"stale wide-frame output" not in channel.output_snapshot()
        assert channel.output_snapshot_complete() is True
    finally:
        await m.shutdown()


async def test_resize_rows_only_preserves_ring(tui_shim_path, tmp_path, monkeypatch):
    """A height-only resize must NOT wipe scrollback.

    The FE's post-spawn resize always bumps the row count (spawn rows → the
    fitted viewport height) while keeping the pinned column width. xterm
    reflows scrollback losslessly on a height change, so the captured ring
    must survive it — clearing here discarded the oldest history (the top of
    the conversation) ~1s into claude's render, which is the "scroll up but
    never reach the top" bug on re-attach. Only a width change garbles replay.
    """
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude_config"))
    bus = EventBroadcaster()
    m = PtyManager(_StubDB(), bus, idle_timeout_s=30, idle_warn_s=20)

    try:
        await m.focus(
            "sess-native-resize-rows",
            cwd=str(tmp_path),
            bin_name="claude",
            model="",
            permission_mode="default",
            new_chat=True,
        )

        channel = m._channels["sess-native-resize-rows"].channel
        _, cols = channel.dimensions()
        channel._state.ring.extend(b"captured history output")

        # Same width, different height — must preserve the ring.
        await m.resize("sess-native-resize-rows", 99, cols)

        rows, new_cols = channel.dimensions()
        assert (rows, new_cols) == (99, cols)
        assert b"captured history output" in channel.output_snapshot()
        assert channel.output_snapshot_complete() is True
    finally:
        await m.shutdown()


async def test_pty_output_chunk_published_on_read(tui_shim_path, tmp_path, monkeypatch):
    """Every drained PTY chunk is broadcast for Native View rendering."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude_config"))
    bus = EventBroadcaster()
    q = bus.subscribe()
    m = PtyManager(_StubDB(), bus, idle_timeout_s=30, idle_warn_s=20)

    try:
        await m.focus(
            "sess-output",
            cwd=str(tmp_path),
            bin_name="claude",
            model="",
            permission_mode="default",
            new_chat=True,
        )
        await _wait_alive(m._channels["sess-output"].channel)

        # The coalesced flush only publishes pty_output_chunk for the on-screen
        # (_active_session_id) session, and the fake's bootstrap banner can be
        # emitted before focus() marks the session active. Drive fresh output
        # now that it IS active (the fake echoes input) and drain for the chunk —
        # output is coalesced per-frame and pty_native_state may precede it.
        await m.submit("sess-output", "hello")
        event = await _wait_bus_event_type(q, "pty_output_chunk", timeout=5)
        assert event["session_id"] == "sess-output"
        assert event["data_b64"]
    finally:
        bus.unsubscribe(q)
        await m.shutdown()


async def test_native_snapshot_classifies_ring_state(
    tui_shim_path, tmp_path, monkeypatch
):
    """Native snapshots expose the conservative classifier state."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude_config"))
    bus = EventBroadcaster()
    m = PtyManager(_StubDB(), bus, idle_timeout_s=30, idle_warn_s=20)

    try:
        await m.focus(
            "sess-native-classified",
            cwd=str(tmp_path),
            bin_name="claude",
            model="",
            permission_mode="default",
            new_chat=True,
        )
        m._channels["sess-native-classified"].channel._state.ring.extend(
            "Not logged in · Please run /login".encode("utf-8"),
        )

        snap = m.native_snapshot("sess-native-classified")

        assert snap["native_state"] == "login_required"
        assert snap["decoded_input_safe"] is False
    finally:
        await m.shutdown()


async def test_pty_native_state_published_on_read(tui_shim_path, tmp_path, monkeypatch):
    """PTY output publishes the current conservative native state."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude_config"))
    bus = EventBroadcaster()
    q = bus.subscribe()
    m = PtyManager(_StubDB(), bus, idle_timeout_s=30, idle_warn_s=20)

    try:
        await m.focus(
            "sess-native-state",
            cwd=str(tmp_path),
            bin_name="claude",
            model="",
            permission_mode="default",
            new_chat=True,
        )

        event = await _wait_bus_event_type(q, "pty_native_state", timeout=3)
        assert event["session_id"] == "sess-native-state"
        assert event["state"] == "idle_chat_input"
        assert event["decoded_input_safe"] is True
    finally:
        bus.unsubscribe(q)
        await m.shutdown()


async def test_pty_native_state_dead_published_when_channel_exits(
    tui_shim_path, tmp_path, monkeypatch
):
    """A PTY EOF publishes ``dead`` so the UI can clear stale native state."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude_config"))
    bus = EventBroadcaster()
    q = bus.subscribe()
    m = PtyManager(_StubDB(), bus, idle_timeout_s=30, idle_warn_s=20)
    session_id = "sess-native-dead-state"

    try:
        await m.focus(
            session_id,
            cwd=str(tmp_path),
            bin_name="claude",
            model="",
            permission_mode="default",
            new_chat=True,
        )

        await m.write_raw_input(session_id, b"\x03")

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            event = await _wait_bus_event_type(q, "pty_native_state", timeout=1)
            if event["session_id"] == session_id and event["state"] == "dead":
                assert event["decoded_input_safe"] is False
                break
        else:
            raise AssertionError("dead native state event was not published")
    finally:
        bus.unsubscribe(q)
        await m.shutdown()
