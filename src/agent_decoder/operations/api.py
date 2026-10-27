"""Operations API. Recording a run never starts or sends input to a harness."""

from pathlib import Path

from fastapi import APIRouter, HTTPException

from ..db import Database
from ..events_bus import EventBroadcaster
from .models import AgentRun, CreateRun, FinishRun, TERMINAL_STATES, now
from .store import OperationsStore


def operations_router(path: Path, bus: EventBroadcaster) -> APIRouter:
    router = APIRouter(prefix="/api/operations", tags=["operations"])

    @router.get("/runs")
    async def runs(project_id: str | None = None):
        async with OperationsStore.open(path) as store:
            records = await store.list("run")
        return [
            r for r in records if project_id is None or r["project_id"] == project_id
        ]

    @router.post("/runs", status_code=201)
    async def create_run(req: CreateRun):
        async with Database(path) as db:
            session = await db.get_session_detail(req.session_id)
        if session is None:
            raise HTTPException(404, "Indexed session not found")
        async with OperationsStore.open(path) as store:
            for existing in await store.list("run"):
                if (
                    existing["session_id"] == req.session_id
                    and existing["state"] not in TERMINAL_STATES
                ):
                    raise HTTPException(409, "Session already has an open run")
            run = AgentRun(
                provider=session.provider,
                session_id=session.id,
                project_id=session.project_id,
                model=session.model,
                cwd=session.cwd,
                git_branch=session.git_branch,
                is_worktree=session.is_worktree,
            )
            await store.put("run", run)
        bus.publish({"type": "operations_changed", "run_id": run.id})
        return run

    @router.get("/runs/{run_id}")
    async def run_detail(run_id: str):
        async with OperationsStore.open(path) as store:
            run = await store.get("run", run_id)
        if run is None:
            raise HTTPException(404, "Run not found")
        return run

    @router.post("/runs/{run_id}/finish")
    async def finish_run(run_id: str, req: FinishRun):
        async with OperationsStore.open(path) as store:
            data = await store.get("run", run_id)
            if data is None:
                raise HTTPException(404, "Run not found")
            run = AgentRun(**data)
            if run.state in TERMINAL_STATES:
                raise HTTPException(409, "Run is already finished")
            run.state, run.state_reason = req.state, req.reason
            run.completed_at = run.updated_at = now()
            await store.put("run", run)
        bus.publish({"type": "operations_changed", "run_id": run.id})
        return run

    return router
