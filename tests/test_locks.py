"""Tests for ``agent_decoder.locks`` — Phase-1 lock sidecar.

Covers:
  - acquire/release happy path (file appears, file disappears).
  - O_EXCL atomic acquire blocks a fresh non-self caller.
  - is_self / is_fresh / is_stale signal contract.
  - stale-lock take-over: pid-dead sidecar is silently replaced.
  - hostname-mismatch sidecars are fresh but un-takeoverable from here
    (we deliberately don't blow them away — cross-host coordination is
    documented out of scope).
  - heartbeat refresh updates the on-disk timestamp.
"""

from __future__ import annotations

import asyncio
import errno
import json
import os
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_decoder.locks import (
    HEARTBEAT_INTERVAL_S,
    STALE_AFTER_S,
    LockAlreadyHeld,
    LockSidecar,
    _lock_path_for,
)


def _jsonl(tmp_path: Path, name: str = "abc.jsonl") -> Path:
    p = tmp_path / name
    p.write_text("")  # claude would have written something; doesn't matter here
    return p


def test_acquire_release_round_trip(tmp_path):
    """Sidecar file appears on acquire and disappears on release."""
    jsonl = _jsonl(tmp_path)
    lp = _lock_path_for(jsonl)
    assert not lp.exists()

    sc = LockSidecar.acquire(jsonl, owner_kind="agent-decoder")
    assert lp.exists()
    payload = json.loads(lp.read_text())
    assert payload["owner_kind"] == "agent-decoder"
    assert payload["pid"] == os.getpid()
    assert payload["hostname"] == socket.gethostname()
    assert payload["ui_endpoint"] is None

    sc.release()
    assert not lp.exists()
    # Double release is a no-op.
    sc.release()


def test_acquire_writes_ui_endpoint(tmp_path):
    jsonl = _jsonl(tmp_path)
    try:
        sc = LockSidecar.acquire(
            jsonl,
            owner_kind="agent-decoder",
            ui_endpoint="http://127.0.0.1:4242",
        )
        payload = json.loads(_lock_path_for(jsonl).read_text())
        assert payload["ui_endpoint"] == "http://127.0.0.1:4242"
    finally:
        sc.release()


def test_is_self_when_pid_and_host_match(tmp_path):
    jsonl = _jsonl(tmp_path)
    sc = LockSidecar.acquire(jsonl, owner_kind="agent-decoder")
    try:
        assert sc.is_self()
        # Read-back instance also matches because we're still the same process.
        rb = LockSidecar.read(jsonl)
        assert rb is not None
        assert rb.is_self()
    finally:
        sc.release()


def test_read_returns_none_when_missing(tmp_path):
    jsonl = _jsonl(tmp_path)
    assert LockSidecar.read(jsonl) is None


def test_read_returns_none_on_malformed_json(tmp_path):
    jsonl = _jsonl(tmp_path)
    _lock_path_for(jsonl).write_text("not-json")
    assert LockSidecar.read(jsonl) is None


def test_acquire_raises_lock_already_held_for_fresh_foreign_lock(tmp_path):
    """Hand-craft a sidecar that looks foreign (a pid likely to be alive
    AND a different hostname so the same-host PID-alive probe doesn't
    fire) — acquire from us must raise."""
    jsonl = _jsonl(tmp_path)
    _lock_path_for(jsonl).write_text(
        json.dumps(
            {
                "owner_kind": "agent-decoder",
                "pid": 1,  # init — alive but not ours
                "hostname": socket.gethostname() + "-other",  # foreign host
                "heartbeat_at": datetime.now(timezone.utc).isoformat(),
                "ui_endpoint": "http://other.host:4242",
            }
        )
    )
    with pytest.raises(LockAlreadyHeld) as exc:
        LockSidecar.acquire(jsonl, owner_kind="agent-decoder")
    assert exc.value.existing.pid == 1
    assert exc.value.existing.hostname.endswith("-other")


def test_acquire_takes_over_stale_lock_with_dead_pid(tmp_path):
    """Same-host sidecar whose PID is dead is silently replaced."""
    jsonl = _jsonl(tmp_path)
    # Use a pid that's certainly dead on this box — pid 999999 hits
    # ESRCH on macOS + Linux.
    dead_pid = 999_999
    _lock_path_for(jsonl).write_text(
        json.dumps(
            {
                "owner_kind": "agent-decoder",
                "pid": dead_pid,
                "hostname": socket.gethostname(),  # same host so PID probe fires
                "heartbeat_at": datetime.now(timezone.utc).isoformat(),
                "ui_endpoint": None,
            }
        )
    )
    # The pre-existing sidecar should look stale.
    pre = LockSidecar.read(jsonl)
    assert pre is not None and pre.is_stale()

    # acquire takes it over silently.
    sc = LockSidecar.acquire(jsonl, owner_kind="agent-decoder")
    try:
        assert sc.pid == os.getpid()
        payload = json.loads(_lock_path_for(jsonl).read_text())
        assert payload["pid"] == os.getpid()
    finally:
        sc.release()


def test_same_host_eperm_pid_is_fresh(tmp_path, monkeypatch):
    """EPERM from kill(pid, 0) means the process exists but is not ours."""
    jsonl = _jsonl(tmp_path)
    _lock_path_for(jsonl).write_text(
        json.dumps(
            {
                "owner_kind": "agent-decoder",
                "pid": 12345,
                "hostname": socket.gethostname(),
                "heartbeat_at": datetime.now(timezone.utc).isoformat(),
                "ui_endpoint": None,
            }
        )
    )

    def fake_kill(pid: int, sig: int) -> None:
        assert pid == 12345
        assert sig == 0
        raise OSError(errno.EPERM, "operation not permitted")

    monkeypatch.setattr(os, "kill", fake_kill)

    sc = LockSidecar.read(jsonl)
    assert sc is not None
    assert sc.is_fresh()
    assert not sc.is_stale()


def test_acquire_takes_over_when_cross_host_heartbeat_expired(tmp_path):
    """A cross-host sidecar older than STALE_AFTER_S is takeoverable."""
    jsonl = _jsonl(tmp_path)
    old = datetime.now(timezone.utc) - timedelta(seconds=STALE_AFTER_S + 60)
    _lock_path_for(jsonl).write_text(
        json.dumps(
            {
                "owner_kind": "agent-decoder",
                "pid": 1,
                "hostname": socket.gethostname() + "-other",
                "heartbeat_at": old.isoformat(),
                "ui_endpoint": None,
            }
        )
    )
    sc = LockSidecar.acquire(jsonl, owner_kind="agent-decoder")
    try:
        payload = json.loads(_lock_path_for(jsonl).read_text())
        assert payload["pid"] == os.getpid()
        assert payload["hostname"] == socket.gethostname()
    finally:
        sc.release()


def test_acquire_returns_existing_self_lock_with_refreshed_heartbeat(tmp_path):
    """If we acquire twice in the same process, the second call refreshes
    the heartbeat in place rather than raising."""
    jsonl = _jsonl(tmp_path)
    sc1 = LockSidecar.acquire(jsonl, owner_kind="agent-decoder")
    try:
        ts1 = sc1.heartbeat_at
        # Tiny sleep so timestamps strictly differ.
        import time as _t

        _t.sleep(0.01)
        sc2 = LockSidecar.acquire(jsonl, owner_kind="agent-decoder")
        try:
            assert sc2.pid == os.getpid()
            payload = json.loads(_lock_path_for(jsonl).read_text())
            ts2 = datetime.fromisoformat(payload["heartbeat_at"])
            assert ts2 > ts1, "second acquire should bump heartbeat"
        finally:
            sc2.release()
    finally:
        # First release may run after second's already removed the file;
        # both must be idempotent.
        sc1.release()


async def test_heartbeat_task_refreshes_timestamp(tmp_path):
    """``start_heartbeat`` updates the on-disk heartbeat_at periodically.

    Uses a 50 ms interval so the test runs quickly. Verifies BOTH the
    in-memory ``heartbeat_at`` attribute AND the on-disk JSON tick.
    """
    jsonl = _jsonl(tmp_path)
    sc = LockSidecar.acquire(jsonl, owner_kind="agent-decoder")
    try:
        before = sc.heartbeat_at
        sc.start_heartbeat(interval_s=0.05)
        await asyncio.sleep(0.2)  # 3-4 ticks
        after = sc.heartbeat_at
        assert after > before, "in-memory heartbeat_at should advance"
        payload = json.loads(_lock_path_for(jsonl).read_text())
        disk_ts = datetime.fromisoformat(payload["heartbeat_at"])
        assert disk_ts >= after - timedelta(seconds=0.1)
    finally:
        sc.release()


async def test_release_cancels_running_heartbeat_task(tmp_path):
    jsonl = _jsonl(tmp_path)
    sc = LockSidecar.acquire(jsonl, owner_kind="agent-decoder")
    sc.start_heartbeat(interval_s=0.05)
    task = sc._heartbeat_task
    assert task is not None and not task.done()
    sc.release()
    # Give the loop a tick for the cancellation to propagate.
    await asyncio.sleep(0.01)
    assert task.cancelled() or task.done()


def test_release_is_idempotent_on_read_only_instance(tmp_path):
    jsonl = _jsonl(tmp_path)
    sc1 = LockSidecar.acquire(jsonl, owner_kind="agent-decoder")
    try:
        rb = LockSidecar.read(jsonl)
        # rb is read-only — release must NOT unlink the file out from
        # under sc1.
        rb.release()
        assert _lock_path_for(jsonl).exists()
    finally:
        sc1.release()


def test_heartbeat_constants_are_sane():
    """Belt-and-suspenders sanity check on the module constants."""
    assert HEARTBEAT_INTERVAL_S > 0
    assert STALE_AFTER_S > HEARTBEAT_INTERVAL_S
