"""Run history must survive index churn and never infer task success."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from agent_decoder.agent_runs import RunStore
from agent_decoder.db import Database
from agent_decoder.events_bus import EventBroadcaster
from agent_decoder.execution_manager import ExecutionManager


@pytest.fixture
async def database(tmp_path):
    async with Database(tmp_path / "runs.db") as db:
        await db.init_schema()
        yield db


async def test_concurrent_begin_reuses_identity_and_terminal_attempt_is_not_resurrected(
    database,
):
    store = RunStore(database._db_path)
    await store.initialize()
    first, second = await asyncio.gather(
        store.begin("session", "claude"), store.begin("session", "claude")
    )
    assert first.id == second.id
    await store.update("session", "canceled", "user stop", "explicit_kill")
    await store.update("session", "running", "late callback", "native_input")
    rows = await store.list()
    assert rows[0].state == "canceled"
    assert rows[0].ended_at
    assert (await store.begin("session", "claude")).id != first.id
    assert len(await store.list()) == 2


async def test_restart_and_rekey_preserve_history_without_source(database):
    store = RunStore(database._db_path)
    await store.initialize()
    run = await store.begin("placeholder", "codex", cwd="/tmp/project", model="test")
    await store.update(
        "placeholder", "running", "submitted", "decoded_submit", observed=True
    )
    await store.rekey("placeholder", "native-id")
    await store.initialize()
    recovered = (await store.list())[0]
    assert recovered.id == run.id
    assert recovered.native_session_id == "native-id"
    assert recovered.state == "blocked"
    assert recovered.provenance == "server_restart"
    assert not recovered.source_available
    assert recovered.last_observed_at
    assert recovered.task_id is None and recovered.parent_run_id is None


async def test_manager_commits_submit_native_and_explicit_kill(database, monkeypatch):
    manager = ExecutionManager(database, EventBroadcaster())
    await manager.initialize_runs()
    monkeypatch.setattr(manager, "_focus_execution", AsyncMock())
    monkeypatch.setattr(manager, "_driver_submit", AsyncMock())
    monkeypatch.setattr(manager, "_driver_write_raw_input", AsyncMock())
    monkeypatch.setattr(manager, "_kill_execution", AsyncMock())
    try:
        await manager.focus("s", provider="codex", cwd="/tmp")
        manager._drivers["s"] = object()
        await manager.submit("s", "hello")
        assert (await manager.runs.active("s")).provenance == "decoded_submit"
        await manager.write_raw_input("s", b"\r")
        assert (await manager.runs.active("s")).provenance == "native_input"
        manager._drivers.clear()
        await manager.kill("s")
        assert (await manager.runs.list())[0].state == "canceled"
    finally:
        await manager.shutdown()


async def test_launch_rejection_is_terminal_failure(database, monkeypatch):
    manager = ExecutionManager(database, EventBroadcaster())
    await manager.initialize_runs()
    monkeypatch.setattr(
        manager, "_focus_execution", AsyncMock(side_effect=RuntimeError("rejected"))
    )
    try:
        with pytest.raises(RuntimeError, match="rejected"):
            await manager.focus("s", cwd="/tmp")
        run = (await manager.runs.list())[0]
        assert run.state == "failed" and run.ended_at
    finally:
        await manager.shutdown()


async def test_shutdown_records_uncertainty_and_never_completed(database, monkeypatch):
    manager = ExecutionManager(database, EventBroadcaster())
    await manager.initialize_runs()
    monkeypatch.setattr(manager, "_focus_execution", AsyncMock())
    await manager.focus("s", cwd="/tmp")
    await manager.shutdown()
    run = (await manager.runs.list())[0]
    assert run.state == "blocked"
    assert run.ended_at is None
    assert run.provenance == "server_shutdown"


async def test_cross_store_begin_is_atomic_and_provider_scoped(database):
    stores = [RunStore(database._db_path), RunStore(database._db_path)]
    await stores[0].initialize()
    a, b = await asyncio.gather(*(store.begin("same", "codex") for store in stores))
    assert a.id == b.id
    other = await stores[1].begin("same", "claude")
    assert other.provider == "claude" and other.id != a.id


async def test_filters_before_pagination_and_detail_outside_page(database):
    store = RunStore(database._db_path)
    await store.initialize()
    first = await store.begin("old-codex", "codex", cwd="/project/wanted")
    for i in range(4):
        await store.begin(f"new-claude-{i}", "claude")
    assert (await store.list(provider="codex", q="wanted", limit=1))[0].id == first.id
    assert (await store.list(run_id=first.id, limit=1))[0].id == first.id
    assert len(await store.list(limit=2, offset=2)) == 2


async def test_advisory_screen_and_disconnect_never_complete(database, monkeypatch):
    manager = ExecutionManager(database, EventBroadcaster())
    await manager.initialize_runs()
    monkeypatch.setattr(manager, "_focus_execution", AsyncMock())
    monkeypatch.setattr(manager, "has", lambda sid: True)
    monkeypatch.setattr(manager, "is_alive", lambda sid: True)
    try:
        await manager.focus("s", cwd="/tmp")
        await manager.runs.update("s", "running", "input", "decoded_submit")
        manager._record_run_observation("s", "idle_chat_input")
        await manager._flush_run_observations()
        run = await manager.runs.active("s")
        assert run.state == "awaiting_input"
        assert run.provenance == "screen_classifier"
        observed = run.last_observed_at
        await manager._flush_run_observations()
        assert (await manager.runs.active("s")).last_observed_at == observed
        manager._record_run_observation("s", "permission_prompt")
        await manager._flush_run_observations()
        assert (await manager.runs.active("s")).state == "blocked"
        monkeypatch.setattr(manager, "is_alive", lambda sid: False)
        await manager._flush_run_observations()
        assert (await manager.runs.active("s")).provenance == "transport_unobserved"
    finally:
        await manager.shutdown()


async def test_index_enrichment_survives_session_deletion(database):
    from agent_decoder.models import Project, Session

    store = RunStore(database._db_path)
    await store.initialize()
    await store.begin("native", "codex")
    await database.upsert_project(
        Project(id="p", display_name="Project", raw_path="/project", data_source="test")
    )
    await database.upsert_session(
        Session(
            id="native",
            project_id="p",
            file_path="/tmp/rollout.jsonl",
            provider="codex",
            cwd="/project/worktree",
            git_branch="feature",
            is_worktree=True,
            model="test-model",
        )
    )
    run = (await store.list())[0]
    assert run.project_id == "p" and run.git_branch == "feature" and run.is_worktree
    assert run.source_available
    await database.delete_session("native")
    run = (await store.list())[0]
    assert not run.source_available
    assert run.model == "test-model" and run.git_branch == "feature"


async def test_execution_model_is_not_overwritten_by_session_model(database):
    from agent_decoder.models import Project, Session

    store = RunStore(database._db_path)
    await store.initialize()
    await store.begin("s", "claude", model="requested-new")
    await database.upsert_project(
        Project(id="p", display_name="Project", raw_path="/p", data_source="test")
    )
    await database.upsert_session(
        Session(
            id="s", project_id="p", file_path="/tmp/session.jsonl", model="logged-old"
        )
    )
    assert (await store.list())[0].model == "requested-new"
    await store.update("s", "canceled", "stop", "explicit_kill")
    await database.upsert_session(
        Session(
            id="s",
            project_id="p",
            file_path="/tmp/session.jsonl",
            model="another-model",
        )
    )
    assert (await store.list())[0].model == "requested-new"


async def test_runs_api_filters_and_missing_detail(database):
    from httpx import ASGITransport, AsyncClient
    from agent_decoder.models import AppConfig
    from agent_decoder.server import create_app

    store = RunStore(database._db_path)
    await store.initialize()
    run = await store.begin("s", "codex", cwd="/wanted")
    await store.begin("newer", "claude")
    app = create_app(AppConfig(), database._db_path)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/api/runs", params={"provider": "codex", "limit": 1, "q": "wanted"}
        )
        assert response.status_code == 200
        assert response.json()[0]["id"] == run.id
        assert (await client.get(f"/api/runs/{run.id}")).json()[
            "native_session_id"
        ] == "s"
        assert (await client.get("/api/runs/missing")).status_code == 404
        assert (await client.get("/api/runs?limit=0")).status_code == 422


async def test_rekey_enriches_already_indexed_source_before_deletion(database):
    from agent_decoder.models import Project, Session

    store = RunStore(database._db_path)
    await store.initialize()
    await database.upsert_project(
        Project(
            id="adopted-project",
            display_name="Adopted",
            raw_path="/adopt",
            data_source="test",
        )
    )
    await database.upsert_session(
        Session(
            id="real-id",
            project_id="adopted-project",
            file_path="/tmp/rollout.jsonl",
            provider="codex",
            model="observed-model",
            cwd="/adopt",
            git_branch="adopted-branch",
            is_worktree=True,
        )
    )
    pending = await store.begin("placeholder", "codex")
    await store.rekey("placeholder", "real-id")
    assert (await store.list(project="adopted-project", q="adopted-branch"))[
        0
    ].id == pending.id
    await database.delete_session("real-id")
    run = (await store.list(project="adopted-project", q="adopted-branch"))[0]
    assert (
        run.id == pending.id and run.model == "observed-model" and run.cwd == "/adopt"
    )
    assert run.is_worktree and not run.source_available
