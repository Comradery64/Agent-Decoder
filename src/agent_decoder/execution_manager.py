"""One owner, lock table, active terminal and lifecycle for both providers.

Provider-specific input/state policy stays separate from terminal transport.
Claude reuses its mature channel policy on tmux (direct PTY if unavailable);
Codex uses the driver policy and its own state classifier and resume argv.
"""

from __future__ import annotations

import os

from .driver_manager import DriverSessionPolicy
from .pty_runner import (
    PtyManager,
    PtyOwnershipConflict,
    _OWN_CLAUDE_PIDS,
    _fresh_foreign_sidecar,
)
from .tmux_terminal import TmuxTerminal
from .drivers import build_driver
from .locks import LockSidecar
from .pty_runner import _session_conflict_pids


class ExecutionManager(PtyManager, DriverSessionPolicy):
    def __init__(self, db, bus, *, terminal_backend="tmux", **kwargs):
        kwargs.setdefault(
            "tmux_socket", os.environ.get("AGENT_DECODER_TMUX_SOCKET", "agent-decoder")
        )
        super().__init__(db, bus, terminal_backend=terminal_backend, **kwargs)
        self._init_driver_sessions()

    def has(self, session_id):
        return session_id in self._channels or session_id in self._drivers

    def is_alive(self, session_id):
        return bool(self.status(session_id).get("alive"))

    async def focus(self, session_id, *, provider="claude", **kwargs):
        if self._shutdown_started:
            raise RuntimeError("ExecutionManager is shutting down")
        if provider != "claude":
            if session_id in self._channels:
                raise RuntimeError("session already belongs to Claude")
            await self._driver_focus(session_id, provider=provider, **kwargs)
            return
        if session_id in self._drivers:
            raise RuntimeError("session already belongs to another provider")
        admitted_pid = None
        if self._terminal_backend == "tmux" and TmuxTerminal.available():
            terminal = TmuxTerminal(
                session_id, kwargs["cwd"], socket_name=self._tmux_socket
            )
            admitted_pid = await terminal.managed_pid()
            if admitted_pid is not None:
                sc = _fresh_foreign_sidecar(kwargs.get("jsonl_path"))
                if sc is not None:
                    raise PtyOwnershipConflict([sc.pid])
                _OWN_CLAUDE_PIDS.add(admitted_pid)
        try:
            await super().focus(session_id, **kwargs)
            managed = self._channels.get(session_id)
            if (
                admitted_pid is not None
                and managed is not None
                and managed.channel._harness_pid != admitted_pid
            ):
                _OWN_CLAUDE_PIDS.discard(admitted_pid)
        except BaseException:
            if admitted_pid is not None and session_id not in self._channels:
                _OWN_CLAUDE_PIDS.discard(admitted_pid)
            raise

    async def submit(self, session_id, content):
        if session_id in self._drivers:
            return await self._driver_submit(session_id, content)
        return await super().submit(session_id, content)

    async def write_raw_input(self, session_id, data):
        if session_id in self._drivers:
            return await self._driver_write_raw_input(session_id, data)
        return await super().write_raw_input(session_id, data)

    async def resize(self, session_id, rows, cols):
        if session_id in self._drivers:
            return await self._driver_resize(session_id, rows, cols)
        return await super().resize(session_id, rows, cols)

    async def native_snapshot(self, session_id):
        if session_id in self._drivers:
            return await self._driver_native_snapshot(session_id)
        return super().native_snapshot(session_id)

    def status(self, session_id):
        if session_id in self._drivers:
            return self._driver_status(session_id)
        result = super().status(session_id)
        managed = self._channels.get(session_id)
        result["provider"] = "claude"
        result["backend"] = (
            "tmux" if managed and managed.channel._terminal else "direct"
        )
        return result

    async def managed_pane_pid(self, session_id, cwd):
        """Read-only identity probe; never creates a terminal or claims a lock."""
        if not cwd or self._terminal_backend != "tmux" or not TmuxTerminal.available():
            return None
        terminal = TmuxTerminal(session_id, cwd, socket_name=self._tmux_socket)
        try:
            return await terminal.managed_pid()
        except RuntimeError:
            # Unverified terminals remain foreign to read-only ownership UI.
            return None

    async def ownership(self, session_id, jsonl_path, *, cwd=None):
        result = super().ownership(session_id, jsonl_path)
        pid = await self.managed_pane_pid(session_id, cwd)
        if pid is not None:
            result["foreign_pids"] = [p for p in result["foreign_pids"] if p != pid]
            if not result["foreign_pids"] and result["foreign_owner"] is None:
                result["status"] = "ours"
        return result

    async def kill(self, session_id, *, provider=None, cwd=None, jsonl_path=None):
        """Terminate tracked or verified persistent sessions; never spawn one."""
        if session_id in self._drivers:
            return await self._driver_kill(session_id)
        if session_id in self._channels or provider is None or cwd is None:
            return await super().kill(session_id)
        async with self._session_lock(session_id):
            if provider == "codex":
                driver = build_driver(
                    provider, session_id, cwd, socket_name=self._tmux_socket
                )
                if await driver.has_session():
                    await driver.kill()
                return
            if self._terminal_backend != "tmux" or not TmuxTerminal.available():
                return
            terminal = TmuxTerminal(session_id, cwd, socket_name=self._tmux_socket)
            pid = await terminal.managed_pid()
            if pid is None:
                return
            sidecar = _fresh_foreign_sidecar(jsonl_path)
            foreign = set(_session_conflict_pids(session_id, jsonl_path)) - {pid}
            if sidecar is not None or foreign:
                raise PtyOwnershipConflict(sorted(foreign or {sidecar.pid}))
            lock = (
                LockSidecar.acquire(
                    jsonl_path,
                    owner_kind="agent-decoder",
                    ui_endpoint=self._ui_endpoint,
                )
                if jsonl_path
                else None
            )
            try:
                await terminal.terminate()
            finally:
                if lock is not None:
                    lock.release()

    async def shutdown(self):
        if self._shutdown_started:
            return
        try:
            await super().shutdown()
        finally:
            await self._driver_shutdown()
