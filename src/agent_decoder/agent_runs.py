"""Durable managed execution attempts, independent of the session index.

A run is one managed process attempt, not a turn or a task. No screen or exit
code establishes successful completion. Detached attempts retain their identity.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

import aiosqlite
from pydantic import BaseModel

RunState = Literal[
    "queued",
    "starting",
    "running",
    "awaiting_input",
    "blocked",
    "completed",
    "failed",
    "canceled",
]
TERMINAL = ("completed", "failed", "canceled")


RUN_SCHEMA = """
                CREATE TABLE IF NOT EXISTS agent_runs (
                    id TEXT PRIMARY KEY, provider TEXT NOT NULL,
                    native_session_id TEXT NOT NULL, model TEXT, project_id TEXT,
                    cwd TEXT, git_branch TEXT, is_worktree INTEGER NOT NULL DEFAULT 0,
                    task_id TEXT, parent_run_id TEXT, state TEXT NOT NULL CHECK(state IN ('queued','starting','running','awaiting_input','blocked','completed','failed','canceled')),
                    reason TEXT NOT NULL, provenance TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    last_observed_at TEXT, ended_at TEXT);
                CREATE UNIQUE INDEX IF NOT EXISTS agent_runs_active
                  ON agent_runs(provider, native_session_id)
                  WHERE ended_at IS NULL;
            """


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AgentRun(BaseModel):
    id: str
    provider: str
    native_session_id: str
    model: str | None = None
    project_id: str | None = None
    cwd: str | None = None
    git_branch: str | None = None
    is_worktree: bool = False
    task_id: str | None = None
    parent_run_id: str | None = None
    state: RunState
    reason: str
    provenance: str
    created_at: str
    updated_at: str
    last_observed_at: str | None = None
    ended_at: str | None = None
    source_available: bool = False


class RunStore:
    def __init__(self, path: Path | None, bus=None):
        self.path, self.bus = path, bus
        self.lock = asyncio.Lock()

    async def initialize(self):
        if self.path is None:
            return
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(RUN_SCHEMA)
            await db.execute(
                "UPDATE agent_runs SET state='blocked', reason='Server restarted; process not yet reobserved', provenance='server_restart', updated_at=? WHERE ended_at IS NULL",
                (now(),),
            )
            await db.commit()

    async def list(
        self,
        *,
        limit: int = 200,
        offset: int = 0,
        active_only: bool = False,
        session_id: str | None = None,
        run_id: str | None = None,
        provider: str | None = None,
        state: str | None = None,
        project: str | None = None,
        q: str = "",
    ) -> list[AgentRun]:
        if self.path is None:
            return []
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            clauses, params = [], []
            if active_only:
                clauses.append("r.ended_at IS NULL")
            if session_id is not None:
                clauses.append("r.native_session_id=?")
                params.append(session_id)
            if run_id is not None:
                clauses.append("r.id=?")
                params.append(run_id)
            for column, value in (
                ("provider", provider),
                ("state", state),
                ("project_id", project),
            ):
                if value:
                    clauses.append(f"r.{column}=?")
                    params.append(value)
            if q:
                clauses.append(
                    "lower(coalesce(r.cwd,'') || ' ' || coalesce(r.project_id,'') || ' ' || coalesce(r.model,'') || ' ' || coalesce(r.git_branch,'') || ' ' || r.native_session_id || ' ' || r.id) LIKE ?"
                )
                params.append("%" + q.lower() + "%")
            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            rows = await db.execute(
                """SELECT r.id, r.provider, r.native_session_id,
              coalesce(r.model,s.model) AS model, coalesce(r.project_id,s.project_id) AS project_id,
              coalesce(r.cwd,s.cwd) AS cwd, coalesce(r.git_branch,s.git_branch) AS git_branch,
              CASE WHEN r.project_id IS NULL THEN coalesce(s.is_worktree,r.is_worktree) ELSE r.is_worktree END AS is_worktree,
              r.task_id,r.parent_run_id,r.state,r.reason,r.provenance,r.created_at,r.updated_at,
              r.last_observed_at,r.ended_at,(s.id IS NOT NULL) AS source_available
              FROM agent_runs r LEFT JOIN sessions s ON s.id=r.native_session_id"""
                + where
                + " ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            )
            return [AgentRun(**dict(row)) for row in await rows.fetchall()]

    async def active(self, session_id: str, provider: str | None = None):
        return next(
            iter(
                await self.list(
                    active_only=True, session_id=session_id, provider=provider, limit=1
                )
            ),
            None,
        )

    async def begin(self, session_id: str, provider: str, **metadata):
        if self.path is None:
            return None
        async with self.lock:
            stamp = now()
            async with aiosqlite.connect(self.path) as db:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    "SELECT id FROM agent_runs WHERE provider=? AND native_session_id=? AND ended_at IS NULL",
                    (provider, session_id),
                )
                if await cursor.fetchone():
                    await db.rollback()
                    return await self.active(session_id, provider)
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    "SELECT model, project_id, cwd, git_branch, is_worktree FROM sessions WHERE id=?",
                    (session_id,),
                )
                row = await cursor.fetchone()
                values = dict(row) if row else {}
                for key in ("model", "cwd"):
                    if metadata.get(key) is not None:
                        values[key] = metadata[key]
                run = AgentRun(
                    id=str(uuid4()),
                    provider=provider,
                    native_session_id=session_id,
                    state="starting",
                    reason="Managed launch or attachment requested",
                    provenance="execution_manager",
                    created_at=stamp,
                    updated_at=stamp,
                    **values,
                )
                data = run.model_dump(exclude={"source_available"})
                await db.execute(
                    f"INSERT INTO agent_runs ({','.join(data)}) VALUES ({','.join('?' for _ in data)})",
                    tuple(data.values()),
                )
                await db.commit()
            self.changed()
            return run

    def changed(self):
        if self.bus:
            self.bus.publish({"type": "runs_changed"})

    async def update(
        self,
        session_id: str,
        state: RunState,
        reason: str,
        provenance: str,
        *,
        observed=False,
        close_unknown=False,
        observation_at: str | None = None,
    ):
        async with self.lock:
            run = await self.active(session_id)
            if not run:
                return
            stamp = now()
            async with aiosqlite.connect(self.path) as db:
                await db.execute(
                    "UPDATE agent_runs SET model=?,project_id=?,cwd=?,git_branch=?,is_worktree=? WHERE id=?",
                    (
                        run.model,
                        run.project_id,
                        run.cwd,
                        run.git_branch,
                        run.is_worktree,
                        run.id,
                    ),
                )
                await db.execute(
                    """UPDATE agent_runs SET state=?,reason=?,provenance=?,updated_at=?,
                  last_observed_at=?,ended_at=? WHERE id=? AND ended_at IS NULL""",
                    (
                        state,
                        reason,
                        provenance,
                        stamp,
                        (observation_at or stamp) if observed else run.last_observed_at,
                        stamp if state in TERMINAL or close_unknown else None,
                        run.id,
                    ),
                )
                await db.commit()
            self.changed()

    async def rekey(self, old: str, new: str):
        if self.path is None:
            return
        async with self.lock:
            async with aiosqlite.connect(self.path) as db:
                await db.execute(
                    "UPDATE agent_runs SET native_session_id=?,updated_at=? WHERE native_session_id=? AND ended_at IS NULL",
                    (new, now(), old),
                )
                cursor = await db.execute(
                    "SELECT model,project_id,cwd,git_branch,is_worktree FROM sessions WHERE id=?",
                    (new,),
                )
                source = await cursor.fetchone()
                if source:
                    await db.execute(
                        """UPDATE agent_runs SET model=coalesce(model,?),project_id=coalesce(project_id,?),
                           cwd=coalesce(cwd,?),git_branch=coalesce(git_branch,?),
                           is_worktree=CASE WHEN project_id IS NULL THEN ? ELSE is_worktree END
                           WHERE native_session_id=? AND ended_at IS NULL""",
                        (*source, new),
                    )
                await db.commit()
            self.changed()
