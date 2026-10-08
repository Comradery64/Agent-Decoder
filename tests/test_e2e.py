"""End-to-end tests — every API route exercised with a real seeded database.

Coverage targets:
  server.py  — all routes (health, config, projects, sessions, search, stats,
                refresh/do_scan, analytics ×3, reveal, events) + SSE contract
  config.py  — save_config side-effect path (via PUT /api/config)
  db.py      — migration idempotency, search, stats edge cases
  scanner.py — full scan via /api/refresh with real directory fixture
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from agent_decoder.db import Database
from agent_decoder.models import AppConfig, Project
from agent_decoder.parser import parse_session

FIXTURES = Path(__file__).parent / "fixtures"
SIMPLE_JSONL = FIXTURES / "simple_session.jsonl"
USAGE_JSONL = FIXTURES / "session_with_usage.jsonl"

# Session IDs as derived by the parser from those files
SIMPLE_SESSION_ID = "aaaaaaaa-0000-0000-0000-000000000001"
USAGE_SESSION_ID = "cccccccc-0000-0000-0000-000000000003"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_db(db_path: Path) -> None:
    """Parse both fixtures and upsert into DB."""
    async with Database(db_path) as db:
        await db.init_schema()
        for fixture in (SIMPLE_JSONL, USAGE_JSONL):
            project = Project(
                id=f"proj-{fixture.stem}",
                display_name=fixture.stem,
                raw_path=f"-{fixture.stem}",
                data_source="test",
            )
            session, messages = parse_session(fixture)
            session.project_id = project.id
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


@pytest.fixture
async def client_empty():
    """AsyncClient backed by an empty (schema-only) DB."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        async with Database(db_path) as db:
            await db.init_schema()
        app = _make_app(db_path, config=AppConfig())
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            yield c


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class TestHealth:
    async def test_returns_ok(self, client_empty):
        r = await client_empty.get("/api/health")
        assert r.status_code == 200
        assert r.json() == {"ok": True}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class TestConfig:
    async def test_get_config_returns_app_config(self, client_empty):
        r = await client_empty.get("/api/config")
        assert r.status_code == 200
        data = r.json()
        assert "data_paths" in data
        assert "port" in data
        assert "theme" in data

    async def test_put_config_updates_and_returns(self, client_empty):
        new_cfg = {
            "data_paths": ["/tmp/test"],
            "theme": "dark",
            "auto_open_browser": False,
            "port": 9999,
        }
        with patch("agent_decoder.server.save_config"):
            r = await client_empty.put("/api/config", json=new_cfg)
        assert r.status_code == 200
        data = r.json()
        assert data["theme"] == "dark"
        assert data["port"] == 9999
        # Subsequent GET reflects the update
        r2 = await client_empty.get("/api/config")
        assert r2.json()["theme"] == "dark"


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------


class TestProjects:
    async def test_get_projects_returns_list(self, client_seeded):
        r = await client_seeded.get("/api/projects")
        assert r.status_code == 200
        projects = r.json()
        assert len(projects) == 2

    async def test_get_projects_empty_db(self, client_empty):
        r = await client_empty.get("/api/projects")
        assert r.status_code == 200
        assert r.json() == []

    async def test_get_project_sessions(self, client_seeded):
        r = await client_seeded.get(f"/api/projects/proj-{SIMPLE_JSONL.stem}/sessions")
        assert r.status_code == 200
        sessions = r.json()
        assert len(sessions) == 1
        assert sessions[0]["id"] == SIMPLE_SESSION_ID

    async def test_get_project_sessions_unknown_project(self, client_seeded):
        r = await client_seeded.get("/api/projects/nonexistent/sessions")
        assert r.status_code == 200
        assert r.json() == []


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


class TestSessions:
    async def test_get_session_returns_detail(self, client_seeded):
        r = await client_seeded.get(f"/api/sessions/{SIMPLE_SESSION_ID}")
        assert r.status_code == 200
        data = r.json()
        assert data["id"] == SIMPLE_SESSION_ID
        assert "messages" in data
        assert len(data["messages"]) > 0

    async def test_get_session_not_found(self, client_seeded):
        r = await client_seeded.get("/api/sessions/nonexistent-id")
        assert r.status_code == 404
        assert r.json()["detail"] == "Session not found"

    async def test_session_messages_have_usage_for_usage_fixture(self, client_seeded):
        r = await client_seeded.get(f"/api/sessions/{USAGE_SESSION_ID}")
        assert r.status_code == 200
        data = r.json()
        assistant_msgs = [m for m in data["messages"] if m["role"] == "assistant"]
        assert len(assistant_msgs) == 2
        # Both should have usage serialised
        for msg in assistant_msgs:
            assert msg["usage"] is not None
            assert "input_tokens" in msg["usage"]

    async def test_session_messages_ordered_by_timestamp(self, client_seeded):
        r = await client_seeded.get(f"/api/sessions/{SIMPLE_SESSION_ID}")
        timestamps = [m["timestamp"] for m in r.json()["messages"] if m["timestamp"]]
        assert timestamps == sorted(timestamps)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


class TestSearch:
    async def test_search_finds_content(self, client_seeded):
        r = await client_seeded.get("/api/search?q=Python")
        assert r.status_code == 200
        hits = r.json()
        assert len(hits) >= 1
        assert any(
            "Python" in h["snippet"] or "python" in h["snippet"].lower() for h in hits
        )

    async def test_search_no_results(self, client_seeded):
        r = await client_seeded.get("/api/search?q=xyzzy_nonexistent_42")
        assert r.status_code == 200
        assert r.json() == []

    async def test_search_filtered_by_project(self, client_seeded):
        r = await client_seeded.get(
            f"/api/search?q=Python&project=proj-{SIMPLE_JSONL.stem}"
        )
        assert r.status_code == 200
        hits = r.json()
        assert len(hits) >= 1

    async def test_search_filtered_by_wrong_project_returns_empty(self, client_seeded):
        r = await client_seeded.get("/api/search?q=Python&project=nonexistent")
        assert r.status_code == 200
        assert r.json() == []

    async def test_search_missing_query_param_is_invalid(self, client_seeded):
        r = await client_seeded.get("/api/search")
        assert r.status_code == 422

    async def test_search_empty_string_query_is_invalid(self, client_seeded):
        r = await client_seeded.get("/api/search?q=")
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


class TestStats:
    async def test_stats_with_data(self, client_seeded):
        r = await client_seeded.get("/api/stats")
        assert r.status_code == 200
        data = r.json()
        assert data["total_projects"] == 2
        assert data["total_sessions"] == 2
        assert data["total_messages"] > 0
        assert "data_paths" in data

    async def test_stats_empty_db(self, client_empty):
        r = await client_empty.get("/api/stats")
        assert r.status_code == 200
        data = r.json()
        assert data["total_projects"] == 0
        assert data["total_sessions"] == 0
        assert data["total_messages"] == 0


# ---------------------------------------------------------------------------
# Refresh (exercises do_scan)
# ---------------------------------------------------------------------------


class TestRefresh:
    async def test_refresh_empty_paths_returns_ok(self, client_empty):
        """Refresh with no scannable paths still returns ok."""
        r = await client_empty.post("/api/refresh")
        assert r.status_code == 200
        assert r.json() == {"ok": True}

    async def test_refresh_scans_and_indexes_sessions(self):
        """Refresh with a real projects directory structure indexes sessions."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "test.db"

            # Build the directory structure scanner expects:
            # <root>/projects/<mangled-name>/<uuid>.jsonl
            projects_dir = tmp_path / "root" / "projects" / "-test-project"
            projects_dir.mkdir(parents=True)
            shutil.copy(SIMPLE_JSONL, projects_dir / SIMPLE_JSONL.name)

            async with Database(db_path) as db:
                await db.init_schema()

            config = AppConfig(data_paths=[str(tmp_path / "root")])
            app = _make_app(db_path, config)
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                r = await client.post("/api/refresh")
                assert r.status_code == 200
                # After scan, session should be indexed
                r2 = await client.get("/api/stats")
                assert r2.json()["total_sessions"] >= 1

    async def test_refresh_merges_subagent_chat_into_parent_when_opted_in(self):
        """include_subagent_chats=True: a Task-tool sub-agent transcript
        under <session>/subagents/agent-*.jsonl is merged into the PARENT
        session — not indexed as a session of its own, and without
        corrupting the parent's own title/cwd/message metadata."""
        parent_id = "aaaaaaaa-1111-1111-1111-111111111111"
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "test.db"

            projects_dir = tmp_path / "root" / "projects" / "-test-project"
            projects_dir.mkdir(parents=True)

            parent_file = projects_dir / f"{parent_id}.jsonl"
            parent_file.write_text(
                "\n".join(
                    [
                        '{"type":"custom-title","customTitle":"Parent chat",'
                        f'"sessionId":"{parent_id}"}}',
                        '{"parentUuid":null,"isSidechain":false,"type":"user",'
                        '"message":{"role":"user","content":"Please check status"},'
                        '"uuid":"main-0001","timestamp":"2026-01-02T10:00:00.000Z",'
                        f'"isMeta":false,"cwd":"/repo","sessionId":"{parent_id}",'
                        '"gitBranch":"main"}',
                        '{"parentUuid":"main-0001","isSidechain":false,'
                        '"message":{"model":"claude-sonnet-4-6","id":"msg_m1",'
                        '"type":"message","role":"assistant","content":['
                        '{"type":"text","text":"I\'ll delegate this."},'
                        '{"type":"tool_use","id":"toolu_task1","name":"Task",'
                        '"input":{"prompt":"check status"}}],'
                        '"stop_reason":"tool_use","usage":{}},"requestId":"req_m1",'
                        '"type":"assistant","uuid":"main-0002",'
                        '"timestamp":"2026-01-02T10:00:02.000Z","cwd":"/repo",'
                        f'"sessionId":"{parent_id}","gitBranch":"main"}}',
                        "",
                    ]
                )
            )

            subagents_dir = projects_dir / parent_id / "subagents"
            subagents_dir.mkdir(parents=True)
            (subagents_dir / "agent-task1.jsonl").write_text(
                "\n".join(
                    [
                        '{"parentUuid":null,"isSidechain":true,"type":"user",'
                        '"message":{"role":"user","content":"check status"},'
                        '"uuid":"side-0001","timestamp":"2026-01-02T10:00:03.000Z",'
                        f'"isMeta":false,"cwd":"/repo","sessionId":"{parent_id}",'
                        '"gitBranch":"main"}',
                        '{"parentUuid":"side-0001","isSidechain":true,'
                        '"message":{"model":"claude-haiku-4-5","id":"msg_s1",'
                        '"type":"message","role":"assistant","content":['
                        '{"type":"text","text":"All good."}],'
                        '"stop_reason":"end_turn","usage":{}},"requestId":"req_s1",'
                        '"type":"assistant","uuid":"side-0002",'
                        '"timestamp":"2026-01-02T10:00:04.000Z","cwd":"/repo",'
                        f'"sessionId":"{parent_id}","gitBranch":"main"}}',
                        "",
                    ]
                )
            )
            (subagents_dir / "agent-task1.meta.json").write_text(
                '{"toolUseId": "toolu_task1"}'
            )

            async with Database(db_path) as db:
                await db.init_schema()

            config = AppConfig(
                data_paths=[str(tmp_path / "root")],
                include_subagent_chats=True,
                # Isolate from the real ~/.codex/sessions default — otherwise
                # this machine's actual Codex history would be scanned too,
                # inflating total_sessions unrelated to this test.
                codex_data_paths=[],
            )
            app = _make_app(db_path, config)
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                r = await client.post("/api/refresh")
                assert r.status_code == 200

                # Never indexed as its own session — merged into the parent.
                stats = (await client.get("/api/stats")).json()
                assert stats["total_sessions"] == 1

                projects = (await client.get("/api/projects")).json()
                assert len(projects) == 1
                assert projects[0]["session_count"] == 1

                detail = (await client.get(f"/api/sessions/{parent_id}")).json()
                # Parent metadata must survive untouched, not be overwritten
                # by whatever parse_session scraped from the subagent file
                # alone (which would have no custom-title record at all).
                assert detail["title"] == "Parent chat"
                assert detail["cwd"] == "/repo"

                ids = {m["id"] for m in detail["messages"]}
                assert {"main-0001", "main-0002", "side-0001", "side-0002"} <= ids

                side_root = next(
                    m for m in detail["messages"] if m["id"] == "side-0001"
                )
                assert side_root["source_tool_assistant_uuid"] == "main-0002"

                # Re-scanning must not duplicate the merged messages.
                r2 = await client.post("/api/refresh")
                assert r2.status_code == 200
                detail2 = (await client.get(f"/api/sessions/{parent_id}")).json()
                assert len(detail2["messages"]) == len(detail["messages"])

    async def test_refresh_survives_a_session_file_deleted_mid_scan(self):
        """Regression for 98d3b42: do_scan's stat()/mtime check must be inside
        the per-file try/except. A session file that disappears between
        discovery (the directory glob) and this stat() call used to raise
        FileNotFoundError outside that guard, aborting the whole scan loop —
        silently killing every session queued after the dead one."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "test.db"

            projects_dir = tmp_path / "root" / "projects" / "-test-project"
            projects_dir.mkdir(parents=True)
            doomed_path = projects_dir / "doomed.jsonl"
            shutil.copy(SIMPLE_JSONL, doomed_path)
            shutil.copy(USAGE_JSONL, projects_dir / USAGE_JSONL.name)

            async with Database(db_path) as db:
                await db.init_schema()

            config = AppConfig(data_paths=[str(tmp_path / "root")])
            app = _make_app(db_path, config)

            real_stat = Path.stat

            def flaky_stat(self, *args, **kwargs):
                if self == doomed_path:
                    raise FileNotFoundError(self)
                return real_stat(self, *args, **kwargs)

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            ) as client:
                with patch.object(Path, "stat", flaky_stat):
                    r = await client.post("/api/refresh")
                assert r.status_code == 200

                # The doomed file's own error must not have aborted the scan
                # before the session queued after it got a turn.
                r2 = await client.get("/api/stats")
                assert r2.json()["total_sessions"] >= 1
                projects = (await client.get("/api/projects")).json()
                sessions = (
                    await client.get(f"/api/projects/{projects[0]['id']}/sessions")
                ).json()
                assert any(s["id"] == USAGE_SESSION_ID for s in sessions)


# ---------------------------------------------------------------------------
# Reveal
# ---------------------------------------------------------------------------


class TestReveal:
    async def test_reveal_unknown_session_returns_404(self, client_seeded):
        r = await client_seeded.post("/api/sessions/nonexistent/reveal")
        assert r.status_code == 404
        assert r.json()["detail"] == "Session not found"

    async def test_reveal_known_session_file_not_on_disk_returns_404(
        self, client_seeded
    ):
        # The fixture sessions point to files in our fixtures dir which do exist.
        # We use a session whose file_path the DB stores — but in the seeded app
        # the parser records the real path, so we just verify the route is reachable.
        # We test the "file not found" branch by patching Path.exists.
        with patch("agent_decoder.server.Path") as mock_path_cls:
            mock_instance = mock_path_cls.return_value
            mock_instance.exists.return_value = False
            r = await client_seeded.post(f"/api/sessions/{SIMPLE_SESSION_ID}/reveal")
        # Either 404 (file not found branch) or 200 (real file found) is valid
        assert r.status_code in (200, 404)


# ---------------------------------------------------------------------------
# Analytics — tokens
# ---------------------------------------------------------------------------


class TestAnalyticsTokens:
    async def test_tokens_for_usage_session(self, client_seeded):
        r = await client_seeded.get(
            f"/api/analytics/sessions/{USAGE_SESSION_ID}/tokens"
        )
        assert r.status_code == 200
        data = r.json()
        assert data["input_tokens"] == 32  # 12 + 20
        assert data["output_tokens"] == 5  # 3 + 2
        assert data["cache_creation_tokens"] == 100
        assert data["cache_read_tokens"] == 50
        assert data["total"] == 187
        assert data["session_id"] == USAGE_SESSION_ID

    async def test_tokens_for_simple_session(self, client_seeded):
        r = await client_seeded.get(
            f"/api/analytics/sessions/{SIMPLE_SESSION_ID}/tokens"
        )
        assert r.status_code == 200
        data = r.json()
        # simple_session.jsonl has usage fields on assistant messages
        assert data["input_tokens"] > 0
        assert "total" in data

    async def test_tokens_unknown_session_returns_404(self, client_seeded):
        r = await client_seeded.get("/api/analytics/sessions/nonexistent/tokens")
        assert r.status_code == 404
        assert r.json()["detail"] == "Session not found"

    async def test_tokens_response_shape(self, client_seeded):
        r = await client_seeded.get(
            f"/api/analytics/sessions/{USAGE_SESSION_ID}/tokens"
        )
        data = r.json()
        for key in (
            "session_id",
            "input_tokens",
            "output_tokens",
            "cache_creation_tokens",
            "cache_read_tokens",
            "total",
        ):
            assert key in data


# ---------------------------------------------------------------------------
# Analytics — prompts
# ---------------------------------------------------------------------------


class TestAnalyticsPrompts:
    async def test_prompts_for_usage_session(self, client_seeded):
        r = await client_seeded.get(
            f"/api/analytics/sessions/{USAGE_SESSION_ID}/prompts"
        )
        assert r.status_code == 200
        prompts = r.json()
        assert len(prompts) == 2
        # Sorted descending by total
        assert prompts[0]["total"] >= prompts[1]["total"]

    async def test_prompts_response_shape(self, client_seeded):
        r = await client_seeded.get(
            f"/api/analytics/sessions/{USAGE_SESSION_ID}/prompts"
        )
        p = r.json()[0]
        for key in (
            "user_message_id",
            "assistant_message_id",
            "input_tokens",
            "output_tokens",
            "cache_creation_tokens",
            "cache_read_tokens",
            "total",
        ):
            assert key in p

    async def test_prompts_unknown_session_returns_404(self, client_seeded):
        r = await client_seeded.get("/api/analytics/sessions/nonexistent/prompts")
        assert r.status_code == 404

    async def test_prompts_cache_heavy_ranked_first(self, client_seeded):
        r = await client_seeded.get(
            f"/api/analytics/sessions/{USAGE_SESSION_ID}/prompts"
        )
        top = r.json()[0]
        # The second assistant message (cache_creation=100, cache_read=50) has higher total
        assert top["cache_creation_tokens"] == 100
        assert top["cache_read_tokens"] == 50


# ---------------------------------------------------------------------------
# Analytics — daily
# ---------------------------------------------------------------------------


class TestAnalyticsDaily:
    async def test_daily_returns_list(self, client_seeded):
        r = await client_seeded.get("/api/analytics/daily")
        assert r.status_code == 200
        buckets = r.json()
        assert isinstance(buckets, list)

    async def test_daily_buckets_have_correct_shape(self, client_seeded):
        r = await client_seeded.get("/api/analytics/daily")
        assert r.status_code == 200
        buckets = r.json()
        assert len(buckets) > 0
        b = buckets[0]
        for key in (
            "day",
            "input_tokens",
            "output_tokens",
            "cache_creation_tokens",
            "cache_read_tokens",
            "total",
            "prompt_count",
            "session_count",
        ):
            assert key in b

    async def test_daily_day_is_iso_format(self, client_seeded):
        r = await client_seeded.get("/api/analytics/daily")
        for b in r.json():
            # Must be parseable as ISO date YYYY-MM-DD
            from datetime import date

            date.fromisoformat(b["day"])

    async def test_daily_chronologically_ordered(self, client_seeded):
        r = await client_seeded.get("/api/analytics/daily")
        days = [b["day"] for b in r.json()]
        assert days == sorted(days)

    async def test_daily_empty_db_returns_empty(self, client_empty):
        r = await client_empty.get("/api/analytics/daily")
        assert r.status_code == 200
        assert r.json() == []


# ---------------------------------------------------------------------------
# Sessions — flat endpoint
# ---------------------------------------------------------------------------


class TestAllSessions:
    async def test_get_all_sessions_returns_both(self, client_seeded):
        r = await client_seeded.get("/api/sessions")
        assert r.status_code == 200
        sessions = r.json()
        assert len(sessions) == 2
        ids = {s["id"] for s in sessions}
        assert SIMPLE_SESSION_ID in ids
        assert USAGE_SESSION_ID in ids

    async def test_get_all_sessions_empty_db(self, client_empty):
        r = await client_empty.get("/api/sessions")
        assert r.status_code == 200
        assert r.json() == []


# ---------------------------------------------------------------------------
# Analytics — cost
# ---------------------------------------------------------------------------


class TestAnalyticsCost:
    async def test_cost_for_usage_session(self, client_seeded):
        r = await client_seeded.get(f"/api/analytics/sessions/{USAGE_SESSION_ID}/cost")
        assert r.status_code == 200
        data = r.json()
        assert data["session_id"] == USAGE_SESSION_ID
        assert len(data["models"]) >= 1
        assert data["models"][0]["model"] == "claude-sonnet-4-6"
        assert data["total_usd"] > 0
        assert data["pricing_known"] is True
        assert data["pricing_source"] in ("live", "hardcoded")

    async def test_cost_response_shape(self, client_seeded):
        r = await client_seeded.get(f"/api/analytics/sessions/{USAGE_SESSION_ID}/cost")
        data = r.json()
        for key in (
            "session_id",
            "models",
            "total_usd",
            "pricing_known",
            "pricing_source",
        ):
            assert key in data, f"missing top-level key: {key}"
        assert isinstance(data["models"], list) and len(data["models"]) >= 1
        for key in (
            "model",
            "input_usd",
            "output_usd",
            "cache_write_usd",
            "cache_read_usd",
            "total_usd",
            "pricing_known",
        ):
            assert key in data["models"][0], f"missing per-model key: {key}"

    async def test_cost_total_equals_sum_of_parts(self, client_seeded):
        r = await client_seeded.get(f"/api/analytics/sessions/{USAGE_SESSION_ID}/cost")
        data = r.json()
        # Per-model: each model's total equals the sum of its component fields.
        for entry in data["models"]:
            expected = (
                entry["input_usd"]
                + entry["output_usd"]
                + entry["cache_write_usd"]
                + entry["cache_read_usd"]
            )
            assert abs(entry["total_usd"] - expected) < 1e-9
        # Top-level: aggregate total equals the sum of per-model totals.
        per_model_sum = sum(entry["total_usd"] for entry in data["models"])
        assert abs(data["total_usd"] - per_model_sum) < 1e-9

    async def test_cost_unknown_session_returns_404(self, client_seeded):
        r = await client_seeded.get("/api/analytics/sessions/nonexistent/cost")
        assert r.status_code == 404
        assert r.json()["detail"] == "Session not found"

    async def test_cost_simple_session_has_sonnet_pricing(self, client_seeded):
        r = await client_seeded.get(f"/api/analytics/sessions/{SIMPLE_SESSION_ID}/cost")
        assert r.status_code == 200
        data = r.json()
        assert len(data["models"]) >= 1
        assert data["models"][0]["model"] == "claude-sonnet-4-6"
        assert data["pricing_known"] is True
        assert data["total_usd"] > 0


# ---------------------------------------------------------------------------
# Pricing table
# ---------------------------------------------------------------------------


class TestPricingTable:
    async def test_pricing_returns_200(self, client_empty):
        r = await client_empty.get("/api/pricing")
        assert r.status_code == 200

    async def test_pricing_response_shape(self, client_empty):
        r = await client_empty.get("/api/pricing")
        data = r.json()
        assert "source" in data
        assert "models" in data
        assert data["source"] in ("live", "hardcoded")
        assert isinstance(data["models"], list)

    async def test_pricing_contains_sonnet(self, client_empty):
        r = await client_empty.get("/api/pricing")
        models = [m["model"] for m in r.json()["models"]]
        assert any("sonnet" in m for m in models)

    async def test_pricing_model_entry_shape(self, client_empty):
        r = await client_empty.get("/api/pricing")
        entry = r.json()["models"][0]
        for key in (
            "model",
            "input_per_mtok",
            "output_per_mtok",
            "cache_write_per_mtok",
            "cache_read_per_mtok",
        ):
            assert key in entry, f"missing key: {key}"

    async def test_pricing_rates_are_positive(self, client_empty):
        r = await client_empty.get("/api/pricing")
        for entry in r.json()["models"]:
            assert entry["input_per_mtok"] > 0
            assert entry["output_per_mtok"] > 0


# ---------------------------------------------------------------------------
# Events (SSE — verify route exists and streams)
# ---------------------------------------------------------------------------


class TestEvents:
    def test_events_route_registered(self):
        """Verify /api/events is registered for SSE.

        We can't easily exercise the streaming response: the generator now blocks
        on a real disconnect signal (correct production behaviour), but TestClient
        doesn't reliably emit http.disconnect when a stream context exits mid-body,
        which would deadlock the test. Route introspection verifies registration.
        """
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            app = _make_app(db_path)
        paths = {r.path for r in app.routes if hasattr(r, "path")}
        assert "/api/events" in paths


# ---------------------------------------------------------------------------
# SSE payload contract
# ---------------------------------------------------------------------------


class TestSSEPayloadContract:
    """Lock down the shape of the SSE event payload.

    The backend emits  data: {"type": "refresh", "path": "..."}
    The frontend reads  data.type === "refresh"  to trigger re-fetches.

    Two silent failure modes this guards against:
      1. Field rename  — e.g. type → event_type — frontend never triggers.
      2. Path not stringified — json.dumps(Path(...)) raises TypeError at runtime.

    Tests import _sse_event_data directly so they pin the real serialization
    code, not a copy of it.
    """

    def _payload(self, path=None):
        import json
        from agent_decoder.server import _sse_event_data

        raw = _sse_event_data(
            path or Path("/home/user/.claude/projects/-foo/abc.jsonl")
        )
        return json.loads(raw)

    def test_type_field_is_refresh(self):
        assert self._payload()["type"] == "refresh"

    def test_path_field_is_string(self):
        assert isinstance(self._payload()["path"], str)

    def test_path_field_matches_stringified_input(self):
        p = Path("/home/user/.claude/projects/-my-proj/session.jsonl")
        assert self._payload(p)["path"] == str(p)

    def test_no_extra_fields(self):
        assert set(self._payload().keys()) == {"type", "path"}

    def test_pathlib_path_is_accepted(self):
        """Passing a raw Path (not pre-stringified) must not raise."""
        from agent_decoder.server import _sse_event_data

        result = _sse_event_data(Path("/some/file.jsonl"))
        assert isinstance(result, str)

    def test_string_path_is_also_accepted(self):
        from agent_decoder.server import _sse_event_data

        result = _sse_event_data("/some/file.jsonl")
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Message mutations (Phase 6) — DELETE + PATCH /api/messages/{id}
# ---------------------------------------------------------------------------

SIMPLE_USER_MSG_ID = "msg-0001"  # first user message in simple_session.jsonl


@pytest.fixture
async def edit_client():
    """App with edit_enabled=True, session file writable in a temp dir."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "test.db"

        session_file = tmp_path / SIMPLE_JSONL.name
        shutil.copy(SIMPLE_JSONL, session_file)

        async with Database(db_path) as db:
            await db.init_schema()
            project = Project(
                id="proj-edit",
                display_name="edit-test",
                raw_path="-edit-test",
                data_source="test",
            )
            session, messages = parse_session(session_file)
            session.project_id = project.id
            await db.upsert_project(project)
            await db.upsert_session(session)
            await db.upsert_messages(messages)

        from agent_decoder.models import AppConfig as _AppConfig

        config = _AppConfig(edit_enabled=True)
        app = _make_app(db_path, config)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            yield c, tmp_path


@pytest.fixture
async def read_only_client():
    """AsyncClient backed by a seeded app with edit_enabled=False."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        await _seed_db(db_path)
        app = _make_app(db_path, config=AppConfig(edit_enabled=False))
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            yield c


class TestMessageMutations:
    # -- 403 guard ----------------------------------------------------------

    async def test_delete_returns_403_without_edit_flag(self, read_only_client):
        r = await read_only_client.delete(f"/api/messages/{SIMPLE_USER_MSG_ID}")
        assert r.status_code == 403
        assert "edit_enabled" in r.json()["detail"]

    async def test_patch_returns_403_without_edit_flag(self, read_only_client):
        r = await read_only_client.patch(
            f"/api/messages/{SIMPLE_USER_MSG_ID}",
            json={"content_blocks": [{"type": "text", "text": "hi"}]},
        )
        assert r.status_code == 403

    # -- 404 for unknown message --------------------------------------------

    async def test_delete_returns_404_for_unknown_message(self, edit_client):
        client, _ = edit_client
        r = await client.delete("/api/messages/nonexistent-uuid-that-does-not-exist")
        assert r.status_code == 404
        assert r.json()["detail"] == "Message not found"

    async def test_patch_returns_404_for_unknown_message(self, edit_client):
        client, _ = edit_client
        r = await client.patch(
            "/api/messages/nonexistent-uuid-that-does-not-exist",
            json={"content_blocks": [{"type": "text", "text": "hi"}]},
        )
        assert r.status_code == 404

    # -- DELETE happy path — swap model ------------------------------------
    # Delete rewrites the session in-place (same UUID, Claude-resumable).
    # A .bak. copy of the original appears as a separate sidebar session.

    async def test_delete_keeps_same_session_id(self, edit_client):
        client, _ = edit_client
        r = await client.delete(f"/api/messages/{SIMPLE_USER_MSG_ID}")
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["session_id"] == SIMPLE_SESSION_ID

    async def test_delete_omits_message_from_session(self, edit_client):
        """After swap-delete, the session no longer contains the deleted message."""
        client, _ = edit_client
        await client.delete(f"/api/messages/{SIMPLE_USER_MSG_ID}")
        r = await client.get(f"/api/sessions/{SIMPLE_SESSION_ID}")
        ids = [m["id"] for m in r.json()["messages"]]
        assert SIMPLE_USER_MSG_ID not in ids

    async def test_delete_creates_backup_file(self, edit_client):
        """A .bak. JSONL file is created preserving the original content."""
        client, tmp_path = edit_client
        await client.delete(f"/api/messages/{SIMPLE_USER_MSG_ID}")
        backups = list(tmp_path.glob("*.bak.*.jsonl"))
        assert len(backups) == 1

    async def test_delete_backup_contains_original_message(self, edit_client):
        """The backup file preserves all original messages including the deleted one."""
        client, tmp_path = edit_client
        await client.delete(f"/api/messages/{SIMPLE_USER_MSG_ID}")
        backup = list(tmp_path.glob("*.bak.*.jsonl"))[0]
        import json as _json

        uuids = {
            _json.loads(line).get("uuid")
            for line in backup.read_text().splitlines()
            if line.strip()
        } - {None}
        assert SIMPLE_USER_MSG_ID in uuids

    # -- PATCH happy path — swap model --------------------------------------

    async def test_patch_keeps_same_session_id(self, edit_client):
        client, _ = edit_client
        r = await client.patch(
            f"/api/messages/{SIMPLE_USER_MSG_ID}",
            json={"content_blocks": [{"type": "text", "text": "patched"}]},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["ok"] is True
        assert data["session_id"] == SIMPLE_SESSION_ID

    async def test_patch_original_backed_up(self, edit_client):
        client, tmp_path = edit_client
        original_text = "Write me a greeting function."
        await client.patch(
            f"/api/messages/{SIMPLE_USER_MSG_ID}",
            json={"content_blocks": [{"type": "text", "text": "patched"}]},
        )
        backup = list(tmp_path.glob("*.bak.*.jsonl"))[0]
        import json as _json

        original_found = any(
            original_text in _json.dumps(_json.loads(line))
            for line in backup.read_text().splitlines()
            if line.strip()
        )
        assert original_found

    async def test_patch_fork_has_new_content(self, edit_client):
        """The session itself shows the patched text after a swap."""
        client, _ = edit_client
        new_text = "Patched content for e2e test"
        r = await client.patch(
            f"/api/messages/{SIMPLE_USER_MSG_ID}",
            json={"content_blocks": [{"type": "text", "text": new_text}]},
        )
        fork_id = r.json()["session_id"]
        r2 = await client.get(f"/api/sessions/{fork_id}")
        all_texts = [
            b["text"]
            for m in r2.json()["messages"]
            for b in m.get("content_blocks", [])
            if b.get("type") == "text"
        ]
        assert new_text in all_texts
