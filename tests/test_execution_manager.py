"""Cross-provider lifecycle and ownership using isolated synthetic terminals."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_decoder import driver_manager, execution_manager
from agent_decoder.drivers import TmuxDriver
from agent_decoder.events_bus import EventBroadcaster
from agent_decoder.execution_manager import ExecutionManager
from agent_decoder.pty_runner import PtyChannel, PtyOwnershipConflict
from agent_decoder.tmux_terminal import TmuxTerminal

FAKE = Path(__file__).parent / "fixtures" / "fake_claude_tui.py"
CODEX = Path(__file__).parent / "drivers" / "fake_cli.py"
requires_tmux = pytest.mark.skipif(not shutil.which("tmux"), reason="tmux required")


@pytest.fixture
def terminal_setup(tmp_path, monkeypatch):
    name = "ad-unified-test-" + uuid.uuid4().hex[:12]
    monkeypatch.setenv("AGENT_DECODER_TMUX_SOCKET", name)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "config"))
    shim = tmp_path / "claude-fake"
    shim.write_text(
        f"#!{sys.executable}\nimport os\nos.execv({sys.executable!r}, [{sys.executable!r}, {str(FAKE)!r}] + __import__('sys').argv[1:])\n"
    )
    shim.chmod(0o700)
    params = dict(
        cwd=str(tmp_path),
        bin_name=str(shim),
        model="",
        permission_mode="dontAsk",
        new_chat=True,
        jsonl_path=tmp_path / "session.jsonl",
    )
    yield name, params
    if shutil.which("tmux"):
        subprocess.run(["tmux", "-L", name, "kill-server"], capture_output=True)


@requires_tmux
async def test_claude_shutdown_detaches_then_restart_adopts_same_harness(
    terminal_setup,
):
    _, params = terminal_setup
    sid = str(uuid.uuid4())
    first = ExecutionManager(
        None, EventBroadcaster(), idle_timeout_s=0.1, idle_warn_s=0.05
    )
    second = ExecutionManager(None, EventBroadcaster())
    terminal = None
    try:
        await first.focus(sid, **params)
        channel = first._channels[sid].channel
        terminal = channel._terminal
        pid = await terminal.managed_pid()
        await first.unfocus(sid)
        await asyncio.sleep(0.2)
        assert first.is_alive(sid)
        await first.shutdown()
        assert await terminal.managed_pid() == pid
        assert not params["jsonl_path"].with_suffix(".jsonl.lock").exists()
        await second.focus(sid, **{**params, "new_chat": False})
        assert second._channels[sid].channel._harness_pid == pid
        await second.submit(sid, "hello after restart")
        await second.kill(sid)
        assert not await terminal.exists()
    finally:
        await first.shutdown()
        await second.shutdown()
        if terminal is not None and await terminal.exists():
            await terminal.terminate()


@requires_tmux
async def test_codex_shutdown_detaches_and_rekeys_output_after_restart(
    terminal_setup, monkeypatch
):
    name, params = terminal_setup
    monkeypatch.setattr(
        driver_manager,
        "build_driver",
        lambda provider, sid, cwd, **kw: TmuxDriver(
            sid,
            cwd,
            [sys.executable, str(CODEX)],
            socket_name=name,
            **{
                k: v
                for k, v in kw.items()
                if k not in {"model", "resume_uuid", "fresh", "socket_name"}
            },
        ),
    )
    first = ExecutionManager(None, EventBroadcaster())
    bus = EventBroadcaster()
    second = ExecutionManager(None, bus)
    sid = str(uuid.uuid4())
    try:
        await first.focus(sid, provider="codex", cwd=params["cwd"])
        driver = first._drivers[sid]
        await first.shutdown()
        assert await driver.has_session()
        await second.focus(sid, provider="codex", cwd=params["cwd"])
        new_id = str(uuid.uuid4())
        assert await second.rekey(sid, new_id)
        queue = bus.subscribe()
        second._drivers[new_id]._on_chunk(b"synthetic output")
        assert queue.get_nowait()["session_id"] == new_id
        await second.kill(new_id)
        assert not await driver.has_session()
    finally:
        await first.shutdown()
        await second.shutdown()


@requires_tmux
async def test_foreign_sidecar_blocks_adoption(terminal_setup, monkeypatch):
    _, params = terminal_setup
    sid = str(uuid.uuid4())
    first = ExecutionManager(None, EventBroadcaster())
    second = ExecutionManager(None, EventBroadcaster())
    try:
        await first.focus(sid, **params)
        terminal = first._channels[sid].channel._terminal
        await first.shutdown()
        monkeypatch.setattr(
            execution_manager,
            "_fresh_foreign_sidecar",
            lambda path: SimpleNamespace(pid=12345),
        )
        with pytest.raises(PtyOwnershipConflict):
            await second.focus(sid, **params)
        assert await terminal.managed_pid() is not None
    finally:
        await second.shutdown()


@requires_tmux
async def test_changed_launch_permissions_respawn_verified_pane(terminal_setup):
    _, params = terminal_setup
    sid = str(uuid.uuid4())
    manager = ExecutionManager(None, EventBroadcaster())
    try:
        await manager.focus(sid, **params)
        old_pid = manager._channels[sid].channel._harness_pid
        await manager.focus(sid, **{**params, "permission_mode": "default"})
        assert manager._channels[sid].channel._harness_pid != old_pid
        assert manager.status(sid)["permission_mode"] == "default"
    finally:
        await manager.shutdown()


async def test_missing_tmux_uses_direct_pty(terminal_setup, monkeypatch):
    _, params = terminal_setup
    monkeypatch.setattr(TmuxTerminal, "available", staticmethod(lambda: False))
    manager = ExecutionManager(None, EventBroadcaster())
    try:
        sid = str(uuid.uuid4())
        await manager.focus(sid, **params)
        assert manager.status(sid)["backend"] == "direct"
        proc = manager._channels[sid].channel._state.proc
        await manager.shutdown()
        assert proc.returncode is not None
    finally:
        await manager.shutdown()


async def test_raw_capture_gap_fails_without_persisting_partial_answer(tmp_path):
    bus = EventBroadcaster()
    queue = bus.subscribe()
    channel = PtyChannel("gap", [], str(tmp_path), {}, bus=bus)
    channel._state.expecting_btw_response = True
    channel._state.btw_input_row_id = 42
    channel._state.btw_buffer.extend(b"incomplete response")
    channel._capture_gap()
    event = queue.get_nowait()
    assert event["status"] == "failed"
    assert event["response_id"] is None
    assert not channel._state.expecting_btw_response
    assert not channel._state.btw_buffer


@requires_tmux
async def test_replaced_pane_process_refuses_adoption(terminal_setup):
    _, params = terminal_setup
    manager = ExecutionManager(None, EventBroadcaster())
    sid = str(uuid.uuid4())
    try:
        await manager.focus(sid, **params)
        terminal = manager._channels[sid].channel._terminal
        await manager.shutdown()
        await terminal.run("respawn-pane", "-k", "-t", terminal.name, "sleep", "60")
        with pytest.raises(RuntimeError, match="process changed"):
            await terminal.managed_pid()
    finally:
        await manager.shutdown()


@requires_tmux
@pytest.mark.parametrize("provider", ["claude", "codex"])
async def test_explicit_kill_after_restart_without_focusing(terminal_setup, provider):
    name, params = terminal_setup
    sid = str(uuid.uuid4())
    first = ExecutionManager(None, EventBroadcaster())
    second = ExecutionManager(None, EventBroadcaster())
    try:
        if provider == "claude":
            await first.focus(sid, **params)
            terminal = first._channels[sid].channel._terminal
            exists = terminal.exists
        else:
            # Synthetic Codex driver created on exactly the manager socket.
            driver = TmuxDriver(
                sid, params["cwd"], [sys.executable, str(CODEX)], socket_name=name
            )
            await driver.spawn(cols=120, rows=40)
            first._drivers[sid] = driver
            first._providers[sid] = provider
            exists = driver.has_session
        await first.shutdown()
        assert await exists()
        await second.kill(
            sid, provider=provider, cwd=params["cwd"], jsonl_path=params["jsonl_path"]
        )
        assert not await exists()
    finally:
        await first.shutdown()
        await second.shutdown()


@requires_tmux
async def test_capture_start_failure_can_reattach_without_duplicate_harness(
    terminal_setup, monkeypatch
):
    _, params = terminal_setup
    manager = ExecutionManager(None, EventBroadcaster())
    sid = str(uuid.uuid4())
    original = TmuxTerminal.start_capture

    async def broken(self, *args):
        raise RuntimeError("synthetic capture failure")

    monkeypatch.setattr(TmuxTerminal, "start_capture", broken)
    with pytest.raises(RuntimeError, match="synthetic capture failure"):
        await manager.focus(sid, **params)
    terminal = TmuxTerminal(sid, params["cwd"], socket_name=manager._tmux_socket)
    pid = await terminal.managed_pid()
    assert pid is not None
    assert sid not in manager._channels
    monkeypatch.setattr(TmuxTerminal, "start_capture", original)
    try:
        await manager.focus(sid, **params)
        assert manager._channels[sid].channel._harness_pid == pid
    finally:
        await manager.shutdown()


@requires_tmux
async def test_old_capture_detach_cannot_disable_replacement_capture(terminal_setup):
    _, params = terminal_setup
    manager = ExecutionManager(None, EventBroadcaster())
    sid = str(uuid.uuid4())
    replacement = None
    try:
        await manager.focus(sid, **params)
        original = manager._channels[sid].channel._terminal
        replacement = TmuxTerminal(sid, params["cwd"], socket_name=manager._tmux_socket)
        chunks = []
        await replacement.start_capture(chunks.append, lambda: None)
        await original.stop_capture()
        await manager.submit(sid, "still captured")
        for _ in range(50):
            if chunks:
                break
            await asyncio.sleep(0.02)
        assert chunks
    finally:
        if replacement:
            await replacement.stop_capture()
        await manager.shutdown()


@requires_tmux
async def test_durable_run_survives_synthetic_tmux_restart_and_kill(
    terminal_setup, tmp_path
):
    from agent_decoder.db import Database

    _, params = terminal_setup
    sid = str(uuid.uuid4())
    async with Database(tmp_path / "history.db") as db:
        await db.init_schema()
        first = ExecutionManager(db, EventBroadcaster())
        second = ExecutionManager(db, EventBroadcaster())
        await first.initialize_runs()
        try:
            await first.focus(sid, **params)
            run_id = (await first.runs.active(sid)).id
            await first.submit(sid, "synthetic prompt")
            assert (await first.runs.active(sid)).state == "running"
            await first.shutdown()
            assert (await first.runs.active(sid)).provenance == "server_shutdown"
            await second.initialize_runs()
            assert (await second.runs.active(sid)).provenance == "server_restart"
            await second.focus(sid, **{**params, "new_chat": False})
            assert (await second.runs.active(sid)).id == run_id
            await second.kill(sid)
            run = (await second.runs.list())[0]
            assert run.id == run_id and run.state == "canceled" and run.ended_at
        finally:
            await first.shutdown()
            await second.kill(sid)
            await second.shutdown()


@requires_tmux
async def test_durable_attempt_changes_on_permission_respawn(terminal_setup, tmp_path):
    from agent_decoder.db import Database

    _, params = terminal_setup
    sid = str(uuid.uuid4())
    async with Database(tmp_path / "history.db") as db:
        await db.init_schema()
        manager = ExecutionManager(db, EventBroadcaster())
        await manager.initialize_runs()
        try:
            await manager.focus(sid, **params)
            first = await manager.runs.active(sid)
            await manager.focus(sid, **{**params, "permission_mode": "default"})
            second = await manager.runs.active(sid)
            assert second.id != first.id
            rows = await manager.runs.list()
            old = next(r for r in rows if r.id == first.id)
            assert old.state == "blocked" and old.ended_at
            assert old.provenance == "process_replaced"
        finally:
            await manager.kill(sid)
            await manager.shutdown()


@requires_tmux
async def test_durable_capture_failure_preserves_surviving_attempt(
    terminal_setup, tmp_path, monkeypatch
):
    from agent_decoder.db import Database

    _, params = terminal_setup
    sid = str(uuid.uuid4())
    async with Database(tmp_path / "history.db") as db:
        await db.init_schema()
        manager = ExecutionManager(db, EventBroadcaster())
        await manager.initialize_runs()
        original = TmuxTerminal.start_capture

        async def broken(self, *args):
            raise RuntimeError("synthetic capture failure")

        try:
            monkeypatch.setattr(TmuxTerminal, "start_capture", broken)
            with pytest.raises(RuntimeError, match="synthetic capture failure"):
                await manager.focus(sid, **params)
            run = await manager.runs.active(sid)
            assert run is not None and run.state == "blocked" and not run.ended_at
            monkeypatch.setattr(TmuxTerminal, "start_capture", original)
            await manager.focus(sid, **params)
            assert (await manager.runs.active(sid)).id == run.id
        finally:
            await manager.kill(sid)
            await manager.shutdown()


@requires_tmux
async def test_restart_with_changed_configuration_creates_new_attempt(
    terminal_setup, tmp_path
):
    from agent_decoder.db import Database

    _, params = terminal_setup
    sid = str(uuid.uuid4())
    async with Database(tmp_path / "history.db") as db:
        await db.init_schema()
        first = ExecutionManager(db, EventBroadcaster())
        second = ExecutionManager(db, EventBroadcaster())
        await first.initialize_runs()
        try:
            await first.focus(sid, **params)
            original = await first.runs.active(sid)
            await first.shutdown()
            await second.initialize_runs()
            await second.focus(sid, **{**params, "permission_mode": "default"})
            assert (await second.runs.active(sid)).id != original.id
            old = next(r for r in await second.runs.list() if r.id == original.id)
            assert old.ended_at and old.provenance == "process_replaced"
        finally:
            await first.shutdown()
            await second.kill(sid)
            await second.shutdown()
