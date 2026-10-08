"""End-to-end tests for the Codex provider — exercises all API routes with a
seeded database built from the Codex fixture.

Mirrors ``tests/test_e2e.py`` in structure (``_seed_db`` + ``ASGITransport`` /
``AsyncClient``, ``_make_app``).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from agent_decoder.db import Database
from agent_decoder.models import AppConfig, Project
from agent_decoder.providers.codex import CodexAdapter, _codex_project

FIXTURES = Path(__file__).parent / "fixtures" / "codex"
FIXTURE = FIXTURES / "sample_rollout.jsonl"

CODEX_SESSION_ID = "019e901c-ca9b-7303-802a-789af509fde0"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_db(db_path: Path) -> None:
    """Parse the Codex fixture and upsert into DB."""
    adapter = CodexAdapter()
    session, messages = adapter.parse(FIXTURE)

    project = Project(
        id=_codex_project(session.cwd, "test").id,
        display_name="Dev/demo-project",
        raw_path=session.cwd or "codex/(no project)",
        resolved_path=None,
        data_source="test",
    )
    session.project_id = project.id

    async with Database(db_path) as db:
        await db.init_schema()
        await db.upsert_project(project)
        await db.upsert_session(session)
        await db.upsert_messages(messages)


def _make_app(db_path: Path, config: AppConfig | None = None):
    from agent_decoder.config import load_config
    from agent_decoder.server import create_app

    return create_app(config or load_config(), db_path)


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture
async def client_seeded():
    """AsyncClient backed by a fully-seeded app (no startup side effects)."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        await _seed_db(db_path)
        app = _make_app(db_path, config=AppConfig())
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            yield c


# ---------------------------------------------------------------------------
# Sessions list
# ---------------------------------------------------------------------------


class TestSessionsList:
    async def test_codex_session_is_listed(self, client_seeded):
        r = await client_seeded.get("/api/sessions")
        assert r.status_code == 200
        sessions = r.json()
        ids = [s["id"] for s in sessions]
        assert CODEX_SESSION_ID in ids

    async def test_codex_session_has_correct_provider(self, client_seeded):
        r = await client_seeded.get("/api/sessions")
        sessions = r.json()
        codex_session = next(s for s in sessions if s["id"] == CODEX_SESSION_ID)
        assert codex_session["provider"] == "codex"

    async def test_codex_session_title_non_null(self, client_seeded):
        r = await client_seeded.get("/api/sessions")
        sessions = r.json()
        codex_session = next(s for s in sessions if s["id"] == CODEX_SESSION_ID)
        assert codex_session["title"] is not None
        assert len(codex_session["title"]) > 0


# ---------------------------------------------------------------------------
# Session detail
# ---------------------------------------------------------------------------


class TestSessionDetail:
    async def test_codex_session_detail_has_provider(self, client_seeded):
        r = await client_seeded.get(f"/api/sessions/{CODEX_SESSION_ID}")
        assert r.status_code == 200
        data = r.json()
        assert data["provider"] == "codex"

    async def test_codex_session_detail_has_messages(self, client_seeded):
        r = await client_seeded.get(f"/api/sessions/{CODEX_SESSION_ID}")
        data = r.json()
        assert "messages" in data
        assert len(data["messages"]) > 0

    async def test_codex_session_detail_has_text_block(self, client_seeded):
        r = await client_seeded.get(f"/api/sessions/{CODEX_SESSION_ID}")
        messages = r.json()["messages"]
        all_blocks = [b for m in messages for b in m.get("content_blocks", [])]
        text_blocks = [b for b in all_blocks if b.get("type") == "text"]
        assert len(text_blocks) >= 1

    async def test_codex_session_detail_has_tool_use_block(self, client_seeded):
        r = await client_seeded.get(f"/api/sessions/{CODEX_SESSION_ID}")
        messages = r.json()["messages"]
        all_blocks = [b for m in messages for b in m.get("content_blocks", [])]
        tool_use_blocks = [b for b in all_blocks if b.get("type") == "tool_use"]
        assert len(tool_use_blocks) >= 1

    async def test_codex_session_detail_has_tool_result_block(self, client_seeded):
        r = await client_seeded.get(f"/api/sessions/{CODEX_SESSION_ID}")
        messages = r.json()["messages"]
        all_blocks = [b for m in messages for b in m.get("content_blocks", [])]
        tool_result_blocks = [b for b in all_blocks if b.get("type") == "tool_result"]
        assert len(tool_result_blocks) >= 1

    async def test_codex_session_detail_has_thinking_placeholder(self, client_seeded):
        r = await client_seeded.get(f"/api/sessions/{CODEX_SESSION_ID}")
        messages = r.json()["messages"]
        all_blocks = [b for m in messages for b in m.get("content_blocks", [])]
        thinking_blocks = [b for b in all_blocks if b.get("type") == "thinking"]
        assert len(thinking_blocks) >= 1
        assert thinking_blocks[0]["thinking"] == "🔒 Reasoning (encrypted)"


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


class TestSearch:
    async def test_search_finds_decodeotron(self, client_seeded):
        r = await client_seeded.get("/api/search?q=Decodeotron")
        assert r.status_code == 200
        hits = r.json()
        assert len(hits) >= 1


# ---------------------------------------------------------------------------
# Analytics — model usage breakdown
# ---------------------------------------------------------------------------


class TestAnalyticsModels:
    async def test_models_endpoint_returns_200(self, client_seeded):
        r = await client_seeded.get("/api/analytics/models")
        assert r.status_code == 200

    async def test_gpt55_appears_in_model_usage(self, client_seeded):
        r = await client_seeded.get("/api/analytics/models")
        assert r.status_code == 200
        models_data = r.json()
        model_names = [entry["model"] for entry in models_data]
        assert "gpt-5.5" in model_names
