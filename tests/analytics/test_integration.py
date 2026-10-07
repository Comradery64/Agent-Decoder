"""Phase 1 integration: JSONL → DB → analytics API endpoints, end-to-end."""

import tempfile
from pathlib import Path

from httpx import AsyncClient, ASGITransport

from agent_decoder.db import Database
from agent_decoder.models import Project
from agent_decoder.parser import parse_session

FIXTURE = Path(__file__).parent.parent / "fixtures" / "session_with_usage.jsonl"


async def _seed_db(db_path: Path) -> str:
    """Parse fixture → DB. Returns the session ID."""
    project = Project(
        id="integ-proj",
        display_name="Integration",
        raw_path="-integ",
        data_source="test",
    )
    session, messages = parse_session(FIXTURE)
    session.project_id = project.id
    async with Database(db_path) as db:
        await db.init_schema()
        await db.upsert_project(project)
        await db.upsert_session(session)
        await db.upsert_messages(messages)
    return session.id


async def test_tokens_endpoint_returns_correct_totals():
    from agent_decoder.config import load_config
    from agent_decoder.server import create_app

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        session_id = await _seed_db(db_path)
        app = create_app(load_config(), db_path)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            r = await client.get(f"/api/analytics/sessions/{session_id}/tokens")
    assert r.status_code == 200
    data = r.json()
    assert data["input_tokens"] == 32  # 12 + 20
    assert data["output_tokens"] == 5  # 3 + 2
    assert data["cache_creation_tokens"] == 100
    assert data["cache_read_tokens"] == 50
    assert data["total"] == 187  # 32 + 5 + 100 + 50


async def test_phase2_cost_endpoint_returns_nonzero_for_known_model():
    """Parse usage fixture, call cost route, expect non-zero total for sonnet."""
    from agent_decoder.config import load_config
    from agent_decoder.server import create_app

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        session_id = await _seed_db(db_path)
        app = create_app(load_config(), db_path)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            r = await client.get(f"/api/analytics/sessions/{session_id}/cost")
    assert r.status_code == 200
    data = r.json()
    assert len(data["models"]) >= 1
    assert data["models"][0]["model"] == "claude-sonnet-4-6"
    assert data["total_usd"] > 0
    assert data["pricing_known"] is True


async def test_phase2_pricing_table_contains_sonnet():
    from agent_decoder.config import load_config
    from agent_decoder.server import create_app

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        await _seed_db(db_path)
        app = create_app(load_config(), db_path)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            r = await client.get("/api/pricing")
    assert r.status_code == 200
    data = r.json()
    models = [m["model"] for m in data["models"]]
    assert any("sonnet" in m for m in models)


async def test_prompts_endpoint_returns_ranked_list():
    from agent_decoder.config import load_config
    from agent_decoder.server import create_app

    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        session_id = await _seed_db(db_path)
        app = create_app(load_config(), db_path)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            r = await client.get(f"/api/analytics/sessions/{session_id}/prompts")
    assert r.status_code == 200
    prompts = r.json()
    assert len(prompts) == 2
    # Ranked descending by total
    assert prompts[0]["total"] >= prompts[1]["total"]
    # Verify the higher-total prompt has the cache tokens
    top = prompts[0]
    assert top["cache_creation_tokens"] == 100
    assert top["cache_read_tokens"] == 50
