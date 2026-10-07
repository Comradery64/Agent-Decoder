"""Phase-1 JSONL lock sidecar (pty-ownership-plan.md).

Promotes the Phase-0 hybrid pgrep+lsof detector from heuristic to
authoritative for clau-decode-managed sessions. Each ``PtyChannel``
acquires a ``<jsonl_path>.lock`` JSON sidecar on spawn and refreshes
its ``heartbeat_at`` every 30 s; ``release()`` runs on kill. A second
clau-decode (or — once Phase 2 ships — a wrapped terminal claude)
reading the sidecar can identify the existing owner by ``pid`` +
``hostname`` and decide whether to refuse the spawn or offer take-over.

Acquisition uses ``open(..., O_CREAT|O_EXCL)`` rather than the
plan's drafted "atomic rename + re-read" pattern — the rename pattern
can't detect a lost race because every writer's content wins on
``os.replace``. ``O_EXCL`` gives true exclusion at the filesystem
level. The findings doc records this deviation.

Sidecar schema (JSON, single line)::

    {
      "owner_kind": "clau-decode",
      "pid": 12345,
      "hostname": "example-host.local",
      "heartbeat_at": "2026-05-26T14:30:01.234567+00:00",
      "ui_endpoint": "http://127.0.0.1:4242"
    }

Out of scope (documented in plan):
  - NFS / network filesystems — O_EXCL semantics vary.
  - Cross-host coordination — a remote sidecar's pid can't be
    signalled from here; reader treats hostname-mismatch as "fresh
    but un-takeoverable" and surfaces in the UI.
"""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

_log = logging.getLogger(__name__)

# Heartbeat cadence + staleness threshold. The 5 min threshold tolerates
# brief event-loop stalls, swap pressure, etc., AND comfortably exceeds
# the runner's 5 min idle-kill window so an idle channel doesn't get
# flagged stale before it's killed naturally.
HEARTBEAT_INTERVAL_S = 30.0
STALE_AFTER_S = 5 * 60.0

# Lock file suffix appended to the JSONL path.
_SUFFIX = ".lock"


def _lock_path_for(jsonl_path: Path) -> Path:
    return Path(str(jsonl_path) + _SUFFIX)


def _pid_alive(pid: int) -> bool:
    """``os.kill(pid, 0)`` probe. Returns False on ESRCH; True on EPERM
    (process exists but we can't signal it — same machine, different
    user, still a real owner)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        # EPERM ⇒ the pid exists but is owned by another user.
        return exc.errno == errno.EPERM
    return True


@dataclass
class LockSidecar:
    """A live or read-back lock sidecar for a single session JSONL.

    Instances returned by ``acquire`` own the on-disk file and an
    asyncio heartbeat task started by ``start_heartbeat``. Instances
    returned by ``read`` are read-only snapshots — call ``is_fresh``
    / ``is_self`` to interpret them.
    """

    jsonl_path: Path
    owner_kind: str
    pid: int
    hostname: str
    heartbeat_at: datetime
    ui_endpoint: Optional[str]
    # Internal: True iff this instance was returned by acquire (i.e.
    # we own the file and may refresh / release it). Read-back
    # instances are inert.
    _owned: bool = False
    _heartbeat_task: Optional[asyncio.Task] = None
    _released: bool = False

    # ------------------------------------------------------------------
    # Read / freshness
    # ------------------------------------------------------------------

    @classmethod
    def read(cls, jsonl_path: Path) -> Optional["LockSidecar"]:
        """Return the on-disk sidecar, or ``None`` if missing / malformed.

        Tolerant of partial writes (the writer uses atomic rename, but
        a Phase-2 wrapper crashing mid-write could leave a half-file).
        Returns ``None`` rather than raising in that case.
        """
        lp = _lock_path_for(jsonl_path)
        try:
            raw = lp.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            _log.debug("locks: read(%s) failed: %s", lp, exc)
            return None
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        try:
            return cls(
                jsonl_path=Path(jsonl_path),
                owner_kind=str(data["owner_kind"]),
                pid=int(data["pid"]),
                hostname=str(data["hostname"]),
                heartbeat_at=datetime.fromisoformat(data["heartbeat_at"]),
                ui_endpoint=(
                    str(data["ui_endpoint"])
                    if data.get("ui_endpoint") is not None
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def is_fresh(self, *, now: Optional[datetime] = None) -> bool:
        """A lock is fresh when its heartbeat is recent AND its PID
        is alive on the right host.

        Same-host: PID-alive check is authoritative. A stale-ish
        heartbeat (>5 min) with an alive PID is still fresh — the
        owner is just busy. The threshold is a backstop for the
        cross-host case where we can't probe the PID.

        Cross-host: we can't probe the PID; rely on the heartbeat
        threshold. A lock from a different hostname older than
        ``STALE_AFTER_S`` is considered stale.
        """
        if now is None:
            now = datetime.now(timezone.utc)
        if self.hostname == socket.gethostname():
            return _pid_alive(self.pid)
        # Cross-host — heartbeat threshold only.
        age = now - self.heartbeat_at
        return age <= timedelta(seconds=STALE_AFTER_S)

    def is_stale(self, *, now: Optional[datetime] = None) -> bool:
        return not self.is_fresh(now=now)

    def is_self(self) -> bool:
        """True iff this lock was written by THIS process on THIS host."""
        return self.pid == os.getpid() and self.hostname == socket.gethostname()

    # ------------------------------------------------------------------
    # Acquire / release
    # ------------------------------------------------------------------

    @classmethod
    def acquire(
        cls,
        jsonl_path: Path,
        owner_kind: str,
        *,
        ui_endpoint: Optional[str] = None,
        max_attempts: int = 3,
    ) -> "LockSidecar":
        """Atomically create the sidecar; raise ``LockAlreadyHeld`` on
        a fresh non-self lock.

        Algorithm:
          1. ``open(O_CREAT|O_EXCL|O_WRONLY)``.
          2. If success: write payload, ``os.close``, return owned instance.
          3. If ``FileExistsError``: read the existing lock.
             - missing on re-read (race against another acquirer's
               release) → retry.
             - self → refresh heartbeat in-place, return owned.
             - fresh foreign → raise ``LockAlreadyHeld``.
             - stale → ``os.unlink`` and retry.
        """
        lp = _lock_path_for(jsonl_path)
        own_pid = os.getpid()
        own_host = socket.gethostname()

        # Make sure the parent directory exists. claude itself doesn't
        # create the projects/<cwd> dir until the first JSONL write, so
        # a brand-new session's lock would otherwise hit ENOENT.
        try:
            lp.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            _log.warning("locks: cannot mkdir %s: %s", lp.parent, exc)
            raise

        for _ in range(max_attempts):
            now = datetime.now(timezone.utc)
            payload = json.dumps(
                {
                    "owner_kind": owner_kind,
                    "pid": own_pid,
                    "hostname": own_host,
                    "heartbeat_at": now.isoformat(),
                    "ui_endpoint": ui_endpoint,
                }
            ).encode("utf-8")

            try:
                fd = os.open(
                    str(lp),
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o644,
                )
            except FileExistsError:
                existing = cls.read(jsonl_path)
                if existing is None:
                    # Concurrent release between our O_EXCL and our read.
                    continue
                if existing.is_self():
                    existing._owned = True
                    existing._refresh_to_disk(now=now, ui_endpoint=ui_endpoint)
                    return existing
                if existing.is_fresh(now=now):
                    raise LockAlreadyHeld(existing)
                # Stale: try to take it.
                try:
                    os.unlink(str(lp))
                except FileNotFoundError:
                    pass
                continue

            try:
                os.write(fd, payload)
            finally:
                os.close(fd)
            return cls(
                jsonl_path=Path(jsonl_path),
                owner_kind=owner_kind,
                pid=own_pid,
                hostname=own_host,
                heartbeat_at=now,
                ui_endpoint=ui_endpoint,
                _owned=True,
            )

        raise RuntimeError(
            f"locks: could not acquire {lp} after {max_attempts} attempts "
            f"(persistent contention or filesystem error)"
        )

    def release(self) -> None:
        """Best-effort ``os.remove``. Idempotent.

        Cancels any running heartbeat task. Safe to call from any
        teardown path (clean kill, idle-kill, shutdown, error).
        """
        if self._released:
            return
        self._released = True
        task = self._heartbeat_task
        self._heartbeat_task = None
        if task is not None and not task.done():
            task.cancel()
        if not self._owned:
            return
        lp = _lock_path_for(self.jsonl_path)
        try:
            os.unlink(str(lp))
        except FileNotFoundError:
            pass
        except OSError as exc:
            _log.warning("locks: release(%s) unlink failed: %s", lp, exc)

    # ------------------------------------------------------------------
    # Heartbeat
    # ------------------------------------------------------------------

    def start_heartbeat(self, *, interval_s: float = HEARTBEAT_INTERVAL_S) -> None:
        """Spawn the heartbeat asyncio task. Idempotent — calling twice
        on the same instance leaves the existing task in place."""
        if not self._owned:
            raise RuntimeError("locks: cannot heartbeat a read-only sidecar")
        if self._released:
            raise RuntimeError("locks: cannot heartbeat a released sidecar")
        if self._heartbeat_task is not None and not self._heartbeat_task.done():
            return
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_forever(interval_s),
            name=f"locks.heartbeat[{self.jsonl_path.name}]",
        )

    async def _heartbeat_forever(self, interval_s: float) -> None:
        try:
            while not self._released:
                try:
                    await asyncio.sleep(interval_s)
                except asyncio.CancelledError:
                    return
                if self._released:
                    return
                try:
                    self._refresh_to_disk()
                except Exception as exc:  # pragma: no cover — defensive
                    _log.warning(
                        "locks: heartbeat refresh failed for %s: %s",
                        self.jsonl_path,
                        exc,
                    )
        except asyncio.CancelledError:
            return

    def _refresh_to_disk(
        self,
        *,
        now: Optional[datetime] = None,
        ui_endpoint: Optional[str] = ...,
    ) -> None:
        """Atomically overwrite the lock file with a fresh heartbeat.

        Uses ``<lock>.tmp`` + ``os.replace`` — atomic on POSIX same-fs.
        Caller guarantees ownership (only call from an instance whose
        ``_owned`` is True).
        """
        if not self._owned:
            return
        if now is None:
            now = datetime.now(timezone.utc)
        if ui_endpoint is ...:
            ui_endpoint = self.ui_endpoint
        lp = _lock_path_for(self.jsonl_path)
        tmp = Path(str(lp) + ".tmp")
        payload = json.dumps(
            {
                "owner_kind": self.owner_kind,
                "pid": self.pid,
                "hostname": self.hostname,
                "heartbeat_at": now.isoformat(),
                "ui_endpoint": ui_endpoint,
            }
        ).encode("utf-8")
        # Open exclusive on the tmp path so two refreshes from this
        # process don't clobber each other (defensive — heartbeat is
        # single-task; this just makes the contract explicit).
        try:
            fd = os.open(
                str(tmp),
                os.O_CREAT | os.O_TRUNC | os.O_WRONLY,
                0o644,
            )
        except OSError as exc:
            _log.warning("locks: refresh tmp open(%s) failed: %s", tmp, exc)
            return
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        os.replace(str(tmp), str(lp))
        self.heartbeat_at = now
        self.ui_endpoint = ui_endpoint


class LockAlreadyHeld(RuntimeError):
    """Raised by ``LockSidecar.acquire`` when a fresh non-self lock
    already exists. The caller can inspect ``.existing`` for owner
    metadata before deciding whether to take over."""

    def __init__(self, existing: LockSidecar) -> None:
        super().__init__(
            f"lock for {existing.jsonl_path.name} held by "
            f"{existing.owner_kind}@{existing.hostname} pid {existing.pid}"
        )
        self.existing = existing
