"""SQLite database layer — indexing, caching, FTS5 search.

Contract (for Agent 1 to implement):
  Database class (async context manager):
    - __aenter__ / __aexit__ manage the aiosqlite connection
    - init_schema() — CREATE TABLE IF NOT EXISTS for all tables
    - upsert_project(project: Project) -> None
    - upsert_session(session: Session) -> None
    - upsert_messages(messages: list[Message]) -> None  — bulk upsert + FTS index
    - get_projects() -> list[Project]
    - get_sessions(project_id: str | None) -> list[Session]
    - get_session_detail(session_id: str) -> SessionDetail | None
    - search(query: str, project_id: str | None, limit: int) -> list[SearchHit]
    - get_stats() -> StatsResponse
    - get_session_mtime(session_id: str) -> float | None  — for cache invalidation

Schema:
  projects   (id TEXT PK, display_name, raw_path, resolved_path, data_source, last_activity_at)
  sessions   (id TEXT PK, project_id, file_path, title, model, started_at, updated_at,
              message_count, user_message_count, cwd, git_branch, is_worktree,
              permission_mode, file_mtime REAL)
  messages   (id TEXT PK, session_id, parent_id, role, content_json, timestamp, model,
              is_sidechain, is_meta, cwd, git_branch, source_tool_assistant_uuid,
              usage_json TEXT)
  messages_fts  (FTS5 virtual: content, session_id UNINDEXED, message_id UNINDEXED, role UNINDEXED)
  messages_fts_map  (message_id TEXT PK, fts_rowid, session_id) — rowid sidecar so
              session/message-scoped FTS deletes are index seeks, not full scans

SOLID notes:
  - Dependency Inversion: callers depend on this interface, not on aiosqlite directly
  - All DB path logic lives here; callers pass a db_path: Path argument
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiosqlite

from .query_parser import filter_sql, parse_query
from .models import (
    ContentBlock,
    ImageBlock,
    Message,
    Project,
    SearchHit,
    Session,
    SessionDetail,
    StatsResponse,
    TextBlock,
    ThinkingBlock,
    TokenUsage,
    ToolResultBlock,
    ToolUseBlock,
)

# ---------------------------------------------------------------------------
# Content block (de)serialization helpers
# ---------------------------------------------------------------------------


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _serialize_content_blocks(blocks: list[ContentBlock]) -> str:
    return json.dumps([b.model_dump() for b in blocks])


def _deserialize_content_block(d: dict) -> ContentBlock:
    t = d.get("type")
    match t:
        case "text":
            return TextBlock(**d)
        case "tool_use":
            return ToolUseBlock(**d)
        case "tool_result":
            return ToolResultBlock(**d)
        case "thinking":
            return ThinkingBlock(**d)
        case "image":
            return ImageBlock(**d)
        case _:
            return TextBlock(text=str(d))


def _deserialize_content_blocks(content_json: str) -> list[ContentBlock]:
    try:
        raw = json.loads(content_json)
        return [_deserialize_content_block(d) for d in raw]
    except (json.JSONDecodeError, TypeError):
        return []


def _extract_text_for_fts(blocks: list[ContentBlock]) -> str:
    """Extract plain text from content blocks for FTS indexing."""
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, TextBlock) and block.text:
            parts.append(block.text)
    return " ".join(parts)


def _result_char_count(block: ToolResultBlock) -> int:
    """Char count of a tool_result's content. Mirrors
    ``analytics.tips._result_char_count``; the oversized-tip parity test
    (tests/analytics/test_fast_parity.py) guards the two against drift."""
    if block.content is None:
        return 0
    if isinstance(block.content, str):
        return len(block.content)
    return sum(
        len(item.get("text", "")) for item in block.content if isinstance(item, dict)
    )


def _block_facts(
    message_id: str, session_id: str, blocks: list[ContentBlock]
) -> list[tuple]:
    """Materialized ``message_blocks`` rows for one message: one tuple per
    tool_use / tool_result block, in content-block order. Field extraction
    mirrors the scanners (tool name, file_path-or-path, result char count) so
    SQL aggregation over ``message_blocks`` equals the in-memory scanners.
    """
    facts: list[tuple] = []
    for i, block in enumerate(blocks):
        if isinstance(block, ToolUseBlock):
            path = block.input.get("file_path") or block.input.get("path") or ""
            facts.append(
                (
                    message_id,
                    session_id,
                    i,
                    "tool_use",
                    block.name,
                    path if isinstance(path, str) and path else None,
                    None,
                    None,
                )
            )
        elif isinstance(block, ToolResultBlock):
            facts.append(
                (
                    message_id,
                    session_id,
                    i,
                    "tool_result",
                    None,
                    None,
                    _result_char_count(block),
                    block.tool_use_id,
                )
            )
    return facts


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------


def _dt_to_str(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.isoformat()


def _str_to_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# messages upsert helpers (incremental)
# ---------------------------------------------------------------------------

# Column order for the messages row tuple. content_hash is last and derived
# (not read off the Message) — it is appended by upsert_messages.
_MESSAGE_COLUMNS: tuple[str, ...] = (
    "id",
    "session_id",
    "parent_id",
    "role",
    "content_json",
    "timestamp",
    "model",
    "is_sidechain",
    "is_meta",
    "cwd",
    "git_branch",
    "source_tool_assistant_uuid",
    "usage_json",
    "input_tokens",
    "output_tokens",
    "cache_creation_tokens",
    "cache_read_tokens",
    "content_hash",
)

# Upsert by primary key: UPDATE-in-place on conflict (preserves rowid, so the
# "timestamp ASC, rowid ASC" tie-break in get_session_detail stays stable when
# only some same-timestamp rows change), INSERT with a fresh rowid otherwise.
_UPSERT_MESSAGE_SQL = (
    f"INSERT INTO messages ({', '.join(_MESSAGE_COLUMNS)}) "
    f"VALUES ({', '.join('?' * len(_MESSAGE_COLUMNS))}) "
    "ON CONFLICT(id) DO UPDATE SET "
    + ", ".join(f"{c}=excluded.{c}" for c in _MESSAGE_COLUMNS if c != "id")
)


def _message_row(m: Message) -> tuple:
    """The messages-table value tuple for *m*, minus the trailing content_hash
    (order matches :data:`_MESSAGE_COLUMNS` up to but excluding ``content_hash``)."""
    return (
        m.id,
        m.session_id,
        m.parent_id,
        m.role,
        _serialize_content_blocks(m.content_blocks),
        _dt_to_str(m.timestamp),
        m.model,
        1 if m.is_sidechain else 0,
        1 if m.is_meta else 0,
        m.cwd,
        m.git_branch,
        m.source_tool_assistant_uuid,
        m.usage.model_dump_json() if m.usage else None,
        m.usage.input_tokens if m.usage else None,
        m.usage.output_tokens if m.usage else None,
        m.usage.cache_creation_input_tokens if m.usage else None,
        m.usage.cache_read_input_tokens if m.usage else None,
    )


def _row_hash(row: tuple) -> str:
    """Stable content hash of a :func:`_message_row` tuple. Any change to any
    persisted field (content, usage/token backfill, model, flags…) changes it,
    so an unchanged re-parsed row hashes identically and is skipped."""
    return hashlib.blake2b(
        json.dumps(row, ensure_ascii=False).encode("utf-8"), digest_size=16
    ).hexdigest()


# ---------------------------------------------------------------------------
# Database class
# ---------------------------------------------------------------------------


class Database:
    """Async SQLite database wrapper.  Use as an async context manager."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None

    async def __aenter__(self) -> "Database":
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        # WAL lets readers run alongside a single writer instead of blocking
        # outright; busy_timeout makes contending writers wait briefly instead
        # of erroring. Required because every endpoint + the scanner opens its
        # own connection.
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        # A fresh connection is opened per request against a ~728 MB messages
        # table; a larger page cache + mmap avoids re-reading pages with
        # SQLite's tiny default cache.
        await self._conn.execute("PRAGMA cache_size = -65536")
        await self._conn.execute("PRAGMA mmap_size = 268435456")
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def execute(self, sql: str, params: tuple = ()) -> None:
        """Execute a single SQL statement."""
        assert self._conn is not None
        await self._conn.execute(sql, params)

    async def commit(self) -> None:
        """Commit the current transaction."""
        assert self._conn is not None
        await self._conn.commit()

    async def checkpoint(self) -> None:
        """Fold the WAL back into the main db and truncate the -wal sidecar.

        ``TRUNCATE`` checkpoints as far as possible then resets
        ``index.db-wal`` to zero bytes. It is blocked only by an open *read
        snapshot* on another connection — so it must run from a connection
        that is not sitting between reads (see ``_periodic_checkpoint`` in
        server.py). Without periodic truncation the WAL grew to ~22 GB here,
        because the default ``wal_autocheckpoint`` is passive and was pinned
        by long-lived reader connections that never released their snapshot.
        """
        assert self._conn is not None
        await self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    # -----------------------------------------------------------------------
    # Schema
    # -----------------------------------------------------------------------

    async def init_schema(self) -> None:
        """Create all tables and FTS5 virtual table if they don't exist."""
        assert self._conn is not None
        await self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                raw_path TEXT NOT NULL,
                resolved_path TEXT,
                data_source TEXT NOT NULL DEFAULT '',
                last_activity_at TEXT,
                session_count INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                file_path TEXT NOT NULL,
                title TEXT,
                model TEXT,
                started_at TEXT,
                updated_at TEXT,
                message_count INTEGER NOT NULL DEFAULT 0,
                user_message_count INTEGER NOT NULL DEFAULT 0,
                cwd TEXT,
                git_branch TEXT,
                is_worktree INTEGER NOT NULL DEFAULT 0,
                is_fork INTEGER NOT NULL DEFAULT 0,
                permission_mode TEXT,
                provider TEXT NOT NULL DEFAULT 'claude',
                file_mtime REAL,
                FOREIGN KEY (project_id) REFERENCES projects(id)
            );

            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                parent_id TEXT,
                role TEXT NOT NULL,
                content_json TEXT NOT NULL,
                timestamp TEXT,
                model TEXT,
                is_sidechain INTEGER NOT NULL DEFAULT 0,
                is_meta INTEGER NOT NULL DEFAULT 0,
                cwd TEXT,
                git_branch TEXT,
                source_tool_assistant_uuid TEXT,
                usage_json TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cache_creation_tokens INTEGER,
                cache_read_tokens INTEGER,
                content_hash TEXT,
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                content,
                session_id UNINDEXED,
                message_id UNINDEXED,
                role UNINDEXED
            );

            -- Rowid sidecar for messages_fts. FTS5 tables cannot carry
            -- secondary indexes, so `DELETE FROM messages_fts WHERE
            -- session_id = ?` (or message_id) degrades to a full scan of the
            -- whole FTS table — ~100 MB of reads per call on a mature index,
            -- and the re-index path calls it on every JSONL append. This map
            -- turns every such delete into a rowid lookup. Kept in lockstep
            -- with messages_fts by _fts_insert / _fts_delete_* only; never
            -- write the FTS table directly.
            CREATE TABLE IF NOT EXISTS messages_fts_map (
                message_id TEXT PRIMARY KEY,
                fts_rowid INTEGER NOT NULL,
                session_id TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_fts_map_session
                ON messages_fts_map (session_id);
            -- Makes MAX(fts_rowid) — the rowid allocator — an index seek.
            CREATE INDEX IF NOT EXISTS idx_fts_map_rowid
                ON messages_fts_map (fts_rowid);

            -- Speeds up the last_message_role correlated subquery on get_sessions.
            -- Without this, each session row triggered a full messages-table scan.
            CREATE INDEX IF NOT EXISTS idx_messages_session_timestamp
                ON messages (session_id, timestamp DESC);

            -- Materialized per-block facts for whole-corpus analytics: one row
            -- per tool_use / tool_result block, written at ingest so the
            -- dashboard never re-scans content_json. See analytics/fast.py.
            CREATE TABLE IF NOT EXISTS message_blocks (
                message_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                block_index INTEGER NOT NULL,
                btype TEXT NOT NULL,
                tool_name TEXT,
                file_path TEXT,
                result_chars INTEGER,
                tool_use_id TEXT,
                PRIMARY KEY (message_id, block_index)
            );
            CREATE INDEX IF NOT EXISTS idx_blocks_tool
                ON message_blocks (btype, tool_name);
            CREATE INDEX IF NOT EXISTS idx_blocks_file
                ON message_blocks (btype, file_path);
            CREATE INDEX IF NOT EXISTS idx_blocks_session
                ON message_blocks (session_id);

            CREATE TABLE IF NOT EXISTS _meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS recaps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                covers_until_message_uuid TEXT,
                dismissed INTEGER NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_recaps_session_id
                ON recaps(session_id, created_at);

            -- Per-session metadata override for renames (issue #11).
            -- Lives in a separate table because the JSONL file is the
            -- source of truth for the parsed title; this row is just an
            -- opt-in override the user typed in the sidebar.
            CREATE TABLE IF NOT EXISTS session_meta (
                session_id TEXT PRIMARY KEY,
                custom_title TEXT,
                -- Server-side persistence for the formerly-localStorage flags.
                -- NULL = not set; ISO-8601 string = wall-clock when set.
                archived_at TEXT,
                starred_at TEXT,
                viewed_at TEXT,
                updated_at TEXT NOT NULL
            );

            -- Ephemeral exchanges that never land in JSONL (e.g. /btw).
            -- Both user input and assistant response are stored here, linked
            -- via responds_to (self-FK on the paired input row).
            -- Excluded from analytics (no tokens / cost contribution).
            CREATE TABLE IF NOT EXISTS ephemeral_messages (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id    TEXT NOT NULL,
                kind          TEXT NOT NULL,
                role          TEXT NOT NULL,
                content       TEXT NOT NULL,
                responds_to   INTEGER REFERENCES ephemeral_messages(id),
                timestamp     TEXT NOT NULL,
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            );

            CREATE INDEX IF NOT EXISTS idx_ephemeral_session
                ON ephemeral_messages (session_id, timestamp);

            CREATE VIRTUAL TABLE IF NOT EXISTS ephemeral_messages_fts USING fts5(
                content,
                session_id UNINDEXED,
                kind UNINDEXED,
                ephemeral_id UNINDEXED
            );
        """)

        # FTS5 triggers must be created outside executescript because
        # CREATE TRIGGER is not supported inside a multi-statement string
        # in some SQLite versions. Use separate execute calls instead.
        await self._ensure_ephemeral_triggers()

        await self._conn.commit()
        await self._migrate_add_is_fork()
        await self._migrate_add_provider()
        await self._migrate_add_session_meta_flags()
        await self._migrate_add_content_hash()

    async def _ensure_ephemeral_triggers(self) -> None:
        """Create INSERT/UPDATE/DELETE triggers keeping ephemeral_messages_fts in sync.

        Uses IF NOT EXISTS guard so init_schema() is fully idempotent.
        Mirrors the approach used for messages_fts (manual rebuild on upsert)
        but uses SQL triggers so any direct INSERT/UPDATE/DELETE also stays
        in sync without requiring a Python-side rebuild call.
        """
        assert self._conn is not None
        await self._conn.execute("""
            CREATE TRIGGER IF NOT EXISTS ephemeral_fts_insert
            AFTER INSERT ON ephemeral_messages
            BEGIN
                INSERT INTO ephemeral_messages_fts
                    (content, session_id, kind, ephemeral_id)
                VALUES
                    (NEW.content, NEW.session_id, NEW.kind, NEW.id);
            END
        """)
        await self._conn.execute("""
            CREATE TRIGGER IF NOT EXISTS ephemeral_fts_update
            AFTER UPDATE OF content ON ephemeral_messages
            BEGIN
                DELETE FROM ephemeral_messages_fts
                    WHERE ephemeral_id = OLD.id;
                INSERT INTO ephemeral_messages_fts
                    (content, session_id, kind, ephemeral_id)
                VALUES
                    (NEW.content, NEW.session_id, NEW.kind, NEW.id);
            END
        """)
        await self._conn.execute("""
            CREATE TRIGGER IF NOT EXISTS ephemeral_fts_delete
            BEFORE DELETE ON ephemeral_messages
            BEGIN
                DELETE FROM ephemeral_messages_fts
                    WHERE ephemeral_id = OLD.id;
            END
        """)

    async def _migrate_add_is_fork(self) -> None:
        """Idempotent: add is_fork column to existing DBs."""
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT name FROM pragma_table_info('sessions') WHERE name='is_fork'"
        ) as cur:
            if not await cur.fetchone():
                await self._conn.execute(
                    "ALTER TABLE sessions ADD COLUMN is_fork INTEGER NOT NULL DEFAULT 0"
                )
                await self._conn.commit()

    async def _migrate_add_provider(self) -> None:
        """Idempotent: add provider column to existing DBs."""
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT name FROM pragma_table_info('sessions') WHERE name='provider'"
        ) as cur:
            if not await cur.fetchone():
                await self._conn.execute(
                    "ALTER TABLE sessions ADD COLUMN provider TEXT NOT NULL DEFAULT 'claude'"
                )
                await self._conn.commit()

    async def _migrate_add_content_hash(self) -> None:
        """Idempotent: add content_hash column to messages for incremental upsert.

        Pre-migration rows keep ``content_hash = NULL``; the first incremental
        ``upsert_messages`` after this runs sees NULL != freshly-computed hash
        and rewrites each row once, backfilling the column. Subsequent re-parses
        are then true deltas.
        """
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT name FROM pragma_table_info('messages') WHERE name='content_hash'"
        ) as cur:
            if not await cur.fetchone():
                await self._conn.execute(
                    "ALTER TABLE messages ADD COLUMN content_hash TEXT"
                )
                await self._conn.commit()

    async def migrate_fts_map_v1(self) -> None:
        """One-time backfill of ``messages_fts_map`` for pre-existing indexes.

        Costs exactly one scan of ``messages_fts`` — the same scan the write
        path used to pay on *every* re-index. Idempotent via a ``_meta`` flag.
        """
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT value FROM _meta WHERE key = 'fts_map_v1'"
        ) as cursor:
            if await cursor.fetchone() is not None:
                return

        # INSERT OR REPLACE, not plain INSERT: a message_id duplicated across
        # FTS rows (possible on an index written before the map existed) would
        # otherwise abort on the primary key. Last row wins; the losers are
        # dropped below so the FTS table matches the map exactly.
        await self._conn.execute(
            "INSERT OR REPLACE INTO messages_fts_map "
            "(message_id, fts_rowid, session_id) "
            "SELECT message_id, rowid, session_id FROM messages_fts"
        )
        await self._conn.execute(
            "DELETE FROM messages_fts WHERE rowid NOT IN "
            "(SELECT fts_rowid FROM messages_fts_map)"
        )
        await self._conn.execute(
            "INSERT OR REPLACE INTO _meta (key, value) VALUES ('fts_map_v1', '1')"
        )
        await self._conn.commit()

    async def _migrate_add_session_meta_flags(self) -> None:
        """Idempotent: add archived_at, starred_at, viewed_at to session_meta.

        These were originally localStorage-only on the frontend (LS.ARCHIVED,
        LS.STARRED, LS.VIEWED_AT), which meant a second browser saw stale
        state.  Moving them server-side fixes that.  Each is a nullable ISO
        timestamp: NULL = not set, string = wall-clock when last set.
        """
        assert self._conn is not None
        for col in ("archived_at", "starred_at", "viewed_at"):
            async with self._conn.execute(
                "SELECT name FROM pragma_table_info('session_meta') WHERE name=?",
                (col,),
            ) as cur:
                if not await cur.fetchone():
                    await self._conn.execute(
                        f"ALTER TABLE session_meta ADD COLUMN {col} TEXT"
                    )
        await self._conn.commit()

    async def reset_xml_title_mtimes(self) -> int:
        """Clear file_mtime for sessions whose title contains XML tags.

        This forces the scanner to re-parse those files on the next startup,
        picking up the fixed title-inference logic in parser.py.
        Returns the number of sessions reset.
        """
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT id, title FROM sessions WHERE title LIKE '%<%'"
        ) as cursor:
            rows = await cursor.fetchall()

        bad_ids = [row["id"] for row in rows if row["title"] and "<" in row["title"]]
        if bad_ids:
            placeholders = ",".join("?" * len(bad_ids))
            await self._conn.execute(
                f"UPDATE sessions SET file_mtime = NULL WHERE id IN ({placeholders})",
                bad_ids,
            )
            await self._conn.commit()
        return len(bad_ids)

    async def reset_truncated_titles(self) -> int:
        """One-shot: clear file_mtime for all sessions so titles get re-parsed.

        Originally added when the 80-char title cap was removed. Without
        the _meta guard this fired on every startup and forced every
        session's JSONL to re-parse on next access — blocking get_session
        for 10s+ per large file. The guard makes it a real one-shot.
        """
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT value FROM _meta WHERE key = 'truncated_titles_reset_v1'"
        ) as cursor:
            row = await cursor.fetchone()
        if row is not None:
            return 0  # already migrated
        cursor = await self._conn.execute("UPDATE sessions SET file_mtime = NULL")
        await self._conn.execute(
            "INSERT OR REPLACE INTO _meta (key, value) VALUES ('truncated_titles_reset_v1', '1')"
        )
        await self._conn.commit()
        return cursor.rowcount

    async def migrate_project_id_v2(self) -> None:
        """Mark the project ID v2 migration as done.

        The project ID hash now includes data_source to avoid collisions
        across profiles. Old sessions keep their existing project_ids until
        they're naturally re-scanned (file change, manual refresh).
        """
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT value FROM _meta WHERE key = 'project_id_v2'"
        ) as cursor:
            row = await cursor.fetchone()
        if row is not None:
            return  # already migrated
        await self._conn.execute(
            "INSERT OR REPLACE INTO _meta (key, value) VALUES ('project_id_v2', '1')"
        )
        await self._conn.commit()

    async def migrate_materialize_v1(self) -> None:
        """One-time backfill of the materialized analytics columns + table for
        databases created before they existed. Idempotent via a ``_meta`` flag.

        Token columns come straight from ``usage_json`` (an exact, cheap
        ``UPDATE``). ``message_blocks`` is rebuilt by streaming each message's
        ``content_json`` through the SAME extractor the ingest path uses, so
        the backfilled rows match what new writes produce. This reads the whole
        corpus once — the only expensive content pass, and it happens exactly
        once per database.
        """
        assert self._conn is not None
        # Pre-existing messages tables won't have the token columns yet.
        async with self._conn.execute("PRAGMA table_info(messages)") as cursor:
            cols = {r["name"] for r in await cursor.fetchall()}
        for col in (
            "input_tokens",
            "output_tokens",
            "cache_creation_tokens",
            "cache_read_tokens",
        ):
            if col not in cols:
                await self._conn.execute(
                    f"ALTER TABLE messages ADD COLUMN {col} INTEGER"
                )

        async with self._conn.execute(
            "SELECT value FROM _meta WHERE key = 'materialize_v1'"
        ) as cursor:
            if await cursor.fetchone() is not None:
                return  # already backfilled

        await self._conn.execute(
            """
            UPDATE messages SET
                input_tokens = json_extract(usage_json,'$.input_tokens'),
                output_tokens = json_extract(usage_json,'$.output_tokens'),
                cache_creation_tokens =
                    json_extract(usage_json,'$.cache_creation_input_tokens'),
                cache_read_tokens =
                    json_extract(usage_json,'$.cache_read_input_tokens')
            WHERE usage_json IS NOT NULL
            """
        )

        insert_sql = (
            "INSERT OR REPLACE INTO message_blocks (message_id, session_id, "
            "block_index, btype, tool_name, file_path, result_chars, tool_use_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        )
        await self._conn.execute("DELETE FROM message_blocks")
        batch: list[tuple] = []
        async with self._conn.execute(
            "SELECT id, session_id, content_json FROM messages"
        ) as cursor:
            async for row in cursor:
                facts = _block_facts(
                    row["id"],
                    row["session_id"],
                    _deserialize_content_blocks(row["content_json"]),
                )
                batch.extend(facts)
                if len(batch) >= 5000:
                    await self._conn.executemany(insert_sql, batch)
                    batch.clear()
        if batch:
            await self._conn.executemany(insert_sql, batch)

        await self._conn.execute(
            "INSERT OR REPLACE INTO _meta (key, value) VALUES ('materialize_v1', '1')"
        )
        await self._conn.commit()

    # -----------------------------------------------------------------------
    # Projects
    # -----------------------------------------------------------------------

    async def upsert_project(self, project: Project) -> None:
        assert self._conn is not None
        await self._conn.execute(
            """
            INSERT OR REPLACE INTO projects
                (id, display_name, raw_path, resolved_path, data_source,
                 last_activity_at, session_count)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                project.id,
                project.display_name,
                project.raw_path,
                project.resolved_path,
                project.data_source,
                _dt_to_str(project.last_activity_at),
                project.session_count,
            ),
        )
        await self._conn.commit()

    async def get_projects(
        self, data_sources: Optional[list[str]] = None
    ) -> list[Project]:
        assert self._conn is not None
        if data_sources is not None:
            placeholders = ",".join("?" * len(data_sources))
            query = (
                f"SELECT * FROM projects WHERE data_source IN ({placeholders}) "
                "ORDER BY last_activity_at DESC NULLS LAST"
            )
            params: tuple = tuple(data_sources)
        else:
            query = "SELECT * FROM projects ORDER BY last_activity_at DESC NULLS LAST"
            params = ()
        async with self._conn.execute(query, params) as cursor:
            rows = await cursor.fetchall()
        return [
            Project(
                id=row["id"],
                display_name=row["display_name"],
                raw_path=row["raw_path"],
                resolved_path=row["resolved_path"],
                data_source=row["data_source"] or "",
                last_activity_at=_str_to_dt(row["last_activity_at"]),
                session_count=row["session_count"],
            )
            for row in rows
        ]

    # -----------------------------------------------------------------------
    # Sessions
    # -----------------------------------------------------------------------

    async def upsert_session(
        self, session: Session, file_mtime: Optional[float] = None
    ) -> None:
        """Upsert a session record.

        Args:
            session:    The ``Session`` to store or update.
            file_mtime: The file's ``stat().st_mtime`` — stored for cache
                        invalidation and retrievable via ``get_session_mtime``.
        """
        assert self._conn is not None
        await self._conn.execute(
            """
            INSERT OR REPLACE INTO sessions
                (id, project_id, file_path, title, model, started_at, updated_at,
                 message_count, user_message_count, cwd, git_branch, is_worktree,
                 is_fork, permission_mode, provider, file_mtime)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session.id,
                session.project_id,
                session.file_path,
                session.title,
                session.model,
                _dt_to_str(session.started_at),
                _dt_to_str(session.updated_at),
                session.message_count,
                session.user_message_count,
                session.cwd,
                session.git_branch,
                1 if session.is_worktree else 0,
                1 if session.is_fork else 0,
                session.permission_mode,
                session.provider,
                file_mtime,
            ),
        )
        await self._conn.commit()

    async def get_sessions(
        self, project_id: Optional[str] = None, data_sources: Optional[list[str]] = None
    ) -> list[Session]:
        assert self._conn is not None
        _lmr = (
            "(SELECT role FROM messages WHERE session_id = s.id "
            "ORDER BY timestamp DESC LIMIT 1) AS last_message_role"
        )
        conditions = ["s.title IS NOT NULL"]
        params: list = []
        if project_id is not None:
            conditions.append("s.project_id = ?")
            params.append(project_id)
        if data_sources is not None:
            placeholders = ",".join("?" * len(data_sources))
            conditions.append(
                f"s.project_id IN (SELECT id FROM projects WHERE data_source IN ({placeholders}))"
            )
            params.extend(data_sources)
        where = " AND ".join(conditions)
        # LEFT JOIN session_meta so each session row already carries any
        # server-side rename + archive/star/viewed flags on first paint.
        query = (
            f"SELECT s.*, {_lmr}, "
            "sm.custom_title AS custom_title, "
            "sm.archived_at AS archived_at, "
            "sm.starred_at AS starred_at, "
            "sm.viewed_at AS viewed_at "
            "FROM sessions s LEFT JOIN session_meta sm ON sm.session_id = s.id "
            f"WHERE {where} ORDER BY s.updated_at DESC"
        )
        async with self._conn.execute(query, tuple(params)) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_session(row) for row in rows]

    async def clear_all_mtimes(self) -> None:
        """Reset stored file mtimes so the next scan re-parses every session."""
        assert self._conn is not None
        await self._conn.execute("UPDATE sessions SET file_mtime = NULL")
        await self._conn.commit()

    async def get_session_mtime(self, session_id: str) -> Optional[float]:
        """Return stored file mtime for cache-busting, or None if not indexed."""
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT file_mtime FROM sessions WHERE id = ?", (session_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return row["file_mtime"]

    async def get_session_file_path(self, session_id: str) -> Optional[str]:
        """Return the JSONL file path for a session, or None if not indexed."""
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT file_path FROM sessions WHERE id = ?", (session_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        return row["file_path"]

    async def touch_session_updated_at(
        self, session_id: str, when: Optional[datetime] = None
    ) -> Optional[datetime]:
        """Force ``sessions.updated_at`` to ``when`` (default: now, UTC).

        Used after edit/delete swaps where the JSONL's parsed
        ``updated_at`` (max of remaining message timestamps) would either
        stay flat (content edit) or regress (delete of latest), missing
        the mutation for downstream SSE/dedupe consumers that key on
        ``detail.updated_at``.

        Only advances the value — never moves it backwards — to preserve
        the "most recent activity" invariant if the parsed timestamp is
        already newer than ``when`` (shouldn't happen in practice, but
        defensive).

        Returns the value actually stored (or ``None`` if the session
        row does not exist).
        """
        assert self._conn is not None
        if when is None:
            when = datetime.now().astimezone()
        when_str = _dt_to_str(when)
        async with self._conn.execute(
            "SELECT updated_at FROM sessions WHERE id = ?", (session_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        current = _str_to_dt(row["updated_at"])
        # Only advance — never regress.
        if current is not None and current.tzinfo is None:
            current = current.replace(tzinfo=when.tzinfo)
        if current is not None and current >= when:
            return current
        await self._conn.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?",
            (when_str, session_id),
        )
        await self._conn.commit()
        return when

    # -----------------------------------------------------------------------
    # Session metadata override (rename — issue #11)
    # -----------------------------------------------------------------------

    async def set_custom_title(
        self, session_id: str, title: Optional[str]
    ) -> Optional[str]:
        """Upsert (or clear) the user-supplied title for a session.

        Passing ``None`` deletes the row, restoring the parsed title. Returns
        the normalised value actually stored (None when cleared).
        """
        assert self._conn is not None
        normalised: Optional[str] = None
        if title is not None:
            stripped = title.strip()
            normalised = stripped or None
        if normalised is None:
            await self._conn.execute(
                "DELETE FROM session_meta WHERE session_id = ?", (session_id,)
            )
        else:
            await self._conn.execute(
                """
                INSERT INTO session_meta (session_id, custom_title, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    custom_title = excluded.custom_title,
                    updated_at = excluded.updated_at
                """,
                (session_id, normalised, datetime.now().isoformat()),
            )
        await self._conn.commit()
        return normalised

    async def get_custom_title(self, session_id: str) -> Optional[str]:
        """Return the stored override for ``session_id`` or None if absent."""
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT custom_title FROM session_meta WHERE session_id = ?",
            (session_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return row["custom_title"] if row else None

    # -----------------------------------------------------------------------
    # Session flags — archived / starred / viewed
    #
    # All three live in ``session_meta`` as nullable ISO timestamps so the FE
    # gets the same shape it expects (boolean for archived/starred, optional
    # timestamp for viewed-at).  Originally localStorage-only — moved
    # server-side so a second browser sees consistent state.
    # -----------------------------------------------------------------------

    async def _set_meta_flag(
        self, session_id: str, column: str, value: Optional[str]
    ) -> Optional[str]:
        """Internal: upsert a single nullable timestamp column on session_meta.

        ``column`` is validated against a fixed allow-list to keep this off
        the user-input attack surface.
        """
        assert self._conn is not None
        if column not in {"archived_at", "starred_at", "viewed_at"}:
            raise ValueError(f"unsupported session_meta column: {column!r}")
        now_iso = datetime.now().isoformat()
        # Use a parameterised query for the value; the column name is f-string
        # interpolated but allow-listed above.  ON CONFLICT preserves the
        # other flag columns + custom_title — only the target column updates.
        await self._conn.execute(
            f"""
            INSERT INTO session_meta (session_id, {column}, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                {column} = excluded.{column},
                updated_at = excluded.updated_at
            """,
            (session_id, value, now_iso),
        )
        await self._conn.commit()
        return value

    async def set_archived(self, session_id: str, archived: bool) -> Optional[str]:
        """Mark a session archived (True) or unarchive it (False).

        Returns the stored timestamp (ISO string when archived, None when
        cleared).  An unarchive preserves the rest of the session_meta row
        (custom_title, starred_at, viewed_at).
        """
        value = datetime.now().isoformat() if archived else None
        return await self._set_meta_flag(session_id, "archived_at", value)

    async def set_starred(self, session_id: str, starred: bool) -> Optional[str]:
        """Mark a session starred (True) or unstar it (False)."""
        value = datetime.now().isoformat() if starred else None
        return await self._set_meta_flag(session_id, "starred_at", value)

    async def set_viewed_at(
        self, session_id: str, viewed_at: Optional[str]
    ) -> Optional[str]:
        """Record when the session was last viewed by the user.

        Unlike archived/starred this accepts an explicit timestamp because the
        bell-dismiss logic wants to record "viewed at message_updated_at"
        rather than "viewed at now".  Pass ``None`` to clear (mark unread).
        """
        return await self._set_meta_flag(session_id, "viewed_at", viewed_at)

    async def get_session_meta(self, session_id: str) -> dict:
        """Return the full session_meta row for ``session_id`` as a dict.

        Returns ``{"custom_title": None, "archived_at": None, "starred_at": None,
        "viewed_at": None}`` if no row exists — callers can check truthiness
        on each field rather than handling missing-row separately.
        """
        assert self._conn is not None
        async with self._conn.execute(
            """
            SELECT custom_title, archived_at, starred_at, viewed_at
            FROM session_meta WHERE session_id = ?
            """,
            (session_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return {
                "custom_title": None,
                "archived_at": None,
                "starred_at": None,
                "viewed_at": None,
            }
        return {
            "custom_title": row["custom_title"],
            "archived_at": row["archived_at"],
            "starred_at": row["starred_at"],
            "viewed_at": row["viewed_at"],
        }

    # -----------------------------------------------------------------------
    # Messages
    # -----------------------------------------------------------------------

    # ------------------------------------------------------------------
    # messages_fts write helpers
    #
    # All writes to messages_fts go through these three so the rowid sidecar
    # (messages_fts_map) can never drift. Callers must not INSERT/DELETE the
    # FTS table directly — doing so orphans map rows and silently breaks the
    # indexed deletes below. None of these commit; the caller owns the txn.
    # ------------------------------------------------------------------

    async def _fts_next_rowid(self) -> int:
        """Return the first rowid of a free range above every live FTS row.

        Allocating from ``MAX + 1`` is what makes explicit rowids safe: no live
        row can sit in the range handed out, so an insert can never clobber
        one. Freed rowids *are* reused (a delete + reinsert in the same
        transaction typically lands on the same numbers) — that's fine, and
        cheaper than persisting a monotonic counter. Served by
        idx_fts_map_rowid. The messages_fts fallback only fires on a DB whose
        map is empty but whose FTS table is not (a half-migrated index), where
        one scan is the correct price for not corrupting it.
        """
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT COALESCE(MAX(fts_rowid), 0) FROM messages_fts_map"
        ) as cursor:
            row = await cursor.fetchone()
        high = row[0] if row else 0
        if not high:
            async with self._conn.execute(
                "SELECT COALESCE(MAX(rowid), 0) FROM messages_fts"
            ) as cursor:
                row = await cursor.fetchone()
            high = row[0] if row else 0
        return high + 1

    async def _fts_delete_session(self, session_id: str) -> None:
        """Drop every FTS row for *session_id* via idx_fts_map_session."""
        assert self._conn is not None
        await self._conn.execute(
            "DELETE FROM messages_fts WHERE rowid IN "
            "(SELECT fts_rowid FROM messages_fts_map WHERE session_id = ?)",
            (session_id,),
        )
        await self._conn.execute(
            "DELETE FROM messages_fts_map WHERE session_id = ?", (session_id,)
        )

    async def _fts_delete_messages(self, message_ids: list[str]) -> None:
        """Drop the FTS rows for *message_ids* via the map's primary key."""
        assert self._conn is not None
        if not message_ids:
            return
        await self._conn.executemany(
            "DELETE FROM messages_fts WHERE rowid IN "
            "(SELECT fts_rowid FROM messages_fts_map WHERE message_id = ?)",
            [(mid,) for mid in message_ids],
        )
        await self._conn.executemany(
            "DELETE FROM messages_fts_map WHERE message_id = ?",
            [(mid,) for mid in message_ids],
        )

    async def _fts_insert(self, rows: list[tuple[str, str, str, str]]) -> None:
        """Insert (content, session_id, message_id, role) rows + their map rows.

        Rowids are allocated explicitly rather than left to FTS5 so a single
        executemany can populate both tables — executemany exposes no
        per-row lastrowid.

        Deduped by message_id, last wins, mirroring the ``INSERT OR REPLACE``
        the ``messages`` table uses: a resumed or repaired JSONL can repeat a
        message uuid, and two FTS rows sharing one map row (primary key on
        message_id) would orphan the loser and break the map == FTS invariant
        the indexed deletes rely on.
        """
        assert self._conn is not None
        if not rows:
            return
        rows = list({row[2]: row for row in rows}.values())
        start = await self._fts_next_rowid()
        await self._conn.executemany(
            "INSERT INTO messages_fts (rowid, content, session_id, message_id, role) "
            "VALUES (?, ?, ?, ?, ?)",
            [(start + i, *row) for i, row in enumerate(rows)],
        )
        await self._conn.executemany(
            "INSERT OR REPLACE INTO messages_fts_map "
            "(message_id, fts_rowid, session_id) VALUES (?, ?, ?)",
            [(row[2], start + i, row[1]) for i, row in enumerate(rows)],
        )

    async def upsert_messages(self, messages: list[Message]) -> None:
        """Incrementally upsert one session's messages + FTS/block-fact indexes.

        Only rows whose persisted content actually changed since the last index
        are rewritten — detected by comparing a per-row ``content_hash`` against
        the stored one — instead of rewriting every row and rebuilding the whole
        session's FTS on each re-parse. On a large, mostly-appended session this
        turns a full O(N) rewrite (dominated by re-writing every content_json
        blob) into an O(changed) delta.

        Correctness:
        - New rows and rows whose hash differs (content edits, streaming
          completion, usage/token backfill, model/flag changes) are re-upserted.
        - Rows that vanished from the session (compaction/history rewrite) are
          hard-deleted, along with their FTS + block-fact entries.
        - Rows with a stored hash of NULL (pre-``content_hash``-migration) always
          count as changed, so the first re-parse after migrating backfills the
          column — a freshly-indexed or just-migrated session behaves exactly
          like the old bulk path.
        The upsert is UPDATE-in-place (ON CONFLICT), preserving each unchanged
        row's rowid so the ``timestamp ASC, rowid ASC`` ordering stays stable.
        """
        assert self._conn is not None
        if not messages:
            return

        # All messages belong to one session.
        session_id = messages[0].session_id

        # Build each row once and hash it; diff against the stored hashes.
        rows = {m.id: _message_row(m) for m in messages}
        hashes = {mid: _row_hash(row) for mid, row in rows.items()}

        async with self._conn.execute(
            "SELECT id, content_hash FROM messages WHERE session_id = ?",
            (session_id,),
        ) as cur:
            stored = {r["id"]: r["content_hash"] for r in await cur.fetchall()}

        changed = [mid for mid, h in hashes.items() if stored.get(mid) != h]
        removed = [mid for mid in stored if mid not in rows]

        if not changed and not removed:
            return  # pure no-op re-parse — nothing to write

        # 1. Upsert only the changed/new rows (content_hash appended per column order).
        if changed:
            await self._conn.executemany(
                _UPSERT_MESSAGE_SQL,
                [rows[mid] + (hashes[mid],) for mid in changed],
            )

        # 2. Drop FTS + block facts for every touched id, then re-insert for the
        #    changed ones. message_id is UNINDEXED in fts5, so deleting by it
        #    would force a full VIRTUAL TABLE SCAN of the whole corpus on every
        #    turn — _fts_delete_messages/_fts_insert go through messages_fts_map
        #    instead, a plain-table sidecar fts5 can't carry as a real index.
        touched = changed + removed
        await self._fts_delete_messages(touched)
        for i in range(0, len(touched), 500):
            chunk = touched[i : i + 500]
            placeholders = ",".join("?" * len(chunk))
            delete_blocks_sql = (
                "DELETE FROM message_blocks WHERE message_id IN (" + placeholders + ")"
            )
            await self._conn.execute(delete_blocks_sql, chunk)

        by_id = {m.id: m for m in messages}
        fts_rows: list[tuple] = []
        block_rows: list[tuple] = []
        for mid in changed:
            m = by_id[mid]
            text = _extract_text_for_fts(m.content_blocks)
            if text.strip():
                fts_rows.append((text, m.session_id, m.id, m.role))
            block_rows.extend(_block_facts(m.id, m.session_id, m.content_blocks))

        await self._fts_insert(fts_rows)
        if block_rows:
            await self._conn.executemany(
                "INSERT OR REPLACE INTO message_blocks (message_id, session_id, "
                "block_index, btype, tool_name, file_path, result_chars, tool_use_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                block_rows,
            )

        # 3. Hard-delete rows that vanished from the session (compaction/rewrite).
        for i in range(0, len(removed), 500):
            chunk = removed[i : i + 500]
            placeholders = ",".join("?" * len(chunk))
            await self._conn.execute(
                f"DELETE FROM messages WHERE id IN ({placeholders})", chunk
            )

        await self._conn.commit()

    async def merge_messages(self, messages: list[Message]) -> None:
        """Merge *messages* into a session that already has OTHER messages indexed.

        ``upsert_messages`` treats every id currently stored for
        ``session_id`` that's absent from its argument as removed and
        hard-deletes it — correct when *messages* is the complete, freshly
        re-parsed set for a session file, but destructive if it's a partial
        set: it would wipe every sibling message not included in the call.

        Used for sub-agent (Task tool) transcript ingest
        (``include_subagent_chats``): those messages share the PARENT
        session's id (see ``parser._session_id_from_content``) but only cover
        a handful of sidechain messages, while the parent's own messages are
        already indexed separately. This variant only ever touches the ids in
        *messages*, so calling it repeatedly with a partial set never disturbs
        sibling rows. Deliberately leaves ``content_hash`` NULL — this isn't
        the incremental-upsert path, and a NULL hash just means the next
        regular ``upsert_messages`` pass over the session's own file treats
        these rows as "changed once" and backfills it, same as any
        pre-migration row.
        """
        assert self._conn is not None
        if not messages:
            return

        await self._conn.executemany(
            """
            INSERT OR REPLACE INTO messages
                (id, session_id, parent_id, role, content_json, timestamp, model,
                 is_sidechain, is_meta, cwd, git_branch, source_tool_assistant_uuid,
                 usage_json, input_tokens, output_tokens, cache_creation_tokens,
                 cache_read_tokens)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [_message_row(m) for m in messages],
        )

        # Batched, not per-message: the FTS delete used to run once per message
        # and each one full-scanned messages_fts, so a single sub-agent ingest
        # read (message count x FTS size) bytes off disk.
        await self._fts_delete_messages([m.id for m in messages])
        fts_rows = []
        for m in messages:
            text = _extract_text_for_fts(m.content_blocks)
            if text.strip():
                fts_rows.append((text, m.session_id, m.id, m.role))
        await self._fts_insert(fts_rows)

        await self._conn.executemany(
            "DELETE FROM message_blocks WHERE message_id = ?",
            [(m.id,) for m in messages],
        )
        block_rows = [
            row
            for m in messages
            for row in _block_facts(m.id, m.session_id, m.content_blocks)
        ]
        if block_rows:
            await self._conn.executemany(
                "INSERT OR REPLACE INTO message_blocks (message_id, session_id, "
                "block_index, btype, tool_name, file_path, result_chars, tool_use_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                block_rows,
            )

        await self._conn.commit()

    async def find_message_by_tool_use_id(
        self, session_id: str, tool_use_id: str
    ) -> Optional[str]:
        """Return the id of the assistant message containing a ``tool_use``
        block with the given id, scoped to *session_id*, or None.

        Used to link a sub-agent transcript back to the parent assistant
        message that spawned it (the Task tool_use call) — see the
        ``include_subagent_chats`` ingest path in server.py.

        Deliberately reads ``messages.content_json`` directly rather than
        ``message_blocks.tool_use_id``: that column only records the id a
        ``tool_result`` block is responding to, never a ``tool_use`` block's
        own id, so it cannot answer "which message contains tool_use block
        X" without a schema change and a full-corpus backfill of every
        pre-existing message — which would leave every subagent transcript
        already on disk before this feature shipped unlinkable.
        """
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT id, content_json FROM messages "
            "WHERE session_id = ? AND role = 'assistant'",
            (session_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        for row in rows:
            for block in _deserialize_content_blocks(row["content_json"]):
                if isinstance(block, ToolUseBlock) and block.id == tool_use_id:
                    return row["id"]
        return None

    async def get_session_detail_json_bytes(self, session_id: str) -> Optional[bytes]:
        """Build the SessionDetail JSON response without going through Pydantic.

        Embeds the stored ``content_json`` and ``usage_json`` strings directly
        as JSON fragments, skipping the per-message parse + Pydantic construct
        + Pydantic dump cycle that dominates response time on large chats.
        Used for the unlimited (no ``message_limit``) hot path.
        """
        assert self._conn is not None

        async with self._conn.execute(
            """SELECT s.*,
                   (SELECT role FROM messages WHERE session_id = s.id
                    ORDER BY timestamp DESC LIMIT 1) AS last_message_role,
                   sm.custom_title AS custom_title,
                   sm.archived_at AS archived_at,
                   sm.starred_at AS starred_at,
                   sm.viewed_at AS viewed_at
               FROM sessions s
               LEFT JOIN session_meta sm ON sm.session_id = s.id
               WHERE s.id = ?""",
            (session_id,),
        ) as cur:
            srow = await cur.fetchone()
        if srow is None:
            return None

        async with self._conn.execute(
            # Tie-break by rowid (insertion order = JSONL file order) so a
            # thinking + text pair claude writes at the same ms timestamp
            # surfaces in the order the model produced them. Without this,
            # SQLite's tie-break is non-deterministic and the UI's
            # "is-session-active" heuristic (which inspects the LAST message
            # of a turn) can latch the indicator on permanently when the
            # trailing message happens to be a thinking-only one.
            "SELECT * FROM messages WHERE session_id = ? ORDER BY timestamp ASC, rowid ASC",
            (session_id,),
        ) as cur:
            mrows = await cur.fetchall()

        session_dict = {
            "id": srow["id"],
            "project_id": srow["project_id"],
            "file_path": srow["file_path"],
            "title": srow["title"],
            "custom_title": srow["custom_title"],
            "archived_at": srow["archived_at"]
            if "archived_at" in srow.keys()
            else None,
            "starred_at": srow["starred_at"] if "starred_at" in srow.keys() else None,
            "viewed_at": srow["viewed_at"] if "viewed_at" in srow.keys() else None,
            "model": srow["model"],
            "started_at": srow["started_at"],
            "updated_at": srow["updated_at"],
            "message_count": srow["message_count"],
            "user_message_count": srow["user_message_count"],
            "cwd": srow["cwd"],
            "git_branch": srow["git_branch"],
            "is_worktree": bool(srow["is_worktree"]),
            "is_fork": bool(srow["is_fork"]) if "is_fork" in srow.keys() else False,
            "permission_mode": srow["permission_mode"],
            "provider": srow["provider"] if "provider" in srow.keys() else "claude",
            "last_message_role": srow["last_message_role"],
            # Live filesystem check, NOT a cached DB value. The previous
            # version read projects.resolved_path, which is set once at
            # scan time — it stayed stale (False) long after a removable
            # volume was remounted or a directory recreated. The cost is
            # one isdir() syscall per session-detail request, which is
            # cheap compared to the rest of the response build.
            "cwd_exists": (True if not srow["cwd"] else Path(srow["cwd"]).is_dir()),
        }
        session_bytes = json.dumps(
            session_dict, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")

        parts: list[bytes] = [session_bytes[:-1], b',"messages":[']
        for i, m in enumerate(mrows):
            if i > 0:
                parts.append(b",")
            header = {
                "id": m["id"],
                "session_id": m["session_id"],
                "parent_id": m["parent_id"],
                "role": m["role"],
                "timestamp": m["timestamp"],
                "model": m["model"],
                "is_sidechain": bool(m["is_sidechain"]),
                "is_meta": bool(m["is_meta"]),
                "cwd": m["cwd"],
                "git_branch": m["git_branch"],
                "request_id": None,
                "source_tool_assistant_uuid": m["source_tool_assistant_uuid"],
            }
            header_bytes = json.dumps(
                header, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            cb = (m["content_json"] or "[]").encode("utf-8")
            usage = (m["usage_json"] or "null").encode("utf-8")
            parts.append(header_bytes[:-1])
            parts.append(b',"content_blocks":')
            parts.append(cb)
            parts.append(b',"usage":')
            parts.append(usage)
            parts.append(b"}")
        parts.append(b'],"total_message_count":null}')

        return b"".join(parts)

    async def get_session_detail(
        self, session_id: str, message_limit: Optional[int] = None
    ) -> Optional[SessionDetail]:
        assert self._conn is not None

        # Fetch the session row (include last_message_role subquery for consistency)
        async with self._conn.execute(
            """SELECT s.*,
                   (SELECT role FROM messages WHERE session_id = s.id
                    ORDER BY timestamp DESC LIMIT 1) AS last_message_role,
                   sm.custom_title AS custom_title,
                   sm.archived_at AS archived_at,
                   sm.starred_at AS starred_at,
                   sm.viewed_at AS viewed_at
               FROM sessions s
               LEFT JOIN session_meta sm ON sm.session_id = s.id
               WHERE s.id = ?""",
            (session_id,),
        ) as cursor:
            session_row = await cursor.fetchone()

        if session_row is None:
            return None

        # Count total messages for truncation info
        total_count: Optional[int] = None
        if message_limit is not None:
            async with self._conn.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id = ?", (session_id,)
            ) as cursor:
                row = await cursor.fetchone()
                total_count = row[0]

        # Fetch messages ordered by timestamp, optionally capped to last N
        if (
            message_limit is not None
            and total_count is not None
            and total_count > message_limit
        ):
            # Fetch last N messages by using a subquery to get the tail
            query = (
                "SELECT * FROM messages WHERE session_id = ? "
                "ORDER BY timestamp DESC LIMIT ?"
            )
            async with self._conn.execute(query, (session_id, message_limit)) as cursor:
                msg_rows = await cursor.fetchall()
            # Reverse back to chronological order
            msg_rows = list(reversed(msg_rows))
        else:
            total_count = None  # no truncation needed
            async with self._conn.execute(
                # See get_session_detail's matching ORDER BY — same tie-break.
                "SELECT * FROM messages WHERE session_id = ? ORDER BY timestamp ASC, rowid ASC",
                (session_id,),
            ) as cursor:
                msg_rows = await cursor.fetchall()

        messages = [_row_to_message(row) for row in msg_rows]

        session = _row_to_session(session_row)
        # Live filesystem check — see ``get_session_detail_json_bytes`` for
        # the rationale (stale projects.resolved_path was reporting cwd
        # missing on volumes that had since been remounted).
        cwd_exists = True if not session.cwd else Path(session.cwd).is_dir()
        return SessionDetail(
            **session.model_dump(),
            messages=messages,
            total_message_count=total_count,
            cwd_exists=cwd_exists,
        )

    async def delete_message(self, message_id: str) -> None:
        """Delete a message, its FTS entry, and update the parent session's message counts."""
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT session_id FROM messages WHERE id = ?", (message_id,)
        ) as cursor:
            row = await cursor.fetchone()
        await self._fts_delete_messages([message_id])
        await self._conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))
        if row:
            session_id = row["session_id"]
            await self._conn.execute(
                """UPDATE sessions SET
                   message_count = (SELECT COUNT(*) FROM messages WHERE session_id = ?),
                   user_message_count = (
                       SELECT COUNT(*) FROM messages
                       WHERE session_id = ? AND role = 'user' AND is_meta = 0
                   )
                   WHERE id = ?""",
                (session_id, session_id, session_id),
            )
        await self._conn.commit()

    async def delete_session_messages(self, session_id: str) -> None:
        """Delete all messages (and their FTS entries) for a session."""
        assert self._conn is not None
        await self._fts_delete_session(session_id)
        await self._conn.execute(
            "DELETE FROM messages WHERE session_id = ?", (session_id,)
        )
        await self._conn.commit()

    async def delete_session(self, session_id: str) -> bool:
        """Hard-delete a session: its messages, FTS entries, recaps,
        session_meta override, and the session row itself — all in one
        transaction.

        Returns True if a session row was actually deleted, False if the
        session did not exist in the DB (idempotent: caller may still want
        to unlink the on-disk file).
        """
        assert self._conn is not None
        # Check existence first so we can return an accurate bool without
        # relying on rowcount after the DELETE (which is always 0-or-1 here).
        async with self._conn.execute(
            "SELECT id FROM sessions WHERE id = ?", (session_id,)
        ) as cursor:
            exists = await cursor.fetchone() is not None

        # Delete child rows in dependency order before the parent session row.
        await self._fts_delete_session(session_id)
        await self._conn.execute(
            "DELETE FROM messages WHERE session_id = ?", (session_id,)
        )
        await self._conn.execute(
            "DELETE FROM recaps WHERE session_id = ?", (session_id,)
        )
        # ephemeral_messages and its FTS table — the BEFORE DELETE trigger
        # on ephemeral_messages keeps ephemeral_messages_fts in sync, so we
        # only DELETE the parent rows here.  PRAGMA foreign_keys is off
        # project-wide; without this explicit DELETE the rows would orphan
        # on session-delete (Phase 2 live-smoke finding).
        await self._conn.execute(
            "DELETE FROM ephemeral_messages WHERE session_id = ?", (session_id,)
        )
        await self._conn.execute(
            "DELETE FROM session_meta WHERE session_id = ?", (session_id,)
        )
        await self._conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        await self._conn.commit()
        return exists

    async def update_message_content(
        self, message_id: str, new_blocks: list[ContentBlock]
    ) -> None:
        """Replace a message's content_blocks in DB and rebuild its FTS entry."""
        assert self._conn is not None
        content_json = _serialize_content_blocks(new_blocks)
        await self._conn.execute(
            "UPDATE messages SET content_json = ? WHERE id = ?",
            (content_json, message_id),
        )
        await self._fts_delete_messages([message_id])
        fts_text = _extract_text_for_fts(new_blocks)
        async with self._conn.execute(
            "SELECT session_id, role FROM messages WHERE id = ?", (message_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row and fts_text.strip():
            await self._fts_insert(
                [(fts_text, row["session_id"], message_id, row["role"])]
            )
        await self._conn.commit()

    async def get_session_file_path_for_message(self, message_id: str) -> str | None:
        """Return the file_path of the session that owns the given message."""
        result = await self.get_session_info_for_message(message_id)
        return result[1] if result else None

    async def get_session_info_for_message(
        self, message_id: str
    ) -> tuple[str, str] | None:
        """Return ``(session_id, file_path)`` for the session owning the message."""
        assert self._conn is not None
        async with self._conn.execute(
            """SELECT s.id, s.file_path FROM sessions s
               JOIN messages m ON m.session_id = s.id
               WHERE m.id = ?""",
            (message_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return (row["id"], row["file_path"]) if row else None

    async def get_all_messages(self) -> list[Message]:
        """Fetch all messages from all sessions ordered by timestamp."""
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT * FROM messages ORDER BY timestamp ASC, rowid ASC"
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_message(row) for row in rows]

    async def analytics_signature(self) -> tuple:
        """Cheap fingerprint of the message corpus for analytics caching.

        Computed over the small ``sessions`` table (one row per session),
        so it stays sub-millisecond regardless of message count. The tuple
        changes whenever a session is added, removed, or re-indexed — which
        is exactly when the all-message analytics need recomputing:
          - ``COUNT(*)``           → session added/removed
          - ``SUM(message_count)`` → messages appended within a session
          - ``MAX(file_mtime)``    → a session's JSONL was re-parsed
          - ``MAX(updated_at)``    → activity timestamp moved
        """
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(message_count), 0) AS c, "
            "MAX(file_mtime) AS m, MAX(updated_at) AS u FROM sessions"
        ) as cursor:
            row = await cursor.fetchone()
        return (row["n"], row["c"], row["m"], row["u"])

    # -----------------------------------------------------------------------
    # Search
    # -----------------------------------------------------------------------

    async def search(
        self,
        query: str,
        project_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SearchHit]:
        assert self._conn is not None

        # Each sub-query (messages/ephemeral) is sorted by timestamp DESC
        # independently, then merged and re-sorted below. To page correctly
        # past `offset`, each sub-query must fetch enough rows to cover the
        # requested page, not just `limit` from its own start.
        fetch_size = offset + limit

        # Query language (quotes, -negation, OR, prefix*, role:/project:/
        # session:/before:/after:) is compiled by query_parser into a safe
        # FTS5 MATCH expression plus plain SQL filters.
        pq = parse_query(query)
        if not pq.match:
            return []

        msg_extra, msg_fp = filter_sql(
            pq, role="f.role", project="s.project_id",
            session="f.session_id", ts="m.timestamp",
        )
        eph_extra, eph_fp = filter_sql(
            pq, role="e.role", project="s.project_id",
            session="e.session_id", ts="e.timestamp",
        )
        proj_clause = " AND s.project_id = ?" if project_id is not None else ""
        proj_params: list = [project_id] if project_id is not None else []

        msg_sql = f"""
            SELECT
                snippet(messages_fts, 0, '<b>', '</b>', '...', 20) AS snippet,
                f.session_id,
                f.message_id,
                f.role,
                s.title AS session_title,
                s.project_id,
                m.timestamp
            FROM messages_fts f
            JOIN sessions s ON s.id = f.session_id
            JOIN messages m ON m.id = f.message_id
            WHERE messages_fts MATCH ?{proj_clause}{msg_extra}
            ORDER BY m.timestamp DESC
            LIMIT ?
        """
        msg_params = (pq.match, *proj_params, *msg_fp, fetch_size)

        async with self._conn.execute(msg_sql, msg_params) as cursor:
            msg_rows = await cursor.fetchall()

        hits: list[SearchHit] = []
        for row in msg_rows:
            hits.append(
                SearchHit(
                    session_id=row["session_id"],
                    session_title=row["session_title"],
                    project_id=row["project_id"],
                    message_id=row["message_id"],
                    role=row["role"],
                    snippet=row["snippet"],
                    timestamp=_str_to_dt(row["timestamp"]),
                    source="message",
                )
            )

        # --- Ephemeral search ---
        # Ephemerals have no project_id in their own table; we join via sessions.
        eph_sql = f"""
            SELECT
                snippet(ephemeral_messages_fts, 0, '<b>', '</b>', '...', 20) AS snippet,
                e.session_id,
                CAST(e.id AS TEXT) AS message_id,
                e.role,
                s.title AS session_title,
                s.project_id,
                e.timestamp,
                e.kind,
                e.responds_to
            FROM ephemeral_messages_fts f
            JOIN ephemeral_messages e ON e.id = f.ephemeral_id
            JOIN sessions s ON s.id = e.session_id
            WHERE ephemeral_messages_fts MATCH ?{proj_clause}{eph_extra}
            ORDER BY e.timestamp DESC
            LIMIT ?
        """
        eph_params = (pq.match, *proj_params, *eph_fp, fetch_size)

        async with self._conn.execute(eph_sql, eph_params) as cursor:
            eph_rows = await cursor.fetchall()

        for row in eph_rows:
            hits.append(
                SearchHit(
                    session_id=row["session_id"],
                    session_title=row["session_title"],
                    project_id=row["project_id"],
                    message_id=row["message_id"],
                    role=row["role"],
                    snippet=row["snippet"],
                    timestamp=_str_to_dt(row["timestamp"]),
                    source="ephemeral",
                    kind=row["kind"],
                    responds_to=row["responds_to"],
                )
            )

        # Merge: sort all hits by timestamp DESC (None timestamps sort last).
        hits.sort(key=lambda h: h.timestamp or datetime.min, reverse=True)
        return hits[offset : offset + limit]

    # -----------------------------------------------------------------------
    # Stats
    # -----------------------------------------------------------------------

    async def get_stats(self) -> StatsResponse:
        assert self._conn is not None

        async with self._conn.execute("SELECT COUNT(*) AS cnt FROM projects") as cur:
            projects_count = (await cur.fetchone())["cnt"]

        async with self._conn.execute("SELECT COUNT(*) AS cnt FROM sessions") as cur:
            sessions_count = (await cur.fetchone())["cnt"]

        async with self._conn.execute("SELECT COUNT(*) AS cnt FROM messages") as cur:
            messages_count = (await cur.fetchone())["cnt"]

        return StatsResponse(
            total_projects=projects_count,
            total_sessions=sessions_count,
            total_messages=messages_count,
            data_paths=[],  # filled in by server.py
        )

    # -----------------------------------------------------------------------
    # Recaps
    # -----------------------------------------------------------------------

    async def insert_recap(
        self,
        session_id: str,
        text: str,
        covers_until_message_uuid: Optional[str],
    ) -> int:
        assert self._conn is not None
        created_at = datetime.now().isoformat()
        cursor = await self._conn.execute(
            """
            INSERT INTO recaps (session_id, text, created_at, covers_until_message_uuid)
            VALUES (?, ?, ?, ?)
            """,
            (session_id, text, created_at, covers_until_message_uuid),
        )
        await self._conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def list_recaps(
        self, session_id: str, *, include_dismissed: bool = False
    ) -> list[dict]:
        assert self._conn is not None
        if include_dismissed:
            sql = (
                "SELECT id, session_id, text, created_at, "
                "covers_until_message_uuid, dismissed "
                "FROM recaps WHERE session_id = ? ORDER BY created_at ASC, id ASC"
            )
            params: tuple = (session_id,)
        else:
            sql = (
                "SELECT id, session_id, text, created_at, "
                "covers_until_message_uuid, dismissed "
                "FROM recaps WHERE session_id = ? AND dismissed = 0 "
                "ORDER BY created_at ASC, id ASC"
            )
            params = (session_id,)
        async with self._conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [
            {
                "id": row["id"],
                "session_id": row["session_id"],
                "text": row["text"],
                "created_at": row["created_at"],
                "covers_until_message_uuid": row["covers_until_message_uuid"],
                "dismissed": bool(row["dismissed"]),
            }
            for row in rows
        ]

    async def dismiss_recap(self, recap_id: int) -> bool:
        assert self._conn is not None
        cursor = await self._conn.execute(
            "UPDATE recaps SET dismissed = 1 WHERE id = ?", (recap_id,)
        )
        await self._conn.commit()
        return cursor.rowcount > 0

    # -----------------------------------------------------------------------
    # Ephemeral messages (/btw and similar side-channel exchanges)
    # -----------------------------------------------------------------------

    async def record_ephemeral_input(
        self,
        session_id: str,
        content: str,
        kind: str = "btw",
        timestamp: Optional[str] = None,
    ) -> int:
        """Persist a user-side ephemeral message (e.g. /btw input).

        Returns the new row id.  ``timestamp`` defaults to now (ISO format,
        same as other tables).  The INSERT trigger keeps ``ephemeral_messages_fts``
        in sync automatically.
        """
        assert self._conn is not None
        ts = timestamp if timestamp is not None else _utc_now_iso()
        cursor = await self._conn.execute(
            """
            INSERT INTO ephemeral_messages
                (session_id, kind, role, content, responds_to, timestamp)
            VALUES (?, ?, 'user', ?, NULL, ?)
            """,
            (session_id, kind, content, ts),
        )
        await self._conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def record_ephemeral_response(
        self,
        input_row_id: int,
        content: str,
        timestamp: Optional[str] = None,
    ) -> int:
        """Persist an assistant-side ephemeral response paired with *input_row_id*.

        Inherits ``session_id`` and ``kind`` from the input row to guarantee
        consistency (avoids the caller having to re-pass them and risk skew).
        Raises ``aiosqlite.IntegrityError`` (wrapping ``sqlite3.IntegrityError``)
        if *input_row_id* does not exist — the self-FK is enforced explicitly
        via the lookup below rather than relying on SQLite FK pragma being on.

        Returns the new row id.
        """
        assert self._conn is not None
        async with self._conn.execute(
            "SELECT session_id, kind FROM ephemeral_messages WHERE id = ?",
            (input_row_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            import sqlite3

            raise sqlite3.IntegrityError(
                f"record_ephemeral_response: input_row_id {input_row_id!r} does not exist"
            )
        session_id = row["session_id"]
        kind = row["kind"]
        ts = timestamp if timestamp is not None else _utc_now_iso()
        cursor = await self._conn.execute(
            """
            INSERT INTO ephemeral_messages
                (session_id, kind, role, content, responds_to, timestamp)
            VALUES (?, ?, 'assistant', ?, ?, ?)
            """,
            (session_id, kind, content, input_row_id, ts),
        )
        await self._conn.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def get_ephemeral_messages(self, session_id: str) -> list[dict]:
        """Return all ephemeral messages for *session_id* ordered by timestamp.

        Returns plain dicts (mirroring ``list_recaps``) so callers don't need
        to import a separate Pydantic model.
        """
        assert self._conn is not None
        async with self._conn.execute(
            """
            SELECT id, session_id, kind, role, content, responds_to, timestamp
            FROM ephemeral_messages
            WHERE session_id = ?
            ORDER BY timestamp ASC, id ASC
            """,
            (session_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            {
                "id": row["id"],
                "session_id": row["session_id"],
                "kind": row["kind"],
                "role": row["role"],
                "content": row["content"],
                "responds_to": row["responds_to"],
                "timestamp": row["timestamp"],
            }
            for row in rows
        ]

    async def search_ephemeral(
        self,
        query: str,
        session_id: Optional[str] = None,
    ) -> list[dict]:
        """FTS5 search over ephemeral messages.

        Sanitises the query the same way ``search()`` does (strips FTS5
        operators).  Optional *session_id* narrows to a single session.
        Returns plain dicts matching the shape of ``get_ephemeral_messages``.
        """
        assert self._conn is not None
        pq = parse_query(query)
        if not pq.match:
            return []
        sanitized = pq.match

        if session_id is not None:
            sql = """
                SELECT
                    e.id, e.session_id, e.kind, e.role,
                    e.content, e.responds_to, e.timestamp
                FROM ephemeral_messages_fts f
                JOIN ephemeral_messages e ON e.id = f.ephemeral_id
                WHERE ephemeral_messages_fts MATCH ?
                  AND f.session_id = ?
                ORDER BY e.timestamp ASC, e.id ASC
            """
            params: tuple = (sanitized, session_id)
        else:
            sql = """
                SELECT
                    e.id, e.session_id, e.kind, e.role,
                    e.content, e.responds_to, e.timestamp
                FROM ephemeral_messages_fts f
                JOIN ephemeral_messages e ON e.id = f.ephemeral_id
                WHERE ephemeral_messages_fts MATCH ?
                ORDER BY e.timestamp ASC, e.id ASC
            """
            params = (sanitized,)

        async with self._conn.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
        return [
            {
                "id": row["id"],
                "session_id": row["session_id"],
                "kind": row["kind"],
                "role": row["role"],
                "content": row["content"],
                "responds_to": row["responds_to"],
                "timestamp": row["timestamp"],
            }
            for row in rows
        ]


# ---------------------------------------------------------------------------
# Private row → model converters
# ---------------------------------------------------------------------------


def _row_to_session(row: aiosqlite.Row) -> Session:
    # custom_title + meta-flag columns are only present on rows from the
    # JOIN'd selects.  Detail/list rows include them; upsert-time round-trips
    # don't.  Each column is keys()-guarded so older call sites stay valid.
    keys = row.keys()
    return Session(
        id=row["id"],
        project_id=row["project_id"],
        file_path=row["file_path"],
        title=row["title"],
        custom_title=row["custom_title"] if "custom_title" in keys else None,
        archived_at=row["archived_at"] if "archived_at" in keys else None,
        starred_at=row["starred_at"] if "starred_at" in keys else None,
        viewed_at=row["viewed_at"] if "viewed_at" in keys else None,
        model=row["model"],
        started_at=_str_to_dt(row["started_at"]),
        updated_at=_str_to_dt(row["updated_at"]),
        message_count=row["message_count"],
        user_message_count=row["user_message_count"],
        cwd=row["cwd"],
        git_branch=row["git_branch"],
        is_worktree=bool(row["is_worktree"]),
        is_fork=bool(row["is_fork"]) if "is_fork" in keys else False,
        permission_mode=row["permission_mode"],
        provider=row["provider"] if "provider" in keys else "claude",
        last_message_role=row["last_message_role"],
    )


def _row_to_message(row: aiosqlite.Row) -> Message:
    usage: TokenUsage | None = None
    raw_usage = row["usage_json"]
    if raw_usage:
        try:
            usage = TokenUsage.model_validate_json(raw_usage)
        except Exception:
            pass
    return Message(
        id=row["id"],
        session_id=row["session_id"],
        parent_id=row["parent_id"],
        role=row["role"],
        content_blocks=_deserialize_content_blocks(row["content_json"]),
        timestamp=_str_to_dt(row["timestamp"]),
        model=row["model"],
        is_sidechain=bool(row["is_sidechain"]),
        is_meta=bool(row["is_meta"]),
        cwd=row["cwd"],
        git_branch=row["git_branch"],
        source_tool_assistant_uuid=row["source_tool_assistant_uuid"],
        usage=usage,
    )
