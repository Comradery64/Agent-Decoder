"""Operations tests use synthetic history and never start a coding harness."""

import pytest
from httpx import ASGITransport, AsyncClient

from agent_decoder.db import Database
from agent_decoder.models import AppConfig, Project, Session
from agent_decoder.server import create_app


@pytest.fixture
async def client(tmp_path):
    path = tmp_path / "index.db"
    async with Database(path) as db:
        await db.init_schema()
        await db.init_schema()  # additive/idempotent schema on existing indexes
        await db.upsert_project(Project(id="p", display_name="P", raw_path="p"))
        for provider in ("claude", "codex"):
            await db.upsert_session(
                Session(
                    id=provider,
                    provider=provider,
                    project_id="p",
                    file_path=f"/{provider}.jsonl",
                    cwd="/repo",
                    git_branch="feature",
                )
            )
    app = create_app(AppConfig(data_paths=[]), path)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def test_run_identity_and_explicit_completion(client):
    for provider in ("claude", "codex"):
        r = await client.post("/api/operations/runs", json={"session_id": provider})
        assert r.status_code == 201
        run = r.json()
        assert run["provider"] == provider
        assert run["git_branch"] == "feature"
        assert run["state"] == "blocked"
        assert run["completed_at"] is None
        assert (
            await client.post("/api/operations/runs", json={"session_id": provider})
        ).status_code == 409
        finish = await client.post(
            f"/api/operations/runs/{run['id']}/finish",
            json={"state": "completed", "reason": "Reviewed output"},
        )
        assert finish.json()["completed_at"]
        assert (
            await client.post(
                f"/api/operations/runs/{run['id']}/finish",
                json={"state": "failed", "reason": "Again"},
            )
        ).status_code == 409
    assert len((await client.get("/api/operations/runs?project_id=p")).json()) == 2
    assert (await client.get("/api/operations/runs?project_id=other")).json() == []
    assert (
        await client.post("/api/operations/runs", json={"session_id": "missing"})
    ).status_code == 404
    assert (await client.get("/api/operations/runs/missing")).status_code == 404
