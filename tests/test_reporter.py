"""Tests for reporter.py — JSON and Markdown export."""

from datetime import datetime, timezone
from decimal import Decimal

from agent_decoder.analytics.cost import SessionCost
from agent_decoder.analytics.models import TokenBreakdown
from agent_decoder.analytics.pricing import ModelPricing
from agent_decoder.models import Message, SessionDetail, TextBlock, TokenUsage


def _make_session_detail(
    messages: list[Message] | None = None,
    title: str = "Test Session",
    model: str = "claude-sonnet-4-6",
) -> SessionDetail:
    if messages is None:
        messages = [
            Message(
                id="u1",
                session_id="s1",
                role="user",
                content_blocks=[TextBlock(text="Hello")],
                timestamp=datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc),
            ),
            Message(
                id="a1",
                session_id="s1",
                role="assistant",
                model=model,
                content_blocks=[TextBlock(text="Hi there")],
                timestamp=datetime(2026, 1, 1, 10, 0, 5, tzinfo=timezone.utc),
                usage=TokenUsage(input_tokens=10, output_tokens=5),
            ),
        ]
    return SessionDetail(
        id="s1",
        project_id="p1",
        file_path="/tmp/test.jsonl",
        title=title,
        model=model,
        started_at=datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, 10, 0, 5, tzinfo=timezone.utc),
        message_count=len(messages),
        cwd="/home/user/project",
        git_branch="main",
        messages=messages,
    )


def _make_cost() -> SessionCost:
    return SessionCost(
        model="claude-sonnet-4-6",
        pricing=ModelPricing(
            input_per_mtok=Decimal("3.00"),
            output_per_mtok=Decimal("15.00"),
            cache_write_per_mtok=Decimal("3.75"),
            cache_read_per_mtok=Decimal("0.30"),
        ),
        breakdown=TokenBreakdown(input_tokens=10, output_tokens=5),
        input_usd=Decimal("0.000030"),
        output_usd=Decimal("0.000075"),
        cache_write_usd=Decimal("0"),
        cache_read_usd=Decimal("0"),
        total_usd=Decimal("0.000105"),
    )


class TestExportJson:
    def test_basic_export_structure(self):
        from agent_decoder.reporter import export_json

        detail = _make_session_detail()
        result = export_json(detail)

        assert "session" in result
        assert "messages" in result
        assert result["session"]["id"] == "s1"
        assert result["session"]["title"] == "Test Session"
        assert result["session"]["model"] == "claude-sonnet-4-6"
        assert result["session"]["cwd"] == "/home/user/project"
        assert result["session"]["git_branch"] == "main"
        assert len(result["messages"]) == 2

    def test_messages_include_text_and_usage(self):
        from agent_decoder.reporter import export_json

        detail = _make_session_detail()
        result = export_json(detail)

        user_msg = result["messages"][0]
        assert user_msg["role"] == "user"
        assert user_msg["text"] == "Hello"
        assert "usage" not in user_msg

        asst_msg = result["messages"][1]
        assert asst_msg["role"] == "assistant"
        assert asst_msg["text"] == "Hi there"
        assert asst_msg["usage"]["input_tokens"] == 10
        assert asst_msg["usage"]["output_tokens"] == 5

    def test_export_with_cost(self):
        from agent_decoder.reporter import export_json

        detail = _make_session_detail()
        cost = _make_cost()
        result = export_json(detail, cost=cost)

        assert "cost" in result
        assert result["cost"]["model"] == "claude-sonnet-4-6"
        assert result["cost"]["total_usd"] == 0.000105
        assert result["cost"]["pricing_known"] is True

    def test_export_without_cost_has_no_cost_key(self):
        from agent_decoder.reporter import export_json

        detail = _make_session_detail()
        result = export_json(detail)
        assert "cost" not in result

    def test_export_with_prompts(self):
        from agent_decoder.reporter import export_json

        detail = _make_session_detail()
        prompts = [
            {
                "user_message_id": "u1",
                "assistant_message_id": "a1",
                "breakdown": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cache_creation_tokens": 0,
                    "cache_read_tokens": 0,
                    "total": 15,
                },
            },
        ]
        result = export_json(detail, prompts=prompts)
        assert "prompts" in result
        assert len(result["prompts"]) == 1
        assert result["prompts"][0]["user_message_id"] == "u1"

    def test_export_serializable_to_json(self):
        import json
        from agent_decoder.reporter import export_json

        detail = _make_session_detail()
        cost = _make_cost()
        result = export_json(detail, cost=cost)
        serialized = json.dumps(result)
        assert isinstance(serialized, str)
        parsed = json.loads(serialized)
        assert parsed["session"]["id"] == "s1"


class TestExportMarkdown:
    def test_basic_markdown_has_title_and_summary(self):
        from agent_decoder.reporter import export_markdown

        detail = _make_session_detail()
        md = export_markdown(detail)

        assert "# Test Session" in md
        assert "## Executive Summary" in md
        assert "`s1`" in md
        assert "claude-sonnet-4-6" in md
        assert "15" in md  # total tokens (10+5)

    def test_markdown_includes_token_breakdown(self):
        from agent_decoder.reporter import export_markdown

        detail = _make_session_detail()
        md = export_markdown(detail)

        assert "Input: 10" in md
        assert "Output: 5" in md

    def test_markdown_with_cost(self):
        from agent_decoder.reporter import export_markdown

        detail = _make_session_detail()
        cost = _make_cost()
        md = export_markdown(detail, cost=cost)

        assert "Estimated cost:" in md
        assert "$0.0001" in md

    def test_markdown_with_pricing_table(self):
        from agent_decoder.reporter import export_markdown

        detail = _make_session_detail()
        pricing = ModelPricing(
            input_per_mtok=Decimal("3.00"),
            output_per_mtok=Decimal("15.00"),
            cache_write_per_mtok=Decimal("3.75"),
            cache_read_per_mtok=Decimal("0.30"),
        )
        md = export_markdown(detail, pricing=pricing)

        assert "## Pricing Table" in md
        assert "$3.00" in md
        assert "$15.00" in md
        assert "$3.75" in md
        assert "$0.30" in md

    def test_markdown_with_model_usage(self):
        from agent_decoder.reporter import export_markdown

        detail = _make_session_detail()
        usage = [
            {
                "model": "claude-sonnet-4-6",
                "message_count": 5,
                "input_tokens": 100,
                "output_tokens": 50,
                "total_tokens": 150,
            },
        ]
        md = export_markdown(detail, all_models_usage=usage)

        assert "## Model Usage" in md
        assert "claude-sonnet-4-6" in md
        assert "150" in md

    def test_markdown_with_prompts(self):
        from agent_decoder.reporter import export_markdown

        detail = _make_session_detail()
        prompts = [
            {
                "user_message_id": "u1",
                "assistant_message_id": "a1",
                "breakdown": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cache_creation_tokens": 0,
                    "cache_read_tokens": 0,
                    "total": 15,
                },
            },
        ]
        md = export_markdown(detail, prompts=prompts)

        assert "## Prompt Breakdown" in md

    def test_markdown_conversation_log(self):
        from agent_decoder.reporter import export_markdown

        detail = _make_session_detail()
        md = export_markdown(detail)

        assert "## Conversation" in md
        assert "**User**" in md
        assert "**Assistant**" in md
        assert "Hello" in md
        assert "Hi there" in md

    def test_markdown_meta_messages_excluded(self):
        from agent_decoder.reporter import export_markdown

        messages = [
            Message(
                id="meta1",
                session_id="s1",
                role="user",
                is_meta=True,
                content_blocks=[TextBlock(text="system prompt")],
            ),
            Message(
                id="u1",
                session_id="s1",
                role="user",
                content_blocks=[TextBlock(text="real message")],
            ),
        ]
        detail = _make_session_detail(messages=messages)
        md = export_markdown(detail)

        assert "system prompt" not in md
        assert "real message" in md


class TestExportRoutes:
    """Tests for the export API endpoints."""

    async def _make_client(self):
        from httpx import AsyncClient, ASGITransport
        from agent_decoder.server import create_app
        from agent_decoder.config import AppConfig
        from agent_decoder.db import Database
        from pathlib import Path
        import tempfile

        tmp = tempfile.mkdtemp()
        db_path = Path(tmp) / "test.db"
        async with Database(db_path) as db:
            await db.init_schema()
        app = create_app(AppConfig(), db_path)
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    async def test_json_export_route_404(self):
        async with await self._make_client() as client:
            r = await client.get(
                "/api/sessions/nonexistent/export", params={"format": "json"}
            )
            assert r.status_code == 404

    async def test_markdown_export_route_404(self):
        async with await self._make_client() as client:
            r = await client.get(
                "/api/sessions/nonexistent/export", params={"format": "md"}
            )
            assert r.status_code == 404

    async def test_export_route_invalid_format(self):
        async with await self._make_client() as client:
            r = await client.get(
                "/api/sessions/nonexistent/export", params={"format": "csv"}
            )
            assert r.status_code == 400

    async def test_json_export_seeded_session(self):
        """Export a real seeded session as JSON and verify structure."""
        from pathlib import Path
        import tempfile
        from httpx import AsyncClient, ASGITransport
        from agent_decoder.server import create_app
        from agent_decoder.config import AppConfig
        from agent_decoder.db import Database
        from agent_decoder.parser import parse_session
        from agent_decoder.models import Project

        FIXTURES = Path(__file__).parent / "fixtures"
        USAGE_JSONL = FIXTURES / "session_with_usage.jsonl"
        USAGE_SESSION_ID = "cccccccc-0000-0000-0000-000000000003"

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            project = Project(
                id="test-proj",
                display_name="Test",
                raw_path="-test",
                data_source="test",
            )
            session, messages = parse_session(USAGE_JSONL)
            session.project_id = project.id
            async with Database(db_path) as db:
                await db.init_schema()
                await db.upsert_project(project)
                await db.upsert_session(session)
                await db.upsert_messages(messages)
            app = create_app(AppConfig(), db_path)
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                r = await client.get(
                    f"/api/sessions/{USAGE_SESSION_ID}/export",
                    params={"format": "json"},
                )
            assert r.status_code == 200
            assert "application/json" in r.headers["content-type"]
            assert "attachment" in r.headers.get("content-disposition", "")
            data = r.json()
            assert data["session"]["id"] == USAGE_SESSION_ID
            assert len(data["messages"]) > 0
            assert "cost" in data
            assert data["cost"]["total_usd"] > 0
            assert "prompts" in data

    async def test_markdown_export_seeded_session(self):
        """Export a real seeded session as Markdown and verify content."""
        from pathlib import Path
        import tempfile
        from httpx import AsyncClient, ASGITransport
        from agent_decoder.server import create_app
        from agent_decoder.config import AppConfig
        from agent_decoder.db import Database
        from agent_decoder.parser import parse_session
        from agent_decoder.models import Project

        FIXTURES = Path(__file__).parent / "fixtures"
        USAGE_JSONL = FIXTURES / "session_with_usage.jsonl"
        USAGE_SESSION_ID = "cccccccc-0000-0000-0000-000000000003"

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            project = Project(
                id="test-proj",
                display_name="Test",
                raw_path="-test",
                data_source="test",
            )
            session, messages = parse_session(USAGE_JSONL)
            session.project_id = project.id
            async with Database(db_path) as db:
                await db.init_schema()
                await db.upsert_project(project)
                await db.upsert_session(session)
                await db.upsert_messages(messages)
            app = create_app(AppConfig(), db_path)
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                r = await client.get(
                    f"/api/sessions/{USAGE_SESSION_ID}/export", params={"format": "md"}
                )
            assert r.status_code == 200
            assert "text/markdown" in r.headers["content-type"]
            assert "attachment" in r.headers.get("content-disposition", "")
            md = r.text
            assert "# usage-test-session" in md
            assert "## Executive Summary" in md
            assert "## Pricing Table" in md
            assert "## Conversation" in md
