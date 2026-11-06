"""One owner, lock table, active terminal and lifecycle for both providers.

Provider-specific input/state policy stays separate from terminal transport.
Claude reuses its mature channel policy on tmux (direct PTY if unavailable);
Codex uses the driver policy and its own state classifier and resume argv.
"""

from __future__ import annotations

import os
import asyncio
import logging

from .agent_runs import RunStore, now

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
        self.runs = RunStore(db._db_path if db is not None else None, bus)
        self._run_lock = asyncio.Lock()
        self._run_monitor = None
        self._run_observations = {}

    def has(self, session_id):
        return session_id in self._channels or session_id in self._drivers

    def is_alive(self, session_id):
        return bool(self.status(session_id).get("alive"))

    async def initialize_runs(self):
        await self.runs.initialize()
        self._run_monitor = asyncio.create_task(self._observe_runs())

    def _record_run_observation(self, session_id, state):
        self._run_observations[session_id] = (state, now())

    async def _observe_runs(self):
        while True:
            await asyncio.sleep(2)
            try:
                async with self._run_lock:
                    await self._flush_run_observations()
            except Exception:
                logging.getLogger(__name__).exception(
                    "Run observation failed; retrying next poll"
                )

    async def _flush_run_observations(self):
        for run in await self.runs.list(active_only=True, limit=10000):
            sid = run.native_session_id
            if run.provenance in ("server_restart", "server_shutdown") and not self.has(
                sid
            ):
                continue
            if not self.is_alive(sid):
                if run.provenance != "transport_unobserved":
                    await self.runs.update(
                        sid,
                        "blocked",
                        "Terminal transport disconnected; process outcome unknown",
                        "transport_unobserved",
                    )
                continue
            observation = self._run_observations.pop(sid, None)
            if observation:
                native, stamp = observation
                state = (
                    "awaiting_input"
                    if native == "idle_chat_input"
                    else "running"
                    if native == "running"
                    else "blocked"
                )
                await self.runs.update(
                    sid,
                    state,
                    f"Advisory native screen: {native}; task outcome unknown",
                    "screen_classifier",
                    observed=True,
                    observation_at=stamp,
                )

    async def focus(self, session_id, *, provider="claude", **kwargs):
        async with self._run_lock:
            existing = await self.runs.active(session_id)
            if existing and existing.provider != provider:
                raise RuntimeError("session already belongs to another provider")
            alive = self.has(session_id) and self.is_alive(session_id)
            if existing and not alive:
                # Verify persistent process identity before reusing an attempt.
                if provider == "claude":
                    persistent = await self.managed_pane_pid(
                        session_id, kwargs.get("cwd")
                    )
                else:
                    probe = build_driver(
                        provider,
                        session_id,
                        kwargs["cwd"],
                        socket_name=self._tmux_socket,
                    )
                    persistent = await probe.has_session()
                if not persistent:
                    await self.runs.update(
                        session_id,
                        "blocked",
                        "Prior managed process no longer exists; outcome unknown",
                        "process_absent",
                        close_unknown=True,
                    )
                    existing = None
            await self.runs.begin(session_id, provider, **kwargs)
            try:
                await self._focus_execution(session_id, provider=provider, **kwargs)
            except BaseException:
                surviving = None
                if provider == "claude":
                    surviving = await self.managed_pane_pid(
                        session_id, kwargs.get("cwd")
                    )
                elif provider == "codex":
                    probe = build_driver(
                        provider,
                        session_id,
                        kwargs["cwd"],
                        socket_name=self._tmux_socket,
                    )
                    surviving = await probe.has_session()
                await self.runs.update(
                    session_id,
                    "blocked" if existing or surviving else "failed",
                    "Launch or attachment rejected; no successful start observed",
                    "launch_error",
                )
                raise
            if not alive:
                await self.runs.update(
                    session_id,
                    "blocked",
                    "Managed terminal attached; waiting for a screen observation",
                    "managed_attach",
                    observed=True,
                )

    async def _focus_execution(self, session_id, *, provider="claude", **kwargs):
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

    async def _ensure_channel_locked(self, session_id, params):
        channel = self._channels.get(session_id)
        persistent = None
        run = await self.runs.active(session_id)
        if channel is None or not channel.channel.is_alive():
            persistent = (
                await self.managed_pane_pid(session_id, params.cwd)
                if run and run.state != "starting"
                else None
            )
            if run and run.state != "starting" and not persistent:
                await self.runs.update(
                    session_id,
                    "blocked",
                    "Managed process replaced; prior outcome unknown",
                    "process_replaced",
                    close_unknown=True,
                )
                await self.runs.begin(
                    session_id, "claude", cwd=params.cwd, model=params.model
                )
        try:
            managed = await super()._ensure_channel_locked(session_id, params)
            if (
                persistent
                and run
                and run.state != "starting"
                and managed.channel._harness_pid != persistent
            ):
                await self.runs.update(
                    session_id,
                    "blocked",
                    "Launch configuration replaced persistent process; prior outcome unknown",
                    "process_replaced",
                    close_unknown=True,
                )
                await self.runs.begin(
                    session_id, "claude", cwd=params.cwd, model=params.model
                )
            return managed
        except BaseException:
            persistent = await self.managed_pane_pid(session_id, params.cwd)
            await self.runs.update(
                session_id,
                "blocked" if persistent else "failed",
                "Attachment failed; managed process survives"
                if persistent
                else "Managed process launch failed",
                "attach_error" if persistent else "launch_error",
            )
            raise

    async def submit(self, session_id, content):
        async with self._run_lock:
            return await self._submit_execution(session_id, content)

    async def _submit_execution(self, session_id, content):
        self._run_observations.pop(session_id, None)
        if session_id in self._drivers:
            result = await self._driver_submit(session_id, content)
        else:
            result = await super().submit(session_id, content)
        await self.runs.update(
            session_id,
            "running",
            "Decoded input submitted; completion not yet established",
            "decoded_submit",
            observed=True,
        )
        return result

    async def write_raw_input(self, session_id, data):
        async with self._run_lock:
            return await self._write_execution_input(session_id, data)

    async def _write_execution_input(self, session_id, data):
        if b"\r" in data or b"\n" in data:
            self._run_observations.pop(session_id, None)
        if session_id in self._drivers:
            result = await self._driver_write_raw_input(session_id, data)
        else:
            result = await super().write_raw_input(session_id, data)
        if b"\r" in data or b"\n" in data:
            await self.runs.update(
                session_id,
                "running",
                "Native Enter sent; command and outcome unverified",
                "native_input",
                observed=True,
            )
        return result

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

    async def rekey(self, old_session_id, new_session_id):
        async with self._run_lock:
            if old_session_id != new_session_id and await self.runs.active(
                new_session_id
            ):
                raise RuntimeError("cannot adopt a session with an active run")
            result = await super().rekey(old_session_id, new_session_id)
            if result:
                await self.runs.rekey(old_session_id, new_session_id)
            return result

    async def kill(self, session_id, *, provider=None, cwd=None, jsonl_path=None):
        async with self._run_lock:
            await self._kill_execution(
                session_id, provider=provider, cwd=cwd, jsonl_path=jsonl_path
            )
            await self.runs.update(
                session_id,
                "canceled",
                "User explicitly terminated managed execution",
                "explicit_kill",
            )

    async def _kill_execution(
        self, session_id, *, provider=None, cwd=None, jsonl_path=None
    ):
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
        if self._run_monitor:
            self._run_monitor.cancel()
            await asyncio.gather(self._run_monitor, return_exceptions=True)
        for run in await self.runs.list(active_only=True, limit=10000):
            if not run.ended_at:
                await self.runs.update(
                    run.native_session_id,
                    "blocked",
                    "Server detached; process outcome unobserved",
                    "server_shutdown",
                    close_unknown=self.status(run.native_session_id).get("backend")
                    == "direct"
                    and self.has(run.native_session_id),
                )
        try:
            await super().shutdown()
        finally:
            await self._driver_shutdown()
