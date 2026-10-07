"""FastAPI application — HTTP API + static file serving.

Contract (for Agent 2 to implement):
  create_app(config: AppConfig, db_path: Path) -> FastAPI
    - Mount /api routes
    - Mount static files at / (serves the built React app)
    - On startup: init DB schema, trigger first scan, start file watcher task

  Routes to implement:
    GET  /api/health                         → {"ok": true}
    GET  /api/config                         → AppConfig
    PUT  /api/config  (body: AppConfig)      → AppConfig  (saves + returns updated)
    GET  /api/projects                       → list[Project]
    GET  /api/projects/{project_id}/sessions → list[Session]
    GET  /api/sessions/{session_id}          → SessionDetail
    GET  /api/search?q=&project=&limit=      → list[SearchHit]
    GET  /api/stats                          → StatsResponse
    POST /api/refresh                        → {"ok": true}  (re-scans all paths)
    GET  /api/events                         → SSE stream (text/event-stream)
      — emits {"type": "refresh"} when a JSONL file changes

SOLID notes:
  - Dependency Inversion: routes receive Database as a FastAPI Depends, not import
  - Open/Closed: add routes without touching existing ones
  - server.py does not contain business logic — it delegates to db/scanner/parser
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import Response, StreamingResponse

from pydantic import BaseModel, Field

from .analytics.cost import CostEngine
from .recap_runner import generate_recap as _generate_recap
from . import __version__
from .pty_runner import (
    DEFAULT_ROWS,
    PtyManager,
    PtyOwnershipConflict,
    PtySubmitInFlight,
    _session_conflict_pids,
    _unlink_fresh_foreign_sidecar,
)
from .pty_native import decode_terminal_input
from .driver_manager import DriverManager
from .drivers import availability_for as _driver_availability
from .drivers import supports_driving as _driver_supports
from .analytics import fast as analytics_fast
from .analytics.pricing import CachedPricingStrategy, _HARDCODED_RATES
from .update_check import fetch_latest_version, is_newer
from .analytics.service import TokenAnalyticsService as _AnalyticsSvc
from .analytics.stats import (
    ModelUsageScanner,
)
from .analytics.tips import (
    LowCacheHitRule,
    OversizedToolResultRule,
    RepeatedFileReadRule,
    TipRegistry,
)
from .config import save_config
from .db import Database
from .editor import swap_session
from .events_bus import EventBroadcaster
from .models import AppConfig, Profile
from .providers import register_builtins, registry
from .reporter import export_json, export_markdown
from .scanner import build_project_from_dir
from .watcher import watch_paths


_log = logging.getLogger(__name__)


def _sse_event_data(path: Path | str) -> str:
    """Serialize a file-change path to an SSE JSON payload.

    Extracted so tests can pin this exact contract: if the field names or types
    change here, the frontend's ``data.type === "refresh"`` check silently breaks.
    """
    return json.dumps({"type": "refresh", "path": str(path)})


def _derive_bin_name(file_path: str) -> str:
    """Walk up from a session's JSONL file_path to find the claude binary name.

    ~/.claude/projects/... → .claude → claude
    ~/.cc-mirror/zai/config/projects/... → zai

    If the inferred name isn't on PATH (e.g. demo data living under a
    `demo-data/projects/` tree where the parent isn't a real CLI), fall back
    to the plain `claude` binary.
    """
    import shutil as _shutil

    parts = Path(file_path).parts
    for i, p in enumerate(parts):
        if p == "projects":
            j = i - 1
            while j >= 0 and parts[j] == "config":
                j -= 1
            if j >= 0:
                candidate = parts[j].lstrip(".")
                if _shutil.which(candidate) is not None:
                    return candidate
            break
    return "claude"


def _derive_bin_from_data_path(data_path: str) -> str:
    """Derive a CLI bin_name from a profile's data_path directory.

    Same conventions as ``_derive_bin_name`` but accepts the data-root dir
    (e.g. ``~/.cc-mirror/zai/config`` or ``~/.claude``) instead of a JSONL
    file path. Implemented by synthesizing a fake ``<path>/projects/_.jsonl``
    so a single resolver covers both call sites.
    """
    expanded = str(Path(data_path).expanduser())
    return _derive_bin_name(str(Path(expanded) / "projects" / "_.jsonl"))


def _active_profile_bin_name(config: AppConfig) -> str:
    """Resolve the CLI bin_name to use for a brand-new session.

    Inspects ``config.active_profile_id`` and the matching profile's first
    ``data_paths`` entry (cc-mirror profiles have a single canonical data
    root per CLI install). Falls back to plain ``claude`` when no profile is
    active or no data_paths are configured.

    This mirrors the per-profile binary selection that ``_derive_bin_name``
    performs for EXISTING sessions (whose JSONL path encodes the writer
    binary). For new sessions there is no JSONL yet, so the active profile
    is the only signal available.
    """
    if config.active_profile_id and config.profiles:
        for p in config.profiles:
            if p.id == config.active_profile_id and p.data_paths:
                return _derive_bin_from_data_path(p.data_paths[0])
    # No active profile (or legacy config with only top-level data_paths).
    if config.data_paths:
        return _derive_bin_from_data_path(config.data_paths[0])
    return "claude"


def _config_dir_for_bin(config: AppConfig, bin_name: str) -> Path:
    """Resolve the ``CLAUDE_CONFIG_DIR``-equivalent for a CLI bin_name.

    The directory holds the ``.claude.json`` claude reads at startup
    (containing per-project trust flags etc.). For cc-mirror profiles
    this is the profile's first ``data_paths`` entry; for vanilla
    ``claude`` it's ``~/.claude``.

    Strategy: find the profile whose first data_path resolves to the
    same bin_name. If none matches, fall back to the active profile.
    Last resort: ``~/.claude``.
    """
    candidates: list[Path] = []
    if config.profiles:
        for p in config.profiles:
            if not p.data_paths:
                continue
            if _derive_bin_from_data_path(p.data_paths[0]) == bin_name:
                return Path(p.data_paths[0]).expanduser()
        # No bin match — fall back to active profile.
        for p in config.profiles:
            if p.id == config.active_profile_id and p.data_paths:
                candidates.append(Path(p.data_paths[0]).expanduser())
    if candidates:
        return candidates[0]
    if config.data_paths:
        return Path(config.data_paths[0]).expanduser()
    return Path("~/.claude").expanduser()


def _ensure_trust(config_dir: Path, cwd: str) -> bool:
    """Mark ``cwd`` as trusted in ``<config_dir>/.claude.json``.

    Returns True if a write happened, False if the cwd was already
    trusted (or trust state was already True). claude's TUI shows a
    "trust this folder" dialog on first launch in an untrusted cwd; if
    we don't pre-empt it, our first user message gets consumed by the
    dialog's Enter binding and never reaches the chat input.

    agent-decoder owns the chat-spawn gesture (click ``+``, choose/inherit
    a cwd, hit send) so the trust signal is implicit in the UI flow —
    this just plumbs that intent through to claude's config.

    Atomic via temp-file + rename. Tolerates a missing file (creates
    a minimal one). Re-raises on malformed JSON (caller should let it
    surface; we don't want to silently overwrite a corrupted user file).
    """
    import json as _json
    import os as _os

    path = config_dir / ".claude.json"
    if path.exists():
        with path.open("r") as f:
            data = _json.load(f)
        if not isinstance(data, dict):
            raise RuntimeError(f"{path}: expected top-level object")
    else:
        data = {}

    projects = data.setdefault("projects", {})
    if not isinstance(projects, dict):
        raise RuntimeError(f"{path}: 'projects' is not an object")
    proj = projects.setdefault(cwd, {})
    if not isinstance(proj, dict):
        raise RuntimeError(f"{path}: projects[{cwd!r}] is not an object")
    if proj.get("hasTrustDialogAccepted") is True:
        return False
    proj["hasTrustDialogAccepted"] = True

    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as f:
        _json.dump(data, f, indent=2)
    _os.replace(tmp, path)
    return True


def _extract_worktree_name(file_path: str, cwd: str | None) -> str | None:
    """Extract the worktree name (e.g. 'pr-160-review') from a worktree session."""
    # Try cwd first — the actual filesystem path is more reliable.
    if cwd:
        marker = "/.claude/worktrees/"
        idx = cwd.find(marker)
        if idx >= 0:
            return cwd[idx + len(marker) :]
    # Fall back to the mangled project directory name in the file_path.
    parts = Path(file_path).parts
    for i, p in enumerate(parts):
        if p == "projects" and i + 1 < len(parts):
            mangled = parts[i + 1]
            marker = "worktrees-"
            idx = mangled.find(marker)
            if idx >= 0:
                return mangled[idx + len(marker) :]
            break
    return None


def _reject_root_cwd(cwd: str, action: str) -> None:
    """Raise HTTP 400 if ``cwd`` resolves to the filesystem root.

    A session with cwd "/" is almost always degenerate (no working dir
    recorded); spawning a CLI there triggers claude's "trust this folder?"
    prompt for "/" and is never what the user intended.
    """
    if os.path.realpath(cwd) == os.sep:
        raise HTTPException(
            status_code=400,
            detail=f"Refusing to {action} at the filesystem root (/)",
        )


class _MessageContentUpdate(BaseModel):
    content_blocks: list[dict]


class _CreateProfileRequest(BaseModel):
    name: str
    data_paths: list[str] = Field(default_factory=list)
    color: str = "#e9733a"


class _UpdateProfileRequest(BaseModel):
    name: str | None = None
    data_paths: list[str] | None = None
    color: str | None = None


class _SetActiveProfileRequest(BaseModel):
    active_profile_id: str | None = None


class _NewSessionRequest(BaseModel):
    # All optional — defaults are "last-used cwd" + AppConfig permission mode.
    # No initial_message: brand-new sessions stay empty until the user types
    # their first message (see issue #9 fix — auto-greeting was wrong).
    cwd: str | None = None
    permission_mode: str | None = None
    # Provider for the new chat. Defaults to the active profile (Claude). When
    # the user hits "+" from inside a Codex chat the FE passes "codex" so the
    # new chat stays Codex instead of falling back to the active profile.
    provider: str | None = None


@dataclass(frozen=True)
class _PendingSession:
    """Metadata for a session that's been minted but not yet materialised.

    Lives in a small in-memory map (``app.state.pending_sessions``) keyed by
    session id. Entries are created by ``POST /api/sessions/new`` and consumed
    by the first PTY submit; nothing else touches them, so we don't need a TTL
    — server restarts wipe the map.
    """

    cwd: str
    permission_mode: str
    bin_name: str


class _SessionTitleRequest(BaseModel):
    # Pydantic enforces "must be string or null"; non-string/non-null bodies
    # (e.g. ``{"title": 123}``) produce 422 automatically. Empty / whitespace
    # strings are accepted and treated as a clear by the DB helper.
    title: str | None


class _SessionArchivedRequest(BaseModel):
    archived: bool


class _SessionStarredRequest(BaseModel):
    starred: bool


class _SessionViewedRequest(BaseModel):
    # Explicit ISO timestamp lets the FE record "viewed at message_updated_at"
    # rather than "viewed at now"; None clears.
    viewed_at: str | None


class _LocalStorageMigrationRequest(BaseModel):
    """One-time payload uploaded by the FE the first time it loads with the
    server-backed meta. Maps existing localStorage flags into session_meta."""

    archived: list[str] = []
    starred: list[str] = []
    viewed_at: dict[str, str] = {}


class _SessionDeleteRequest(BaseModel):
    session_ids: list[str]


class _FsWriteBody(BaseModel):
    # Module-scope: BaseModels declared inside create_app() are not picked up
    # by FastAPI as request-body params (they get treated as query params and
    # 422 with `loc: ["query", "body"]`).
    path: str
    content: str


class _PtyFocusRequest(BaseModel):
    session_id: str
    cwd: str | None = None
    bin_name: str | None = None
    model: str | None = None
    permission_mode: str | None = None
    new_chat: bool = False
    # Native view fits the terminal to the pane before spawning and sends the
    # fitted row count here, so the PTY spawns at its final height (no
    # spawn-then-grow that smears the revealed rows / strands the footer).
    # Omitted by non-native callers → PtyManager.focus() uses its default.
    rows: int | None = Field(default=None, gt=0, le=200)


class _PtyBlurRequest(BaseModel):
    session_id: str


class _PtySubmitRequest(BaseModel):
    session_id: str
    content: str
    model: str | None = None


class _PtyKillRequest(BaseModel):
    session_id: str


class _PtyNativeInputRequest(BaseModel):
    session_id: str
    data: str


class _PtyResizeRequest(BaseModel):
    session_id: str
    rows: int = Field(gt=0, le=200)
    cols: int = Field(gt=0, le=400)


def create_app(config: AppConfig, db_path: Path) -> FastAPI:
    """Build and return the FastAPI application instance.

    Wires together all subsystems:
      - Database initialisation and schema migration on startup.
      - Initial filesystem scan on startup.
      - Background file-watcher task that feeds the SSE event queue.
      - All /api/* routes.
      - Optional static file serving from ``src/agent_decoder/static/`` (the built
        React frontend, if present).

    Args:
        config:   The runtime ``AppConfig`` (port, data_paths, theme, …).
        db_path:  Absolute path to the SQLite database file.

    Returns:
        A fully configured ``FastAPI`` application ready to hand to uvicorn.
    """
    # Ensure all built-in provider adapters are registered before any scan
    # or lifespan task uses registry.all_adapters().
    register_builtins()

    # Mutable shared state captured by closures below.
    _state: dict = {"config": config, "watch_task": None, "fanout_task": None}
    _analytics = _AnalyticsSvc()
    _pricing_strat = CachedPricingStrategy()
    _cost_engine = CostEngine(_pricing_strat)
    # Intermediate queue: the watcher writes WatchEvent objects here; a small
    # background fan-out task scans the file and republishes the (now indexed)
    # change to every connected SSE client via the broadcaster.
    _watch_queue: asyncio.Queue = asyncio.Queue()
    _bus = EventBroadcaster()
    _pty_manager: PtyManager | None = None  # populated in lifespan
    # DriverManager owns live ProviderDriver sessions (Codex via tmux). Claude
    # never enters it — it keeps its direct-PTY path. Populated in lifespan.
    _driver_manager: DriverManager | None = None
    # Tombstone set: session ids that have been hard-deleted are recorded here
    # so that concurrent/queued scan tasks never re-upsert a deleted session.
    # Scoped to the create_app() closure — one set per app instance.
    _deleted_tombstones: set[str] = set()
    # In-memory mtime cache for sub-agent transcript files (include_subagent_chats).
    # These files never get a `sessions` row (see _ingest_subagent_file — they
    # share the PARENT session's id, so upserting a `sessions` row for them
    # would corrupt it), so db.get_session_mtime() has nowhere to store their
    # mtime. Without this, the periodic rescan would re-parse every sub-agent
    # file on every pass forever. Process-lifetime only (not persisted) — a
    # restart just reprocesses once, which is harmless since ingest is
    # idempotent (see Database.merge_messages).
    _subagent_mtimes: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _all_scan_roots() -> list[Path]:
        """User-configured paths."""
        return [Path(p).expanduser() for p in _state["config"].get_all_scan_paths()]

    def _is_subagent_path(session_path: Path) -> bool:
        """True for <session>/subagents/agent-*.jsonl transcript files."""
        return session_path.parent.name == "subagents"

    async def _ingest_subagent_file(db: Database, session_path: Path) -> None:
        """Parse a sub-agent (Task tool) transcript and merge its messages
        into the PARENT session — never as a session of its own.

        Every line in an ``agent-*.jsonl`` file carries the parent session's
        uuid as its ``sessionId`` (see ``parser._session_id_from_content``),
        so ``session.id`` here already equals the parent's id. That means we
        must NOT call ``upsert_session``/``upsert_project`` — doing so would
        overwrite the parent session's row with metadata scraped from just
        this sub-agent's messages (wrong title, cwd, message_count,
        file_path). We only ever insert the messages themselves, via
        ``merge_messages`` (not ``upsert_messages``, which does a
        session-scoped delete that would wipe the parent's OTHER messages
        from the FTS/message_blocks indexes).

        Links the sub-agent's local-root message(s) to the parent assistant
        message that spawned them, via the sibling ``agent-<id>.meta.json``'s
        ``toolUseId`` — so ``build_message_tree``'s fallback (parser.py) can
        nest them under the right node even though their own ``parent_id``
        doesn't resolve inside this file. Missing/unresolvable meta.json is
        not fatal: the messages still get ingested and simply surface as an
        orphan sidechain root.
        """
        adapter = registry.adapter_for_path(session_path) or registry.get("claude")
        session, messages = await asyncio.to_thread(adapter.parse, session_path)

        meta_path = session_path.parent / f"{session_path.stem}.meta.json"
        tool_use_id: str | None = None
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                tool_use_id = meta.get("toolUseId")
            except (OSError, json.JSONDecodeError) as exc:
                _log.debug("Could not read %s: %s", meta_path, exc)

        if tool_use_id:
            try:
                parent_message_id = await db.find_message_by_tool_use_id(
                    session.id, tool_use_id
                )
            except Exception as exc:
                _log.debug(
                    "Could not resolve toolUseId %s for %s: %s",
                    tool_use_id,
                    session_path,
                    exc,
                )
                parent_message_id = None
            if parent_message_id:
                local_ids = {m.id for m in messages}
                for m in messages:
                    if m.parent_id not in local_ids:
                        m.source_tool_assistant_uuid = parent_message_id

        await db.merge_messages(messages)

    async def do_scan(db: Database) -> int:
        """Scan all configured paths and upsert new / changed sessions into DB.

        Returns the number of sessions (re)indexed this pass, so callers — e.g.
        the periodic safety-net rescan — can skip publishing a refresh when
        nothing changed.
        """
        MAX_SCAN_SIZE = 20 * 1024 * 1024  # skip files > 20 MB; loaded on demand instead
        changed = 0
        include_subagents = _state["config"].include_subagent_chats
        for adapter in registry.all_adapters():
            roots = adapter.configured_roots(_state["config"])
            async for project, session_path in adapter.discover(
                roots, include_subagents=include_subagents
            ):
                if session_path.stem in _deleted_tombstones:
                    continue
                try:
                    if _is_subagent_path(session_path):
                        # Merged into the parent session, never its own row —
                        # see _ingest_subagent_file. Tracked via an in-memory
                        # mtime cache since there's no `sessions` row to stash
                        # it on.
                        key = str(session_path)
                        current_mtime = session_path.stat().st_mtime
                        if _subagent_mtimes.get(key) == current_mtime:
                            continue
                        await _ingest_subagent_file(db, session_path)
                        _subagent_mtimes[key] = current_mtime
                        changed += 1
                        await asyncio.sleep(0)
                        continue
                    current_mtime = session_path.stat().st_mtime
                    stored_mtime = await db.get_session_mtime(session_path.stem)
                    if stored_mtime == current_mtime:
                        continue
                    file_size = session_path.stat().st_size
                    if file_size > MAX_SCAN_SIZE:
                        # Store just the session metadata so it appears in the list;
                        # messages are loaded on demand via _scan_one when clicked.
                        if stored_mtime is None:
                            session, _ = await asyncio.to_thread(
                                adapter.parse, session_path
                            )
                            session.project_id = project.id
                            session.message_count = 0
                            project.session_count += 1
                            await db.upsert_project(project)
                            await db.upsert_session(session, file_mtime=current_mtime)
                            changed += 1
                        continue
                    session, messages = await asyncio.to_thread(
                        adapter.parse, session_path
                    )
                    session.project_id = project.id
                    project.session_count += 1
                    await db.upsert_project(project)
                    await db.upsert_session(session, file_mtime=current_mtime)
                    await db.upsert_messages(messages)
                    changed += 1
                    await asyncio.sleep(0)
                except Exception as exc:
                    print(f"Warning: skipping {session_path}: {exc}")
        return changed

    async def _scan_one(db: Database, session_path: Path) -> None:
        """Re-parse a single changed JSONL file and upsert it into the DB.

        Much faster than do_scan for live updates: one stat + one DB lookup
        instead of iterating every session file to find what changed.
        Path structure assumed: <root>/projects/<project-dir>/<session>.jsonl

        Also the watcher's per-file entry point: ``watch_paths`` (watcher.py)
        uses ``watchfiles.awatch``, which recurses through every subdirectory
        and filters purely on the ``.jsonl`` suffix — so sub-agent transcript
        files already flow through here with no separate wiring needed. Gated
        on ``include_subagent_chats`` below so the opt-in is honored on the
        live-watch path too.
        """
        if not session_path.exists() or session_path.suffix != ".jsonl":
            return
        if session_path.stem in _deleted_tombstones:
            return
        try:
            if _is_subagent_path(session_path):
                if not _state["config"].include_subagent_chats:
                    return
                key = str(session_path)
                current_mtime = session_path.stat().st_mtime
                if _subagent_mtimes.get(key) == current_mtime:
                    return
                await _ingest_subagent_file(db, session_path)
                _subagent_mtimes[key] = current_mtime
                return
            project_dir = session_path.parent
            root_path = project_dir.parent.parent  # <root>/projects/<proj>/<session>
            project = build_project_from_dir(project_dir.name, str(root_path))
            current_mtime = session_path.stat().st_mtime
            stored_mtime = await db.get_session_mtime(session_path.stem)
            if stored_mtime == current_mtime:
                return
            adapter = registry.adapter_for_path(session_path) or registry.get("claude")
            session, messages = await asyncio.to_thread(adapter.parse, session_path)
            session.project_id = project.id
            project.session_count += 1
            await db.upsert_project(project)
            await db.upsert_session(session, file_mtime=current_mtime)
            await db.upsert_messages(messages)
        except Exception as exc:
            print(f"Warning: skipping {session_path}: {exc}")

    async def _rescan_and_rewatch() -> None:
        """Rescan all paths and restart the watcher with updated paths."""
        # Cancel existing watcher
        old_task = _state.get("watch_task")
        if old_task and not old_task.done():
            old_task.cancel()
        # Rescan
        async with Database(db_path) as db:
            await do_scan(db)
        # Restart watcher with current paths.
        _state["watch_task"] = asyncio.create_task(
            watch_paths(_all_scan_roots(), _watch_queue)
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with Database(db_path) as db:
            await db.init_schema()
            await db.reset_xml_title_mtimes()
            await db.reset_truncated_titles()
            await db.migrate_project_id_v2()
            await db.migrate_materialize_v1()
            await db.migrate_fts_map_v1()
            # No long-lived readers are open yet at startup, so this is the
            # cheapest moment to TRUNCATE any WAL bloat left by a prior run.
            await db.checkpoint()

        async def _background_scan() -> None:
            async with Database(db_path) as db:
                await do_scan(db)

        asyncio.create_task(_background_scan())
        _state["watch_task"] = asyncio.create_task(
            watch_paths(_all_scan_roots(), _watch_queue)
        )

        async def _fanout_watcher_events() -> None:
            """Drain the watcher queue and republish indexed events to all clients.

            One task per app (not per client) so the DB re-index runs once per
            file change regardless of how many viewers are connected.

            Burst coalescing: a streaming turn (claude is spawned with
            ``--include-partial-messages``) rewrites its JSONL many times per
            second. Re-parsing the whole file on every watch event pinned the
            event loop near 80% CPU. Instead we block for the first event, wait
            a short window for the burst to accumulate, dedupe by path, and
            re-index each distinct path once — collapsing ~20 reparses/sec into
            a handful while keeping the reader's view live. Emitting a single
            refresh per path (rather than per event) also throttles the FE's
            on-demand refetch, which itself reparses via GET /api/sessions/{id}.

            A fresh DB connection is opened per burst (not one held for the
            life of the server) so we don't pin a read snapshot between bursts,
            which would block wal_checkpoint(TRUNCATE) and let index.db-wal
            grow without bound.
            """
            COALESCE_WINDOW_S = 0.2
            while True:
                first = await _watch_queue.get()
                paths = {first.path}
                # Let the rest of the burst land, then coalesce by path.
                await asyncio.sleep(COALESCE_WINDOW_S)
                while True:
                    try:
                        nxt = _watch_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    paths.add(nxt.path)
                async with Database(db_path) as db:
                    for path in paths:
                        try:
                            await _scan_one(db, path)
                        except Exception as exc:
                            print(f"Warning: scan_one failed for {path}: {exc}")
                        _bus.publish({"type": "refresh", "path": str(path)})

        _state["fanout_task"] = asyncio.create_task(_fanout_watcher_events())

        # Safety net behind the live watcher. The watcher only catches changes
        # while the server is running AND only if the OS delivers the event;
        # anything that changed while agent-decoder was down, or any watch event
        # the kernel coalesced/dropped, would otherwise stay stale until the
        # next restart (the "session shows no/old history even though it
        # resumes fine in the terminal" symptom). This periodic pass re-stats
        # every session file across ALL profile data paths and re-parses only
        # those whose mtime moved, so the index self-heals without a restart.
        # do_scan returns the changed count, so connected clients are nudged to
        # refetch only when something actually changed.
        PERIODIC_RESCAN_S = 60.0

        async def _periodic_rescan() -> None:
            while True:
                await asyncio.sleep(PERIODIC_RESCAN_S)
                changed = 0
                try:
                    # Fresh connection per pass so we don't hold a read
                    # snapshot across the sleep, which would block
                    # wal_checkpoint(TRUNCATE) and let index.db-wal grow.
                    async with Database(db_path) as db:
                        changed = await do_scan(db)
                except Exception as exc:
                    print(f"Warning: periodic rescan failed: {exc}")
                    continue
                if changed:
                    _bus.publish({"type": "refresh", "path": "periodic-rescan"})

        _state["periodic_rescan_task"] = asyncio.create_task(_periodic_rescan())

        # Fold the WAL back into index.db and truncate index.db-wal on a timer.
        # SQLite's default wal_autocheckpoint is passive and only fires on
        # commit, so it is easily pinned by a held read snapshot — and the
        # PtyManager/DriverManager connection below lives for the whole server
        # lifetime. This dedicated short-lived connection owns no snapshot, so
        # TRUNCATE can shrink the WAL that autocheckpoint can't. Without it
        # index.db-wal grew to ~22 GB.
        PERIODIC_CHECKPOINT_S = 600.0

        async def _periodic_checkpoint() -> None:
            while True:
                await asyncio.sleep(PERIODIC_CHECKPOINT_S)
                try:
                    async with Database(db_path) as db:
                        await db.checkpoint()
                except Exception as exc:
                    print(f"Warning: periodic checkpoint failed: {exc}")

        _state["periodic_checkpoint_task"] = asyncio.create_task(_periodic_checkpoint())

        async def _refresh_pricing() -> None:
            await _pricing_strat.refresh()

        asyncio.create_task(_refresh_pricing())
        nonlocal _pty_manager, _driver_manager
        # ui_endpoint is written into Phase-1 lock sidecars so a peer
        # agent-decoder reading the lock can render "open in UI at ..."
        # in its take-over banner. Best-effort — config.host of
        # "0.0.0.0" gets normalised to "127.0.0.1" for the URL.
        _ui_host = "127.0.0.1" if config.host in ("0.0.0.0", "::") else config.host
        # PtyManager holds a long-lived Database reference for Phase 2 /btw
        # capture (`record_ephemeral_input` / `record_ephemeral_response`).
        # The async-context-manager MUST be entered here so ``_conn`` is
        # opened — passing a bare ``Database(db_path)`` would leave the
        # connection None and silently fail every ephemeral write. Routes
        # continue to use their own per-request ``async with Database(...)``
        # contexts; SQLite WAL mode keeps the long-lived + per-request
        # connections from contending.
        async with Database(db_path) as _pty_db:
            _pty_manager = PtyManager(
                _pty_db,
                _bus,
                ui_endpoint=f"http://{_ui_host}:{config.port}",
            )
            app.state.pty_manager = _pty_manager
            _pty_manager.set_native_cols(_state["config"].native_pty_cols)
            # DriverManager shares the bus + a long-lived DB handle. No idle
            # reaper: tmux-backed sessions survive disconnect/idle by design,
            # so the "5-min reaper kills long tasks" bug cannot recur here.
            _driver_manager = DriverManager(
                _pty_db,
                _bus,
                ui_endpoint=f"http://{_ui_host}:{config.port}",
            )
            app.state.driver_manager = _driver_manager
            _driver_manager.set_native_cols(_state["config"].native_pty_cols)
            try:
                yield
            finally:
                if _driver_manager is not None:
                    await _driver_manager.shutdown()
                if _pty_manager is not None:
                    await _pty_manager.shutdown()

    app = FastAPI(title="Agent Decoder", version="0.1.0", lifespan=lifespan)
    # Session-detail responses are megabytes of JSON for old chats; gzip cuts
    # transfer time by ~10x. minimum_size avoids overhead for tiny payloads.
    # Localhost-only app, so there's no network to save bytes on: default
    # compresslevel=9 was pure CPU cost blocking the event loop on large JSON
    # bodies (measured ~3.4s->~1s); level 1 keeps most of the size savings cheaply.
    app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=1)

    # Pending-session map (issue #9 fix): /api/sessions/new mints metadata into
    # this dict; the first PTY submit for that id materializes the session with
    # --session-id. Guarded by an asyncio lock so two concurrent submits for the
    # same fresh id can't both claim it.
    app.state.pending_sessions = {}
    app.state.pending_sessions_lock = asyncio.Lock()

    # Session ids currently routed to the DriverManager (driver-backed, e.g.
    # Codex). Populated on a successful driver focus; cleared on kill. Hot-path
    # native endpoints (input/resize/snapshot/status/kill) consult this O(1)
    # set to pick the manager without a per-call DB lookup.
    app.state.driver_sessions = set()

    # LRU cache of pre-serialized SessionDetail responses, keyed by session id
    # with file mtime as the validation token. A hit skips the SQL fetch,
    # JSON parse, Pydantic round-trip, and re-serialization entirely.
    _detail_cache: "OrderedDict[str, tuple[float, bytes]]" = OrderedDict()
    _DETAIL_CACHE_MAX = 4

    def _invalidate_detail_cache(session_id: str) -> None:
        _detail_cache.pop(session_id, None)

    # Whole-corpus analytics cache. Six endpoints (daily/stats/models/tools/
    # files/tips) each used to run `SELECT * FROM messages` and scan all
    # ~145k rows in Python — independently, uncached, on every dashboard view
    # (~6-12s each). We now load the corpus once and compute the whole bundle
    # behind a lock, keyed on a cheap sessions-table signature. Cache hits are
    # the common case (dashboard re-opened, data unchanged → instant); the
    # bundle recomputes only when a session is added/removed/re-indexed.
    _analytics_cache: dict = {}
    _analytics_cache_sig: list = [None]  # boxed so the closure can rebind it
    _analytics_lock = asyncio.Lock()

    async def _analytics_bundle(db: Database) -> dict:
        sig = await db.analytics_signature()
        if _analytics_cache_sig[0] == sig and _analytics_cache:
            return _analytics_cache
        async with _analytics_lock:
            # Re-check: a concurrent request may have filled the cache while
            # we waited for the lock (browsers fetch all six in parallel).
            if _analytics_cache_sig[0] == sig and _analytics_cache:
                return _analytics_cache
            # SQL-backed aggregation: two corpus reads (usage + content) in
            # the engine instead of loading 145k pydantic Message objects.
            # Verified byte-for-byte against the scanners in
            # tests/analytics/test_fast_parity.py.
            bundle = await analytics_fast.compute_bundle(db._conn)
            _analytics_cache.clear()
            _analytics_cache.update(bundle)
            _analytics_cache_sig[0] = sig
            return _analytics_cache

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------

    @app.get("/api/health")
    async def health():
        return {"ok": True}

    @app.get("/api/config")
    async def get_config():
        return _state["config"]

    @app.put("/api/config")
    async def update_config(new_config: AppConfig):
        _state["config"] = new_config
        save_config(new_config)
        # Refresh the authoritative native PTY width so new spawns pick it up.
        if _pty_manager is not None:
            _pty_manager.set_native_cols(new_config.native_pty_cols)
        return new_config

    # ------------------------------------------------------------------
    # Profiles
    # ------------------------------------------------------------------

    @app.get("/api/profiles")
    async def get_profiles():
        return {
            "profiles": [p.model_dump() for p in _state["config"].profiles],
            "active_profile_id": _state["config"].active_profile_id,
        }

    @app.post("/api/profiles")
    async def create_profile(req: _CreateProfileRequest):
        cfg = _state["config"]
        if not cfg.profiles:
            cfg.profiles.insert(
                0,
                Profile(
                    name="Default",
                    data_paths=cfg.data_paths,
                    color="#6b7280",
                ),
            )
        profile = Profile(
            name=req.name, data_paths=req.data_paths or ["~/.claude"], color=req.color
        )
        cfg.profiles.append(profile)
        save_config(cfg)
        asyncio.create_task(_rescan_and_rewatch())
        return profile.model_dump()

    @app.put("/api/profiles/active")
    async def set_active_profile(req: _SetActiveProfileRequest):
        _state["config"].active_profile_id = req.active_profile_id
        save_config(_state["config"])
        return {"active_profile_id": req.active_profile_id}

    @app.put("/api/profiles/{profile_id}")
    async def update_profile(profile_id: str, req: _UpdateProfileRequest):
        cfg = _state["config"]
        for p in cfg.profiles:
            if p.id == profile_id:
                if req.name is not None:
                    p.name = req.name
                if req.data_paths is not None:
                    p.data_paths = req.data_paths
                if req.color is not None:
                    p.color = req.color
                save_config(cfg)
                asyncio.create_task(_rescan_and_rewatch())
                return p.model_dump()
        raise HTTPException(404, "Profile not found")

    @app.delete("/api/profiles/{profile_id}")
    async def delete_profile(profile_id: str):
        cfg = _state["config"]
        cfg.profiles = [p for p in cfg.profiles if p.id != profile_id]
        if cfg.active_profile_id == profile_id:
            cfg.active_profile_id = None
        save_config(cfg)
        return {"ok": True}

    @app.get("/api/projects")
    async def get_projects():
        data_sources = _state["config"].get_active_data_sources()
        async with Database(db_path) as db:
            return await db.get_projects(data_sources=data_sources)

    @app.get("/api/projects/{project_id}/sessions")
    async def get_project_sessions(project_id: str):
        data_sources = _state["config"].get_active_data_sources()
        async with Database(db_path) as db:
            return await db.get_sessions(
                project_id=project_id, data_sources=data_sources
            )

    @app.get("/api/sessions")
    async def get_all_sessions():
        data_sources = _state["config"].get_active_data_sources()
        async with Database(db_path) as db:
            return await db.get_sessions(data_sources=data_sources)

    def _pending_session_placeholder(session_id: str, pending: _PendingSession):
        """Synthesize a minimal SessionDetail for a not-yet-materialised session.

        Returned when the caller asks for a session that's been minted via
        ``/api/sessions/new`` but whose JSONL hasn't appeared on disk yet
        (lazy-spawn PTY path: the user has focused the chat input and may
        even have hit Submit, but ``claude`` is still booting). Without this
        the UI 404s for the brief window between submit and the first JSONL
        flush. The placeholder keeps the route deterministic during that
        bootstrap window instead of relying on post-submit polling.
        """
        # Synthesize a plausible file_path so ``_derive_bin_name`` and the
        # frontend's worktree-extraction logic stay consistent once the
        # real JSONL replaces the placeholder via the SSE refresh event.
        file_path = str(Path(pending.cwd) / ".pending" / f"{session_id}.jsonl")
        return {
            "id": session_id,
            "project_id": "",
            "file_path": file_path,
            "title": None,
            "custom_title": None,
            "model": None,
            "started_at": None,
            "updated_at": None,
            "message_count": 0,
            "user_message_count": 0,
            "cwd": pending.cwd,
            "git_branch": None,
            "is_worktree": False,
            "is_fork": False,
            "permission_mode": pending.permission_mode,
            "last_message_role": None,
            # Provider drives the FE skin + caps for the empty chat. A pending
            # Codex chat must report "codex" so the composer/toggle/skin match
            # before any rollout exists; everything else is Claude.
            "provider": "codex" if pending.bin_name == "codex" else "claude",
            "messages": [],
        }

    @app.get("/api/sessions/{session_id}")
    async def get_session(
        session_id: str, limit: int | None = Query(None, ge=1, le=5000)
    ):
        async with Database(db_path) as db:
            # On-demand re-parse: if the JSONL file changed since last index
            # (e.g. server restarted before background scan reached this file),
            # re-parse now so the caller always sees fresh content.
            file_path_str = await db.get_session_file_path(session_id)
            current_mtime: float | None = None
            if file_path_str:
                session_path = Path(file_path_str)
                await _scan_one(db, session_path)
                try:
                    current_mtime = session_path.stat().st_mtime
                except OSError:
                    current_mtime = None

            # Cache hit: skip the SQL+parse+serialize path entirely.
            if limit is None and current_mtime is not None:
                hit = _detail_cache.get(session_id)
                if hit is not None and hit[0] == current_mtime:
                    _detail_cache.move_to_end(session_id)
                    return Response(content=hit[1], media_type="application/json")

            # Hot path (no limit): bypass Pydantic — embed stored content_json /
            # usage_json strings directly into the response.
            if limit is None:
                body = await db.get_session_detail_json_bytes(session_id)
                if body is None:
                    # Session not on disk — check the pending-session map
                    # before 404'ing. A session minted by /api/sessions/new
                    # and submitted via /api/pty/submit has no JSONL until
                    # claude finishes booting + flushes its first turn;
                    # return a placeholder so the UI can render an empty
                    # chat instead of treating it as a deleted session.
                    async with app.state.pending_sessions_lock:
                        pending = app.state.pending_sessions.get(session_id)
                    if pending is not None:
                        return _pending_session_placeholder(session_id, pending)
                    raise HTTPException(status_code=404, detail="Session not found")
                # Session is materialised on disk — drop any stale pending
                # entry so the placeholder branch above never wins after
                # the real JSONL arrives. The PTY flow leaves the entry in
                # place until materialization confirms the session is on disk.
                async with app.state.pending_sessions_lock:
                    app.state.pending_sessions.pop(session_id, None)
                if current_mtime is not None:
                    _detail_cache[session_id] = (current_mtime, body)
                    _detail_cache.move_to_end(session_id)
                    while len(_detail_cache) > _DETAIL_CACHE_MAX:
                        _detail_cache.popitem(last=False)
                return Response(content=body, media_type="application/json")

            # Slow path (limited): Pydantic round-trip is fine for small payloads.
            detail = await db.get_session_detail(session_id, message_limit=limit)

        if detail is None:
            async with app.state.pending_sessions_lock:
                pending = app.state.pending_sessions.get(session_id)
            if pending is not None:
                return _pending_session_placeholder(session_id, pending)
            raise HTTPException(status_code=404, detail="Session not found")
        async with app.state.pending_sessions_lock:
            app.state.pending_sessions.pop(session_id, None)
        return detail

    @app.get("/api/search")
    async def search(
        q: str = Query(..., min_length=1),
        project: str | None = Query(None),
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ):
        async with Database(db_path) as db:
            return await db.search(q, project_id=project, limit=limit, offset=offset)

    @app.get("/api/stats")
    async def get_stats():
        async with Database(db_path) as db:
            stats = await db.get_stats()
        stats.data_paths = _state["config"].data_paths
        return stats

    @app.get("/api/dashboard")
    async def get_dashboard():
        data_sources = _state["config"].get_active_data_sources()
        async with Database(db_path) as db:
            projects = await db.get_projects(data_sources)
            sessions = await db.get_sessions(data_sources=data_sources)
            stats = await db.get_stats()

            # Only load messages for the 10 most recent sessions
            recent = sessions[:10]
            recent_data: list[dict] = []
            all_recent_prompts: list = []
            all_recent_msgs: list = []
            for s in recent:
                detail = await db.get_session_detail(s.id)
                session_msgs = detail.messages if detail else []
                models_used = list(
                    dict.fromkeys(
                        m.model
                        for m in session_msgs
                        if m.model and m.role == "assistant"
                    )
                )
                prompts = _analytics.prompt_breakdown(session_msgs)
                multi = _cost_engine.compute_multi(prompts)
                all_recent_prompts.extend(prompts)
                all_recent_msgs.extend(session_msgs)
                recent_data.append(
                    {
                        "id": s.id,
                        "title": s.title,
                        "project_id": s.project_id,
                        "models": models_used,
                        "message_count": s.message_count,
                        "total_usd": float(multi.total_usd),
                        "updated_at": s.updated_at.isoformat()
                        if s.updated_at
                        else None,
                        "last_message_role": s.last_message_role,
                    }
                )

        # Global stats from recent sessions only (fast)
        model_usage = ModelUsageScanner().scan(all_recent_msgs)
        tip_registry = TipRegistry()
        tip_registry.register(RepeatedFileReadRule())
        tip_registry.register(OversizedToolResultRule())
        tip_registry.register(LowCacheHitRule())
        tips = tip_registry.run(all_recent_msgs)[:3]

        recent_cost = _cost_engine.compute_multi(all_recent_prompts)

        project_data = [
            {
                "id": p.id,
                "display_name": p.display_name,
                "session_count": p.session_count,
                "last_activity_at": p.last_activity_at.isoformat()
                if p.last_activity_at
                else None,
            }
            for p in projects
        ]

        return {
            "recent_sessions": recent_data,
            "projects": project_data,
            "model_usage": model_usage,
            "total_cost_usd": float(recent_cost.total_usd),
            "total_sessions": stats.total_sessions,
            "total_messages": stats.total_messages,
            "tips": [
                {
                    "rule_id": t.rule_id,
                    "severity": t.severity,
                    "title": t.title,
                    "detail": t.detail,
                }
                for t in tips
            ],
        }

    @app.get("/api/analytics/sessions/{session_id}/tokens")
    async def get_session_tokens(session_id: str):
        async with Database(db_path) as db:
            detail = await db.get_session_detail(session_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Session not found")
        bd = _analytics.session_totals(detail.messages)
        return {
            "session_id": session_id,
            "input_tokens": bd.input_tokens,
            "output_tokens": bd.output_tokens,
            "cache_creation_tokens": bd.cache_creation_tokens,
            "cache_read_tokens": bd.cache_read_tokens,
            "total": bd.total,
        }

    @app.get("/api/analytics/sessions/{session_id}/prompts")
    async def get_session_prompts(session_id: str):
        async with Database(db_path) as db:
            detail = await db.get_session_detail(session_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Session not found")
        prompts = _analytics.prompt_breakdown(detail.messages)
        return [
            {
                "user_message_id": p.user_message_id,
                "assistant_message_id": p.assistant_message_id,
                "input_tokens": p.breakdown.input_tokens,
                "output_tokens": p.breakdown.output_tokens,
                "cache_creation_tokens": p.breakdown.cache_creation_tokens,
                "cache_read_tokens": p.breakdown.cache_read_tokens,
                "total": p.breakdown.total,
            }
            for p in prompts
        ]

    @app.get("/api/analytics/daily")
    async def get_daily_analytics():
        async with Database(db_path) as db:
            return (await _analytics_bundle(db))["daily"]

    @app.get("/api/analytics/sessions/{session_id}/cost")
    async def get_session_cost(session_id: str):
        async with Database(db_path) as db:
            detail = await db.get_session_detail(session_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Session not found")
        prompts = _analytics.prompt_breakdown(detail.messages)
        multi = _cost_engine.compute_multi(prompts)
        pricing_source = "live" if _pricing_strat._cached_data else "hardcoded"
        return {
            "session_id": session_id,
            "models": [
                {
                    "model": c.model,
                    "input_usd": float(c.input_usd),
                    "output_usd": float(c.output_usd),
                    "cache_write_usd": float(c.cache_write_usd),
                    "cache_read_usd": float(c.cache_read_usd),
                    "total_usd": float(c.total_usd),
                    "pricing_known": c.pricing is not None,
                }
                for c in multi.models
            ],
            "total_usd": float(multi.total_usd),
            "pricing_known": any(c.pricing is not None for c in multi.models),
            "pricing_source": pricing_source,
        }

    @app.get("/api/analytics/stats")
    async def get_prompt_stats():
        async with Database(db_path) as db:
            return (await _analytics_bundle(db))["stats"]

    @app.get("/api/analytics/models")
    async def get_model_usage():
        async with Database(db_path) as db:
            return (await _analytics_bundle(db))["models"]

    @app.get("/api/analytics/tools")
    async def get_tool_usage():
        async with Database(db_path) as db:
            return (await _analytics_bundle(db))["tools"]

    @app.get("/api/analytics/files")
    async def get_file_touches():
        async with Database(db_path) as db:
            return (await _analytics_bundle(db))["files"]

    @app.get("/api/analytics/tips")
    async def get_tips():
        async with Database(db_path) as db:
            return (await _analytics_bundle(db))["tips"]

    @app.get("/api/pricing")
    async def get_pricing_table():
        source = "live" if _pricing_strat._cached_data else "hardcoded"
        data = _pricing_strat._cached_data or _HARDCODED_RATES
        return {
            "source": source,
            "models": [
                {
                    "model": model,
                    "input_per_mtok": float(p.input_per_mtok),
                    "output_per_mtok": float(p.output_per_mtok),
                    "cache_write_per_mtok": float(p.cache_write_per_mtok),
                    "cache_read_per_mtok": float(p.cache_read_per_mtok),
                }
                for model, p in sorted(data.items())
            ],
        }

    # ------------------------------------------------------------------
    # Export routes (Phase 7)
    # ------------------------------------------------------------------

    @app.get("/api/sessions/{session_id}/export")
    async def export_session(session_id: str, format: str = Query("json")):
        if format not in ("json", "md"):
            raise HTTPException(status_code=400, detail="format must be 'json' or 'md'")
        async with Database(db_path) as db:
            detail = await db.get_session_detail(session_id)
            ephemerals = await db.get_ephemeral_messages(session_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Session not found")

        bd = _analytics.session_totals(detail.messages)
        model = detail.model or "unknown"
        cost = _cost_engine.compute(model, bd)
        prompts = _analytics.prompt_breakdown(detail.messages)
        prompts_dicts = [
            {
                "user_message_id": p.user_message_id,
                "assistant_message_id": p.assistant_message_id,
                "breakdown": {
                    "input_tokens": p.breakdown.input_tokens,
                    "output_tokens": p.breakdown.output_tokens,
                    "cache_creation_tokens": p.breakdown.cache_creation_tokens,
                    "cache_read_tokens": p.breakdown.cache_read_tokens,
                    "total": p.breakdown.total,
                },
            }
            for p in prompts
        ]

        if format == "json":
            data = export_json(
                detail, cost=cost, prompts=prompts_dicts, ephemerals=ephemerals
            )
            import json as _json

            slug = (detail.title or detail.id).replace(" ", "_").lower()
            return Response(
                content=_json.dumps(data, indent=2),
                media_type="application/json",
                headers={"Content-Disposition": f'attachment; filename="{slug}.json"'},
            )
        else:
            pricing = _pricing_strat.get_pricing(model)
            md = export_markdown(
                detail,
                cost=cost,
                prompts=prompts_dicts,
                pricing=pricing,
                ephemerals=ephemerals,
            )
            slug = (detail.title or detail.id).replace(" ", "_").lower()
            return Response(
                content=md,
                media_type="text/markdown",
                headers={"Content-Disposition": f'attachment; filename="{slug}.md"'},
            )

    # -----------------------------------------------------------------------
    # Mutation guard (Phase 6)
    # -----------------------------------------------------------------------

    def _require_edit() -> None:
        if not _state["config"].edit_enabled:
            raise HTTPException(
                status_code=403,
                detail="Editing is disabled. Set edit_enabled: true in settings to enable.",
            )

    # -----------------------------------------------------------------------
    # Provider capability gating (Phase 4b) — effective caps = static
    # ProviderCaps AND runtime drivability. Driving-gated caps (send/resume)
    # are ANDed with the backend availability ONLY for driver-backed providers
    # (Codex). Claude is not driver-backed — it sends over its own direct-PTY
    # path, so its caps stand on their own and stay fully available on POSIX.
    # -----------------------------------------------------------------------

    def _provider_availability(provider: str) -> dict:
        """Runtime drivability for *provider* as a serialisable dict."""
        if _driver_supports(provider):
            a = _driver_availability(provider)
            return {"available": a.available, "reason": a.reason}
        # Native-path provider (claude): driven over its own POSIX PTY.
        return {"available": True, "reason": None}

    def _effective_caps(provider: str) -> dict:
        """Static caps reconciled with runtime drivability."""
        try:
            caps = registry.get(provider).capabilities
        except KeyError:
            return {
                "can_send": False,
                "can_resume": False,
                "can_fork": False,
                "can_edit": False,
            }
        if _driver_supports(provider):
            drivable = _driver_availability(provider).available
            send = caps.can_send and drivable
            resume = caps.can_resume and drivable
        else:
            send, resume = caps.can_send, caps.can_resume
        # fork/edit are file-level operations, not gated on the live transport.
        return {
            "can_send": send,
            "can_resume": resume,
            "can_fork": caps.can_fork,
            "can_edit": caps.can_edit,
        }

    def _require_capability(provider: str, attr: str) -> None:
        """Raise 409 if *provider* does not effectively support *attr*."""
        if not _effective_caps(provider).get(attr, False):
            raise HTTPException(
                status_code=409,
                detail={
                    "kind": "capability_unsupported",
                    "provider": provider,
                    "capability": attr,
                    "availability": _provider_availability(provider),
                },
            )

    def _find_new_codex_rollout(cwd: str, after_ts: float) -> str | None:
        """Newest codex rollout written after ``after_ts`` whose session_meta
        cwd matches ``cwd`` — i.e. the rollout a freshly-spawned codex just
        created on its first message. Returns its session id, or None.

        Cheap: stat-filters on mtime first and only reads the first line
        (session_meta) of files newer than ``after_ts``, of which there are
        ~none until the new rollout appears.
        """
        cwd_resolved = os.path.realpath(os.path.expanduser(cwd))
        best_id: str | None = None
        best_mtime = after_ts
        for root in _state["config"].codex_data_paths:
            root_p = Path(root).expanduser()
            if not root_p.is_dir():
                continue
            for path in root_p.rglob("rollout-*.jsonl"):
                try:
                    mt = path.stat().st_mtime
                except OSError:
                    continue
                if mt <= best_mtime:
                    continue
                try:
                    with path.open("r", encoding="utf-8", errors="replace") as fh:
                        rec = json.loads(fh.readline())
                except (OSError, ValueError):
                    continue
                if rec.get("type") != "session_meta":
                    continue
                payload = rec.get("payload", {})
                rid = payload.get("id")
                rec_cwd = payload.get("cwd")
                if not rid or not rec_cwd:
                    continue
                if os.path.realpath(os.path.expanduser(rec_cwd)) != cwd_resolved:
                    continue
                best_id, best_mtime = rid, mt
        return best_id

    async def _adopt_codex_session(
        placeholder_id: str, cwd: str, after_ts: float
    ) -> None:
        """Adopt the real Codex rollout UUID for a brand-new Codex chat.

        A fresh ``codex`` invents its own rollout id on the first message (codex
        has no ``--session-id``), so the chat the user is driving under the
        placeholder id must be re-keyed to that real id once it appears — else
        the live session and the on-disk rollout are two different identities.
        Polls for the new rollout, re-keys the live driver in place (tmux rename,
        no respawn), drops the placeholder, and signals the FE to navigate.
        Gives up after 10 min (user never sent a first message) or if the
        session dies.
        """
        deadline = time.time() + 600.0
        while time.time() < deadline:
            await asyncio.sleep(1.5)
            if placeholder_id not in app.state.driver_sessions:
                return  # session closed / died before a rollout appeared
            try:
                real_id = await asyncio.to_thread(
                    _find_new_codex_rollout, cwd, after_ts
                )
            except Exception as exc:  # pragma: no cover — defensive
                _log.warning("codex adopt: scan raised: %s", exc)
                continue
            if not real_id or real_id == placeholder_id:
                continue
            if _driver_manager is None:
                return
            try:
                rekeyed = await _driver_manager.rekey(placeholder_id, real_id)
            except Exception as exc:  # pragma: no cover — defensive
                _log.warning("codex adopt: rekey raised: %s", exc)
                return
            if not rekeyed:
                return
            app.state.driver_sessions.discard(placeholder_id)
            app.state.driver_sessions.add(real_id)
            async with app.state.pending_sessions_lock:
                app.state.pending_sessions.pop(placeholder_id, None)
            _log.info("codex adopt: %s -> %s", placeholder_id, real_id)
            # Navigate the FE placeholder → real id, and refresh the list so the
            # newly-materialised rollout shows up.
            _bus.publish(
                {"type": "session_adopted", "old": placeholder_id, "new": real_id}
            )
            _bus.publish({"type": "refresh", "path": "codex-adopt"})
            return

    async def _resolve_provider(session_id: str) -> str:
        """Provider for *session_id* — on-disk detail, else a pending mint.

        A brand-new Codex chat has no rollout yet, so its provider lives only
        in the pending-session map (bin_name="codex"). Consult it so focus/submit
        route to the DriverManager and spawn fresh codex instead of treating the
        empty chat as Claude.
        """
        async with Database(db_path) as db:
            detail = await db.get_session_detail(session_id)
        if detail is not None:
            return detail.provider
        async with app.state.pending_sessions_lock:
            pending = app.state.pending_sessions.get(session_id)
        if pending is not None and _driver_supports(pending.bin_name):
            return pending.bin_name
        return "claude"

    @app.get("/api/providers")
    async def get_providers():
        """Per-provider static caps + runtime availability + effective caps.

        The FE drives every affordance off ``effective`` so a read-only or
        non-drivable provider never shows a send/Native/fork button that would
        misfire (the read-only-honesty gap).
        """
        out = []
        for adapter in registry.all_adapters():
            name = adapter.name
            out.append(
                {
                    "name": name,
                    "caps": adapter.capabilities.model_dump(),
                    "availability": _provider_availability(name),
                    "effective": _effective_caps(name),
                    "driver_backed": _driver_supports(name),
                }
            )
        return out

    # -----------------------------------------------------------------------
    # Message mutation routes (Phase 6)
    # -----------------------------------------------------------------------

    @app.delete("/api/messages/{message_id}")
    async def delete_message_route(message_id: str):
        _require_edit()
        async with Database(db_path) as db:
            info = await db.get_session_info_for_message(message_id)
            if info is None:
                raise HTTPException(status_code=404, detail="Message not found")
            session_id, file_path = info
            # The editor rewrites the on-disk file in the Claude JSONL format;
            # applying it to a Codex rollout would corrupt it. Gate on the
            # provider's effective can_edit (defense in depth behind the FE).
            _require_capability(await _resolve_provider(session_id), "can_edit")
            path = Path(file_path)
            if not path.exists():
                raise HTTPException(
                    status_code=404, detail="Session file not found on disk"
                )
            edited_path, _, backup_path, _ = swap_session(
                path, session_id, delete_uuid=message_id
            )
            await db.delete_session_messages(session_id)
            await _scan_one(db, edited_path)
            await _scan_one(db, backup_path)
            # Parsed updated_at = max(remaining message timestamps); deleting
            # the latest message regresses it. Force-bump so SSE/dedupe see
            # the mutation. See db.touch_session_updated_at docstring.
            await db.touch_session_updated_at(session_id)
        return {"ok": True, "session_id": session_id}

    @app.patch("/api/messages/{message_id}")
    async def patch_message_route(message_id: str, body: _MessageContentUpdate):
        _require_edit()
        async with Database(db_path) as db:
            info = await db.get_session_info_for_message(message_id)
            if info is None:
                raise HTTPException(status_code=404, detail="Message not found")
            session_id, file_path = info
            # See delete_message_route: provider-format-specific editor, so gate
            # on effective can_edit to never corrupt a non-Claude session file.
            _require_capability(await _resolve_provider(session_id), "can_edit")
            path = Path(file_path)
            if not path.exists():
                raise HTTPException(
                    status_code=404, detail="Session file not found on disk"
                )
            edited_path, _, backup_path, _ = swap_session(
                path, session_id, edit_uuid=message_id, new_content=body.content_blocks
            )
            await db.delete_session_messages(session_id)
            await _scan_one(db, edited_path)
            await _scan_one(db, backup_path)
            # Content edits leave message timestamps untouched, so the
            # parsed updated_at doesn't move. Force-bump so SSE/dedupe see
            # the mutation. See db.touch_session_updated_at docstring.
            await db.touch_session_updated_at(session_id)
        return {"ok": True, "session_id": session_id}

    @app.post("/api/sessions/{session_id}/reveal")
    async def reveal_session(session_id: str):
        """Reveal the session JSONL file in the OS file manager."""
        async with Database(db_path) as db:
            detail = await db.get_session_detail(session_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Session not found")
        file_path = Path(detail.file_path)
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="File not found on disk")
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(file_path)])
        elif sys.platform.startswith("linux"):
            subprocess.Popen(["xdg-open", str(file_path.parent)])
        else:
            subprocess.Popen(["explorer", "/select,", str(file_path)])
        return {"ok": True}

    @app.post("/api/sessions/{session_id}/open-terminal")
    async def open_terminal(session_id: str):
        """Open a new Terminal.app window at the session's cwd and run <binary> -r."""
        async with Database(db_path) as db:
            detail = await db.get_session_detail(session_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Session not found")
        cwd = detail.cwd
        if not cwd:
            cwd = str(Path(detail.file_path).parent)
        if not Path(cwd).is_dir():
            raise HTTPException(status_code=404, detail=f"Directory not found: {cwd}")
        _reject_root_cwd(cwd, "open a terminal")
        # Build a provider-correct resume command. Codex resumes with
        # ``codex resume <uuid>`` (the uuid IS our session id); Claude uses
        # ``claude -r <id>`` (with ``-w <worktree>`` for worktree sessions).
        # Without this branch a Codex "open in terminal" would run
        # ``claude -r <codex-uuid>`` — the wrong binary against a foreign id.
        if detail.provider == "codex":
            cmd = f"codex resume {shlex.quote(session_id)}"
        else:
            bin_name = _derive_bin_name(detail.file_path)
            wt = (
                _extract_worktree_name(detail.file_path, detail.cwd)
                if detail.is_worktree
                else None
            )
            quoted_bin = shlex.quote(bin_name)
            cmd = (
                f"{quoted_bin} -w {shlex.quote(wt)} -r {session_id}"
                if wt
                else f"{quoted_bin} -r {session_id}"
            )
        # Apply the same API-key cleanup for every terminal launch. Subscription
        # CLIs avoid unexpected-key prompts; API-key CLIs that need a key can
        # re-read it from their own shell/keychain setup.
        unset_key = "unset ANTHROPIC_API_KEY && "
        if sys.platform == "darwin":
            subprocess.Popen(
                [
                    "osascript",
                    "-e",
                    f'tell application "Terminal"\n'
                    f"  activate\n"
                    f'  do script "{unset_key}cd {shlex.quote(cwd)} && {cmd}"\n'
                    f"end tell",
                ]
            )
        elif sys.platform.startswith("linux"):
            for term in ["gnome-terminal", "konsole", "xfce4-terminal"]:
                if shutil.which(term):
                    subprocess.Popen(
                        [
                            term,
                            "--",
                            "bash",
                            "-c",
                            f"{unset_key}cd {shlex.quote(cwd)} && {cmd}; exec bash",
                        ]
                    )
                    break
            else:
                subprocess.Popen(
                    [
                        "x-terminal-emulator",
                        "-e",
                        "bash",
                        "-c",
                        f"{unset_key}cd {shlex.quote(cwd)} && {cmd}; exec bash",
                    ]
                )
        else:
            subprocess.Popen(
                [
                    "cmd",
                    "/c",
                    "start",
                    "cmd",
                    "/k",
                    f'cd /d "{cwd}" && {cmd}',
                ]
            )
        return {"ok": True}

    # -----------------------------------------------------------------------
    # Headless runner — send/stop/status
    # -----------------------------------------------------------------------

    @app.post("/api/sessions/new")
    async def new_session(req: _NewSessionRequest):
        """Mint metadata for a brand-new Claude session (issue #9).

        This endpoint is a PURE METADATA MINT — it does NOT spawn the CLI and
        does NOT write any JSONL on disk. It stashes a ``_PendingSession`` in
        ``app.state.pending_sessions`` keyed by the new uuid. The user's first
        real PTY submit for that id is what materialises the session via
        ``claude --session-id``.

        Why no spawn here: an auto-greeting that materialises the JSONL would
        appear as the user's first turn in the conversation, which is wrong.
        A new session must land empty.

        Defaults (all overridable):
          * ``cwd`` — last-used cwd from the most-recent indexed session.
          * ``permission_mode`` — ``AppConfig.claude_default_permission_mode``.
        """
        import uuid

        # Resolve cwd default → last-used session's cwd. Query directly
        # (not via get_sessions) because we want to fall back to any session
        # with a cwd on disk, including untitled ones.
        cwd = req.cwd
        if not cwd:
            async with Database(db_path) as db:
                assert db._conn is not None
                async with db._conn.execute(
                    "SELECT cwd FROM sessions WHERE cwd IS NOT NULL "
                    "ORDER BY updated_at DESC"
                ) as cursor:
                    rows = await cursor.fetchall()
            for row in rows:
                candidate = row["cwd"]
                if candidate and Path(candidate).is_dir():
                    cwd = candidate
                    break
        if not cwd:
            raise HTTPException(
                status_code=400,
                detail="cwd is required and no prior session cwd is available",
            )
        if not Path(cwd).is_dir():
            raise HTTPException(status_code=404, detail=f"Directory not found: {cwd}")

        # The binary that writes the JSONL determines which data_path the
        # session shows up under, so a brand-new session must use the CLI
        # whose data root is the active profile's data_path. Otherwise a
        # ``crad``-profile user clicking "New Task" while the work profile
        # is active would silently spawn vanilla ``claude`` and the session
        # would never appear in their active-profile sidebar. We validate
        # the resolved bin up front so the caller learns about a missing
        # CLI now rather than only on their first message.
        # Provider override (e.g. "+" from inside a Codex chat) wins over the
        # active-profile default so the new chat stays on the same harness. A
        # driver-backed provider maps to its CLI bin (codex → "codex"); anything
        # else falls back to the active profile's binary.
        if req.provider and _driver_supports(req.provider):
            bin_name = req.provider
        else:
            bin_name = _active_profile_bin_name(_state["config"])
        if shutil.which(bin_name) is None:
            raise HTTPException(status_code=503, detail=f"{bin_name} not found on PATH")

        permission_mode = (
            req.permission_mode
            or _state["config"].claude_default_permission_mode
            or "default"
        )
        new_id = str(uuid.uuid4())
        async with app.state.pending_sessions_lock:
            app.state.pending_sessions[new_id] = _PendingSession(
                cwd=cwd,
                permission_mode=permission_mode,
                bin_name=bin_name,
            )
        return {
            "session_id": new_id,
            "cwd": cwd,
            "permission_mode": permission_mode,
        }

    def _pty_busy_snapshot(session_id: str) -> dict:
        """Return the sidebar busy-badge shape for ``session_id``.

        Busy state is derived from PtyManager. The FE polls
        ``/api/runner-status?ids=...`` (kept by name for API compatibility);
        Phase 7+ may rename the endpoint to ``/api/pty/status-batch``.

        Busy heuristic: PTY alive, the channel has seen input within
        ~2 minutes, and it's emitted output within the last 2 seconds.
        The output window catches streaming (TUI redraws on every chunk);
        the input bound prevents long-idle channels from being flagged as
        busy after the user walks away.
        """
        if _pty_manager is None:
            return {
                "busy": False,
                "last_error": None,
                "permission_mode": None,
            }
        pty = _pty_manager.status(session_id)
        now_ms = int(time.time() * 1000)
        last_in = int(pty.get("last_input_ms") or 0)
        last_out = int(pty.get("last_pty_output_ms") or 0)
        pty_busy = (
            bool(pty.get("alive"))
            and last_in > 0
            and (now_ms - last_in) < 120_000
            and (now_ms - last_out) < 2_000
        )
        return {
            "busy": pty_busy,
            "last_error": None,
            "permission_mode": pty.get("permission_mode") or None,
        }

    @app.get("/api/runner-status")
    async def runner_status_batch(ids: str = Query("")):
        """Batch busy-status for a comma-separated id list → ``{id: status}``.

        Drives the sidebar busy-indicator. One shared poll for the whole
        visible session list, capped to bound a pathological query string.
        """
        session_ids = [s for s in ids.split(",") if s][:500]
        return {sid: _pty_busy_snapshot(sid) for sid in session_ids}

    # -----------------------------------------------------------------------
    # Recaps
    # -----------------------------------------------------------------------

    _RECAP_PROMPT = (
        "In 1-2 short sentences, recap where this conversation left off. "
        "No bullets, no preamble. Just the sentences. This is a glance-and-go "
        "context-restore inline in the chat view, not a summary document."
    )

    @app.post("/api/sessions/{session_id}/recap")
    async def create_recap(session_id: str):
        async with Database(db_path) as db:
            detail = await db.get_session_detail(session_id)
            if detail is None:
                raise HTTPException(status_code=404, detail="Session not found")
            if detail.is_fork:
                raise HTTPException(
                    status_code=422,
                    detail="fork sessions are not valid --resume targets",
                )
            cwd = detail.cwd or str(Path(detail.file_path).parent)
            if not Path(cwd).is_dir():
                raise HTTPException(
                    status_code=404, detail=f"Directory not found: {cwd}"
                )
            bin_name = _derive_bin_name(detail.file_path)
            if shutil.which(bin_name) is None:
                raise HTTPException(
                    status_code=503, detail=f"{bin_name} not found on PATH"
                )
            latest_uuid: str | None = (
                detail.messages[-1].id if detail.messages else None
            )

            # Phase 8: recap runs through a hidden PTY against a forked
            # session. Pre-mint the fork id and tombstone it so the
            # watcher doesn't briefly surface the fork JSONL in the
            # sidebar between file creation and the recap runner's
            # cleanup unlink.
            import uuid

            fork_id = str(uuid.uuid4())
            _deleted_tombstones.add(fork_id)
            # Pre-empt the trust-this-folder TUI dialog (same as the
            # main chat PTY does) — without this the fork's first
            # spawn into a fresh cwd would eat the recap prompt while
            # waiting for the user to confirm trust.
            try:
                _ensure_trust(_config_dir_for_bin(_state["config"], bin_name), cwd)
            except Exception as exc:
                _log.warning(
                    "recap: trust pre-flight failed for %s in %s: %s",
                    bin_name,
                    cwd,
                    exc,
                )
            text = await _generate_recap(
                session_id,
                cwd=cwd,
                bin_name=bin_name,
                prompt=_RECAP_PROMPT,
                source_jsonl_path=Path(detail.file_path),
                fork_id=fork_id,
            )
            if text is None:
                raise HTTPException(status_code=502, detail="recap generation failed")
            new_id = await db.insert_recap(session_id, text, latest_uuid)
            recaps = await db.list_recaps(session_id, include_dismissed=True)
        row = next((r for r in recaps if r["id"] == new_id), None)
        if row is None:
            raise HTTPException(
                status_code=500, detail="recap row missing after insert"
            )
        return row

    @app.get("/api/sessions/{session_id}/recaps")
    async def list_recaps_route(
        session_id: str,
        include_dismissed: bool = Query(False),
    ):
        async with Database(db_path) as db:
            return await db.list_recaps(session_id, include_dismissed=include_dismissed)

    @app.post("/api/sessions/{session_id}/recaps/{recap_id}/dismiss")
    async def dismiss_recap_route(session_id: str, recap_id: int):
        async with Database(db_path) as db:
            ok = await db.dismiss_recap(recap_id)
        if not ok:
            raise HTTPException(status_code=404, detail="Recap not found")
        return {"ok": True, "dismissed": True}

    @app.get("/api/sessions/{session_id}/ephemerals")
    async def list_ephemerals_route(session_id: str):
        """Return ordered ephemeral_messages for *session_id* (e.g. /btw pairs).

        Both user and assistant rows are returned; the FE pairs them via
        ``responds_to`` and interleaves with regular messages by timestamp.
        """
        async with Database(db_path) as db:
            return await db.get_ephemeral_messages(session_id)

    @app.put("/api/sessions/{session_id}/title")
    async def set_session_title(session_id: str, req: _SessionTitleRequest):
        """Persist a user-supplied rename and broadcast to other clients.

        Body: ``{"title": str | null}`` — null/blank clears the override.
        Returns ``{ok, id, custom_title}`` so the caller can reconcile.
        """
        async with Database(db_path) as db:
            existing = await db.get_session_file_path(session_id)
            if existing is None:
                raise HTTPException(status_code=404, detail="Session not found")
            stored = await db.set_custom_title(session_id, req.title)
        # The detail cache embeds custom_title — drop it so the next read
        # rebuilds with the override.
        _invalidate_detail_cache(session_id)
        _bus.publish({"type": "session-meta", "id": session_id, "title": stored})
        return {"ok": True, "id": session_id, "custom_title": stored}

    @app.put("/api/sessions/{session_id}/archived")
    async def set_session_archived(session_id: str, req: _SessionArchivedRequest):
        """Persist the archived flag server-side and broadcast.

        Body: ``{"archived": bool}``. Returns ``{ok, id, archived_at}``.
        Replaces the previous localStorage-only state (LS.ARCHIVED), which
        meant a second browser saw stale state.
        """
        async with Database(db_path) as db:
            if await db.get_session_file_path(session_id) is None:
                raise HTTPException(status_code=404, detail="Session not found")
            stored = await db.set_archived(session_id, req.archived)
        _invalidate_detail_cache(session_id)
        _bus.publish({"type": "session-meta", "id": session_id, "archived_at": stored})
        return {"ok": True, "id": session_id, "archived_at": stored}

    @app.put("/api/sessions/{session_id}/starred")
    async def set_session_starred(session_id: str, req: _SessionStarredRequest):
        """Persist the starred flag server-side. Same shape as ``archived``."""
        async with Database(db_path) as db:
            if await db.get_session_file_path(session_id) is None:
                raise HTTPException(status_code=404, detail="Session not found")
            stored = await db.set_starred(session_id, req.starred)
        _invalidate_detail_cache(session_id)
        _bus.publish({"type": "session-meta", "id": session_id, "starred_at": stored})
        return {"ok": True, "id": session_id, "starred_at": stored}

    @app.put("/api/sessions/{session_id}/viewed")
    async def set_session_viewed(session_id: str, req: _SessionViewedRequest):
        """Record the wall-clock the session was last viewed by the user.

        Body: ``{"viewed_at": str | null}``. Pass ``null`` to mark unread.
        The FE typically passes the session's current ``updated_at`` so the
        bell stays dismissed only as long as no new messages have arrived.
        """
        async with Database(db_path) as db:
            if await db.get_session_file_path(session_id) is None:
                raise HTTPException(status_code=404, detail="Session not found")
            stored = await db.set_viewed_at(session_id, req.viewed_at)
        _invalidate_detail_cache(session_id)
        _bus.publish({"type": "session-meta", "id": session_id, "viewed_at": stored})
        return {"ok": True, "id": session_id, "viewed_at": stored}

    @app.post("/api/sessions/migrate-localstorage")
    async def migrate_session_localstorage(req: _LocalStorageMigrationRequest):
        """One-time migration: import localStorage-only meta into the DB.

        Called by the FE on first load after the upgrade so existing users
        don't lose their archive / star / read-receipt state.  Idempotent —
        the FE should clear the corresponding localStorage keys only after
        a 200 response, but calling this twice with overlapping ids just
        rewrites the same timestamps.

        Body shape:
            { "archived": [sid, ...],
              "starred":  [sid, ...],
              "viewed_at": { sid: iso_timestamp, ... } }
        Returns counts of rows actually written. Missing sessions are
        silently skipped (deleted-since-archive is fine — no row needed).
        """
        applied = {"archived": 0, "starred": 0, "viewed_at": 0}
        async with Database(db_path) as db:
            for sid in req.archived:
                if await db.get_session_file_path(sid) is not None:
                    await db.set_archived(sid, True)
                    applied["archived"] += 1
            for sid in req.starred:
                if await db.get_session_file_path(sid) is not None:
                    await db.set_starred(sid, True)
                    applied["starred"] += 1
            for sid, ts in req.viewed_at.items():
                if await db.get_session_file_path(sid) is not None:
                    await db.set_viewed_at(sid, ts)
                    applied["viewed_at"] += 1
        # No per-session SSE — the FE is expected to refetch its session
        # list once after migration.  Single broadcast tells other tabs to
        # do the same.
        _bus.publish({"type": "session-meta-bulk-migration", "applied": applied})
        return {"ok": True, "applied": applied}

    @app.post("/api/sessions/delete")
    async def delete_sessions(req: _SessionDeleteRequest):
        """Hard-delete sessions: unlinks the on-disk .jsonl file and removes
        all DB rows (messages, FTS, recaps, session_meta, session).

        Race-free design:
          1. Every session id is added to ``_deleted_tombstones`` BEFORE any
             unlink or DB delete, so concurrent ``do_scan`` / ``_scan_one``
             iterations that are already running skip re-inserting the row.
          2. All DB work for the whole batch runs inside ONE ``Database``
             context, eliminating the per-id connection-open/close churn
             (busy-timeout lock contention).

        Path safety: each file_path is resolved with os.path.realpath and
        verified to live under one of the configured scan roots AND to end
        with '.jsonl'.  Any path that fails this check is recorded in
        ``failed`` and skipped — the file is never touched.

        Returns {"ok": true, "deleted": [...ids], "failed": [...{id, error}]}.
        """
        deleted: list[str] = []
        failed: list[dict] = []

        # Compute the set of allowed root prefixes once for the whole batch.
        # Sessions live at <root>/projects/<proj>/<session>.jsonl so the file
        # must resolve under one of the configured scan roots. Claude sessions
        # live under the profile data_paths (get_all_scan_paths); Codex rollouts
        # live under codex_data_paths (~/.codex/sessions by default). Those are
        # deliberately NOT in get_all_scan_paths (that one scopes the Claude
        # watcher/scanner), so they must be added here — otherwise every Codex
        # session fails this safety check and can't be deleted.
        scan_roots = [
            os.path.realpath(str(Path(p).expanduser()))
            for p in (
                list(_state["config"].get_all_scan_paths())
                + list(_state["config"].codex_data_paths)
            )
        ]

        # Phase 0: tombstone ALL requested ids BEFORE touching disk or DB so
        # that any in-flight do_scan / _scan_one iteration sees the guard
        # before we do the unlink + row delete.
        for session_id in req.session_ids:
            _deleted_tombstones.add(session_id)

        # Kill live PTY channels for all requested sessions (idempotent when
        # no channel exists) before opening the DB, so an attached claude can't
        # keep writing to the JSONL we're about to unlink.
        if _pty_manager is not None:
            for session_id in req.session_ids:
                try:
                    await _pty_manager.kill(session_id)
                except Exception:
                    pass
        # Also tear down any driver-backed (Codex/tmux) sessions for these ids.
        if _driver_manager is not None:
            for session_id in req.session_ids:
                if session_id in app.state.driver_sessions:
                    try:
                        await _driver_manager.kill(session_id)
                    except Exception:
                        pass
                    app.state.driver_sessions.discard(session_id)

        # Map of session_id -> resolved file path for files that pass safety
        # checks and should be unlinked AFTER the DB transaction commits.
        to_unlink: dict[str, str] = {}

        # Single DB connection for the whole batch — no per-id lock churn.
        try:
            async with Database(db_path) as db:
                for session_id in req.session_ids:
                    try:
                        fp = await db.get_session_file_path(session_id)

                        if fp is not None:
                            # --- Path safety check ---
                            resolved = os.path.realpath(fp)
                            if not resolved.endswith(".jsonl"):
                                failed.append(
                                    {
                                        "id": session_id,
                                        "error": "path outside sessions root / not .jsonl",
                                    }
                                )
                                # Do NOT delete row, do NOT unlink.
                                _deleted_tombstones.discard(session_id)
                                continue
                            if not any(
                                resolved == r or resolved.startswith(r + os.sep)
                                for r in scan_roots
                            ):
                                failed.append(
                                    {
                                        "id": session_id,
                                        "error": "path outside sessions root / not .jsonl",
                                    }
                                )
                                _deleted_tombstones.discard(session_id)
                                continue
                            to_unlink[session_id] = resolved

                        await db.delete_session(session_id)
                        deleted.append(session_id)

                    except Exception as exc:
                        failed.append({"id": session_id, "error": str(exc)})
                        _deleted_tombstones.discard(session_id)
        except Exception as exc:
            # Whole-batch DB open failure — mark everything not yet handled.
            handled = {e["id"] for e in failed} | set(deleted)
            for session_id in req.session_ids:
                if session_id not in handled:
                    failed.append({"id": session_id, "error": str(exc)})
                    _deleted_tombstones.discard(session_id)

        # Unlink on-disk files for successfully deleted sessions.
        for session_id in deleted:
            resolved = to_unlink.get(session_id)
            if resolved:
                try:
                    os.unlink(resolved)
                except FileNotFoundError:
                    pass  # already gone — still counts as success
            _invalidate_detail_cache(session_id)

        # Broadcast a single refresh event so all connected clients reload
        # their session list (same event type the file-watcher uses).
        if deleted:
            _bus.publish({"type": "refresh", "path": ""})

        return {"ok": True, "deleted": deleted, "failed": failed}

    @app.post("/api/refresh")
    async def refresh():
        async with Database(db_path) as db:
            await do_scan(db)
        return {"ok": True}

    @app.get("/api/events")
    async def events(request: Request):
        # Each /api/events connection gets its own queue off the shared
        # broadcaster. Without this, multiple clients on the same server
        # would steal events from each other (issue #11).
        client_queue = _bus.subscribe()

        async def _wait_disconnect() -> None:
            # request.is_disconnected() is a NON-BLOCKING peek (uses a pre-cancelled
            # anyio scope) — it returns False immediately. To actually block until the
            # client disconnects we have to consume from request.receive() ourselves.
            while True:
                message = await request.receive()
                if message["type"] == "http.disconnect":
                    return

        async def generate():
            disconnect_task = asyncio.ensure_future(_wait_disconnect())
            try:
                while True:
                    queue_get = asyncio.ensure_future(client_queue.get())
                    try:
                        done, _ = await asyncio.wait(
                            {queue_get, disconnect_task},
                            timeout=30.0,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                    except Exception:
                        queue_get.cancel()
                        return
                    if disconnect_task in done:
                        queue_get.cancel()
                        return
                    if queue_get in done:
                        event = queue_get.result()
                        yield f"data: {json.dumps(event)}\n\n"
                    else:
                        # Timeout — emit keepalive and poll again.
                        queue_get.cancel()
                        yield ": keepalive\n\n"
            finally:
                disconnect_task.cancel()
                _bus.unsubscribe(client_queue)

        return StreamingResponse(generate(), media_type="text/event-stream")

    # ------------------------------------------------------------------
    # File system browser (read-only, sandboxed)
    # ------------------------------------------------------------------

    _EXTENSION_LANGUAGE: dict[str, str] = {
        ".py": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".jsx": "javascript",
        ".rs": "rust",
        ".go": "go",
        ".java": "java",
        ".kt": "kotlin",
        ".rb": "ruby",
        ".php": "php",
        ".c": "c",
        ".cpp": "cpp",
        ".h": "c",
        ".hpp": "cpp",
        ".cs": "csharp",
        ".swift": "swift",
        ".scala": "scala",
        ".sh": "bash",
        ".bash": "bash",
        ".zsh": "bash",
        ".sql": "sql",
        ".html": "xml",
        ".xml": "xml",
        ".css": "css",
        ".json": "json",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".toml": "ini",
        ".md": "markdown",
        ".lua": "lua",
        ".r": "r",
        ".pl": "perl",
        ".ex": "elixir",
        ".exs": "elixir",
        ".erl": "erlang",
        ".hs": "haskell",
        ".ml": "ocaml",
        ".vim": "vim",
        ".dockerfile": "dockerfile",
        ".tf": "hcl",
        ".dart": "dart",
    }

    async def _allowed_prefixes() -> set[str]:
        """Collect all filesystem paths the FS browser may access."""
        prefixes: set[str] = set()
        for p in _state["config"].get_all_scan_paths():
            prefixes.add(str(Path(p).expanduser().resolve()))
        async with Database(db_path) as db:
            data_sources = _state["config"].get_active_data_sources()
            projects = await db.get_projects(data_sources=data_sources)
            for proj in projects:
                if proj.resolved_path:
                    prefixes.add(str(Path(proj.resolved_path).resolve()))
            for session in await db.get_sessions(data_sources=data_sources):
                if session.cwd:
                    prefixes.add(str(Path(session.cwd).resolve()))
        return prefixes

    def _validate_fs_path(requested: str, prefixes: set[str]) -> Path:
        resolved = Path(requested).resolve()
        if not any(str(resolved).startswith(p) for p in prefixes):
            raise HTTPException(
                status_code=403, detail="Path outside allowed directories"
            )
        return resolved

    @app.get("/api/host-info")
    async def host_info(request: Request):
        """Per-connection metadata for the UI to gate host-side actions.

        ``is_remote_client`` is True when the request did NOT originate from a
        loopback address on the server box. Used by the frontend to disable
        actions that run osascript / xdg-open on the SERVER's host (e.g.
        "Open in terminal", "Reveal in Finder") — those would silently fire
        on the host machine and confuse a remote viewer.

        Caveat: this can't see through ssh -L / Tailscale forwards. A user
        tunneling to 127.0.0.1 reads as local to the server, which is the
        correct behavior — the actions DO run somewhere they can see.
        """
        host = request.client.host if request.client else None
        is_local = False
        if host:
            if host == "localhost":
                is_local = True
            else:
                try:
                    is_local = ipaddress.ip_address(host).is_loopback
                except ValueError:
                    is_local = False
        return {
            "is_remote_client": not is_local,
            "platform": sys.platform,
            "client_host": host,
            "version": __version__,
        }

    UPDATE_CHECK_INTERVAL_S = 6 * 60 * 60  # cache: PyPI-friendly, still same-day fresh

    @app.get("/api/update-check")
    async def update_check():
        """Best-effort PyPI version check backing the About panel's update
        banner. In-process cached so re-opening Settings doesn't hit PyPI on
        every click; a failed/offline check just reports no update available
        rather than erroring the UI (see update_check.fetch_latest_version).
        """
        cache = _state.get("update_check_cache")
        now = time.monotonic()
        if cache is None or now - cache["checked_at"] > UPDATE_CHECK_INTERVAL_S:
            latest = await fetch_latest_version()
            cache = {"checked_at": now, "latest": latest}
            _state["update_check_cache"] = cache
        latest = cache["latest"]
        return {
            "current_version": __version__,
            "latest_version": latest,
            "update_available": bool(latest) and is_newer(latest, __version__),
        }

    @app.get("/api/fs/list")
    async def fs_list(
        path: str = Query(...),
        show_hidden: bool = Query(False),
    ):
        prefixes = await _allowed_prefixes()
        resolved = _validate_fs_path(path, prefixes)
        if not resolved.is_dir():
            raise HTTPException(status_code=404, detail="Not a directory")

        entries: list[dict] = []
        for child in resolved.iterdir():
            if not show_hidden and child.name.startswith("."):
                continue
            try:
                stat = child.stat()
            except OSError:
                continue
            entries.append(
                {
                    "name": child.name,
                    "type": "dir" if child.is_dir() else "file",
                    "size": stat.st_size if child.is_file() else None,
                    "modified": stat.st_mtime,
                }
            )

        dirs = sorted(
            [e for e in entries if e["type"] == "dir"], key=lambda e: e["name"].lower()
        )
        files = sorted(
            [e for e in entries if e["type"] == "file"], key=lambda e: e["name"].lower()
        )
        return {"path": str(resolved), "entries": dirs + files}

    @app.put("/api/fs/write")
    async def fs_write(body: _FsWriteBody):
        # File-preview editing is always available; edit_enabled gates only
        # message edit/delete on the chat side.
        prefixes = await _allowed_prefixes()
        resolved = _validate_fs_path(body.path, prefixes)
        if resolved.is_dir():
            raise HTTPException(status_code=400, detail="Path is a directory")
        # Refuse to create new files for now — only edit existing.
        if not resolved.exists():
            raise HTTPException(status_code=404, detail="File does not exist")
        encoded = body.content.encode("utf-8")
        if len(encoded) > 2 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="File too large (max 2MB)")
        if b"\x00" in encoded:
            raise HTTPException(status_code=415, detail="Binary content not allowed")
        try:
            with open(resolved, "w", encoding="utf-8") as f:
                f.write(body.content)
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"Write failed: {e}")
        return {"ok": True, "path": str(resolved), "size": len(encoded)}

    @app.get("/api/fs/read")
    async def fs_read(path: str = Query(...)):
        prefixes = await _allowed_prefixes()
        resolved = _validate_fs_path(path, prefixes)
        if not resolved.is_file():
            raise HTTPException(status_code=404, detail="Not a file")

        size = resolved.stat().st_size
        if size > 2 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="File too large (max 2MB)")

        with open(resolved, "rb") as f:
            head = f.read(8192)
        if b"\x00" in head:
            raise HTTPException(status_code=415, detail="Binary file")

        content = head.decode("utf-8", errors="replace")
        if size > 8192:
            with open(resolved, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()

        ext = resolved.suffix.lower()
        language = _EXTENSION_LANGUAGE.get(ext)
        if not language and resolved.name == "Dockerfile":
            language = "dockerfile"
        if not language and resolved.name == "Makefile":
            language = "makefile"

        return {
            "path": str(resolved),
            "name": resolved.name,
            "content": content,
            "size": size,
            "language": language,
        }

    @app.get("/api/fs/blob")
    async def fs_blob(path: str = Query(...)):
        import mimetypes

        prefixes = await _allowed_prefixes()
        resolved = _validate_fs_path(path, prefixes)
        if not resolved.is_file():
            raise HTTPException(status_code=404, detail="Not a file")

        size = resolved.stat().st_size
        if size > 50 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="File too large (max 50MB)")

        mime_type, _ = mimetypes.guess_type(str(resolved))
        if not mime_type:
            mime_type = "application/octet-stream"

        from starlette.responses import FileResponse as _BlobFileResponse

        return _BlobFileResponse(
            str(resolved),
            media_type=mime_type,
            headers={"Cache-Control": "private, max-age=60"},
        )

    # ------------------------------------------------------------------
    # PTY runner endpoints (Phase 1)
    # ------------------------------------------------------------------

    async def _resolve_pty_focus_params(
        session_id: str,
        *,
        bin_override: str | None = None,
        cwd_override: str | None = None,
        permission_mode_override: str | None = None,
    ) -> tuple[str, str, str, bool, Path | None]:
        """Resolve (bin_name, cwd, permission_mode, new_chat, jsonl_path) for PTY focus/submit.

        Priority for PTY focus/submit:
          1. Explicit override (caller knows best).
          2. Existing on-disk session detail (resume case → new_chat=False).
          3. ``app.state.pending_sessions`` entry minted by ``/api/sessions/new``
             (no JSONL yet → new_chat=True).
          4. Active profile fallback for ad-hoc calls (treated as resume).

        ``jsonl_path`` is ``None`` when no JSONL exists yet (new_chat=True
        + pending session). For resumes we hand back the on-disk JSONL
        path so the PTY runner can run its Phase-0 ownership check.
        """
        cfg = _state["config"]
        bin_name: str | None = bin_override
        cwd: str | None = cwd_override
        permission_mode: str | None = permission_mode_override
        new_chat = False
        jsonl_path: Path | None = None

        async with Database(db_path) as db:
            detail = await db.get_session_detail(session_id)
        if detail is not None:
            if bin_name is None:
                bin_name = _derive_bin_name(detail.file_path)
            if cwd is None:
                cwd = detail.cwd or str(Path(detail.file_path).parent)
            if permission_mode is None and detail.permission_mode:
                permission_mode = detail.permission_mode
            jsonl_path = Path(detail.file_path)
        else:
            async with app.state.pending_sessions_lock:
                pending = app.state.pending_sessions.get(session_id)
            if pending is not None:
                new_chat = True
                if bin_name is None:
                    bin_name = pending.bin_name
                if cwd is None:
                    cwd = pending.cwd
                if permission_mode is None:
                    permission_mode = pending.permission_mode
            else:
                if bin_name is None:
                    bin_name = _active_profile_bin_name(cfg)

        if cwd is None:
            cwd = os.getcwd()
        if permission_mode is None:
            permission_mode = cfg.claude_default_permission_mode or "default"
        if bin_name is None:
            bin_name = "claude"

        return bin_name, cwd, permission_mode, new_chat, jsonl_path

    @app.post("/api/pty/focus")
    async def pty_focus(req: _PtyFocusRequest):
        (
            bin_name,
            cwd,
            permission_mode,
            resolved_new_chat,
            jsonl_path,
        ) = await _resolve_pty_focus_params(
            req.session_id,
            bin_override=req.bin_name,
            cwd_override=req.cwd,
            permission_mode_override=req.permission_mode,
        )
        # Driver-backed providers (Codex) route to the DriverManager instead of
        # the Claude direct-PTY path. Gate on effective can_send so a read-only
        # / non-drivable Codex session 409s here rather than bringing a live
        # process up (until caps flip in 4e this is always a 409 for Codex).
        provider = await _resolve_provider(req.session_id)
        driver_new_chat = req.new_chat or resolved_new_chat
        if _driver_supports(provider):
            _require_capability(provider, "can_send")
            if _driver_manager is None:
                raise HTTPException(status_code=503, detail="driver manager not ready")
            # A brand-new driver chat has no rollout to resume — spawn fresh
            # (resume_uuid=None). The CLI (codex) mints its OWN rollout UUID on
            # the first message; we adopt it below so the placeholder id is
            # replaced by the real, persisted session.
            adopt_after_ts = time.time() - 2.0
            await _driver_manager.focus(
                req.session_id,
                provider=provider,
                cwd=cwd,
                model=req.model,
                resume_uuid=req.session_id,
                new_chat=driver_new_chat,
                rows=req.rows,
            )
            app.state.driver_sessions.add(req.session_id)
            if driver_new_chat:
                # Watch for the real rollout codex writes on the first message,
                # then re-key placeholder → real id and tell the FE to navigate.
                asyncio.ensure_future(
                    _adopt_codex_session(req.session_id, cwd, adopt_after_ts)
                )
            return {"ok": True}
        # Caller's explicit req.new_chat wins; otherwise use the resolver's
        # inferred value (True iff session lives in pending_sessions only).
        new_chat = req.new_chat or resolved_new_chat
        # Pre-empt claude's trust-this-folder TUI dialog. Without this the
        # dialog eats the first user message (Enter selects "Yes, I trust"
        # and the typed content goes nowhere). See _ensure_trust docstring.
        try:
            _ensure_trust(_config_dir_for_bin(_state["config"], bin_name), cwd)
        except Exception as exc:
            _log.warning(
                "pty: trust pre-flight failed for %s in %s: %s",
                bin_name,
                cwd,
                exc,
            )
        try:
            await _pty_manager.focus(
                req.session_id,
                cwd=cwd,
                bin_name=bin_name,
                model=req.model or "",
                permission_mode=permission_mode,
                new_chat=new_chat,
                rows=req.rows if req.rows is not None else DEFAULT_ROWS,
                jsonl_path=jsonl_path,
            )
        except PtyOwnershipConflict as exc:
            # Foreign claude already owns the JSONL — surface as 409 so
            # the FE can render its Take-over banner without surprising
            # the user with a spinner that never finishes.
            raise HTTPException(
                status_code=409,
                detail={
                    "kind": "pty_ownership_conflict",
                    "session_id": req.session_id,
                    "foreign_pids": exc.foreign_pids,
                    "jsonl_path": str(jsonl_path) if jsonl_path else None,
                },
            )
        return {"ok": True}

    @app.post("/api/pty/blur")
    async def pty_blur(req: _PtyBlurRequest):
        # Driver-backed sessions have no idle reaper — blur is a no-op so the
        # tmux session survives navigation/disconnect (the persistence point).
        if _is_driver_session(req.session_id):
            return {"ok": True}
        await _pty_manager.unfocus(req.session_id)
        return {"ok": True}

    @app.post("/api/pty/submit")
    async def pty_submit(req: _PtySubmitRequest):
        # Defensive: if the session's cwd directory was deleted since the
        # FE last loaded the detail, refuse the submit with a clear 410.
        # The FE banner + handleSend short-circuit cover the typical path;
        # this protects against the cwd disappearing mid-session.
        async with Database(db_path) as db:
            _detail = await db.get_session_detail(req.session_id)
        if _detail is not None and _detail.cwd and not _detail.cwd_exists:
            raise HTTPException(
                status_code=410,
                detail=(
                    f"Session's working directory no longer exists: "
                    f"{_detail.cwd}. Cannot deliver the message."
                ),
            )
        # Driver-backed providers (Codex) submit through the DriverManager.
        # Gate on effective can_send: a read-only / no-tmux Codex session 409s
        # here instead of falling through and wrongly spawning a *claude* PTY
        # against the Codex cwd (the read-only-honesty bug). Until caps flip in
        # 4e this is always a 409 for Codex.
        # Resolve provider via the pending-aware helper so a brand-new Codex
        # chat (no on-disk detail yet) still routes to the DriverManager instead
        # of falling through to the Claude PTY path.
        provider = await _resolve_provider(req.session_id)
        if _driver_supports(provider):
            _require_capability(provider, "can_send")
            if _driver_manager is None:
                raise HTTPException(status_code=503, detail="driver manager not ready")
            if _detail is not None:
                cwd = _detail.cwd or str(Path(_detail.file_path).parent)
                driver_new_chat = False
            else:
                async with app.state.pending_sessions_lock:
                    pending = app.state.pending_sessions.get(req.session_id)
                cwd = pending.cwd if pending is not None else os.getcwd()
                driver_new_chat = pending is not None
            adopt_after_ts = time.time() - 2.0
            await _driver_manager.focus(
                req.session_id,
                provider=provider,
                cwd=cwd,
                model=req.model,
                resume_uuid=req.session_id,
                new_chat=driver_new_chat,
            )
            app.state.driver_sessions.add(req.session_id)
            if driver_new_chat:
                asyncio.ensure_future(
                    _adopt_codex_session(req.session_id, cwd, adopt_after_ts)
                )
            await _driver_manager.submit(req.session_id, req.content)
            return {"ok": True}
        # Lazily ensure focus. The frontend wires onFocus on the chat input
        # but that doesn't always fire (autofocus on mount, programmatic
        # focus, navigation between sessions with cursor still in input).
        # Auto-focusing on submit keeps the user-facing contract simple:
        # POST /api/pty/submit just works for any valid session_id.
        #
        # Model handling: ``req.model`` reflects the model picker's current
        # selection at submit time. If the live channel was spawned with a
        # different ``--model``, kill it so the auto-focus block below
        # respawns with the user's intended model — claude's TUI is locked
        # to its spawn-time ``--model``, so a mid-session change requires
        # a respawn. This costs the next message ~1-3s of spawn time but
        # honours the user's expectation that the picker matches the reply.
        requested_model = req.model or ""
        status = _pty_manager.status(req.session_id)
        # Heuristic for "turn currently in flight": user input has been
        # written more recently than the last drained PTY byte. We skip
        # the model switch during a live turn — sending /model mid-stream
        # would mean the in-flight reply finishes on the old model and the
        # *next* turn uses the new one, but the slash command itself shows
        # up in JSONL out of order. The picker change takes effect on the
        # next idle submit instead. UX cost: one "wrong-model" turn after
        # switching mid-stream.
        last_in = int(status.get("last_input_ms") or 0)
        last_out = int(status.get("last_pty_output_ms") or 0)
        turn_in_flight = last_in > 0 and last_in > last_out
        if (
            status.get("alive")
            and requested_model
            and status.get("model") != requested_model
            and not turn_in_flight
        ):
            # Use claude's /model slash command to switch the live TUI's
            # model in place. Keeps the TUI alive, conversation context
            # intact, near-instant — vs the old kill+respawn path that
            # blocked the submit for 10-15s while the new TUI bootstrapped.
            await _pty_manager.switch_model(req.session_id, requested_model)
        if not status.get("alive"):
            (
                bin_name,
                cwd,
                permission_mode,
                new_chat,
                jsonl_path,
            ) = await _resolve_pty_focus_params(req.session_id)
            try:
                _ensure_trust(_config_dir_for_bin(_state["config"], bin_name), cwd)
            except Exception as exc:
                _log.warning(
                    "pty: trust pre-flight failed for %s in %s: %s", bin_name, cwd, exc
                )
            try:
                await _pty_manager.focus(
                    req.session_id,
                    cwd=cwd,
                    bin_name=bin_name,
                    model=requested_model,
                    permission_mode=permission_mode,
                    new_chat=new_chat,
                    jsonl_path=jsonl_path,
                )
            except PtyOwnershipConflict as exc:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "kind": "pty_ownership_conflict",
                        "session_id": req.session_id,
                        "foreign_pids": exc.foreign_pids,
                        "jsonl_path": str(jsonl_path) if jsonl_path else None,
                    },
                )
        try:
            await _pty_manager.submit(req.session_id, req.content)
        except PtySubmitInFlight as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except PtyOwnershipConflict as exc:
            # Auto-respawn inside submit() can hit the same conflict if
            # a foreign claude attached during our idle-kill window.
            raise HTTPException(
                status_code=409,
                detail={
                    "kind": "pty_ownership_conflict",
                    "session_id": req.session_id,
                    "foreign_pids": exc.foreign_pids,
                },
            )
        return {"ok": True}

    def _is_driver_session(session_id: str) -> bool:
        """True if this session is currently routed to the DriverManager."""
        return session_id in app.state.driver_sessions

    @app.post("/api/pty/kill")
    async def pty_kill(req: _PtyKillRequest):
        if _is_driver_session(req.session_id) and _driver_manager is not None:
            await _driver_manager.kill(req.session_id)
            app.state.driver_sessions.discard(req.session_id)
            return {"ok": True}
        await _pty_manager.kill(req.session_id)
        return {"ok": True}

    @app.get("/api/pty/native-snapshot")
    async def pty_native_snapshot(session_id: str = Query(...)):
        try:
            if _is_driver_session(session_id) and _driver_manager is not None:
                return await _driver_manager.native_snapshot(session_id)
            return _pty_manager.native_snapshot(session_id)
        except RuntimeError as exc:
            raise HTTPException(status_code=404, detail=str(exc))

    @app.post("/api/pty/input")
    async def pty_native_input(req: _PtyNativeInputRequest):
        try:
            if _is_driver_session(req.session_id) and _driver_manager is not None:
                await _driver_manager.write_raw_input(
                    req.session_id, decode_terminal_input(req.data)
                )
            else:
                await _pty_manager.write_raw_input(
                    req.session_id,
                    decode_terminal_input(req.data),
                )
        except RuntimeError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return {"ok": True}

    @app.post("/api/pty/resize")
    async def pty_resize(req: _PtyResizeRequest):
        try:
            if _is_driver_session(req.session_id) and _driver_manager is not None:
                await _driver_manager.resize(req.session_id, req.rows, req.cols)
            else:
                await _pty_manager.resize(req.session_id, req.rows, req.cols)
        except RuntimeError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return {"ok": True}

    @app.get("/api/pty/status")
    async def pty_status(session_id: str = Query(...)):
        if _is_driver_session(session_id) and _driver_manager is not None:
            return _driver_manager.status(session_id)
        return _pty_manager.status(session_id)

    @app.get("/api/pty/ownership/{session_id}")
    async def pty_ownership(session_id: str):
        """Phase-0 ownership snapshot. Pure read — never spawns.

        Returns ``{status, foreign_pids, jsonl_path}`` for the FE badge.
        For sessions whose JSONL doesn't exist yet (brand-new pending
        sessions, or unknown ids), reports ``status="idle"``.
        """
        async with Database(db_path) as db:
            detail = await db.get_session_detail(session_id)
        jsonl_path = Path(detail.file_path) if detail is not None else None
        return _pty_manager.ownership(session_id, jsonl_path)

    @app.post("/api/pty/takeover/{session_id}")
    async def pty_takeover(session_id: str):
        """Phase-0 take-over. SIGINTs each foreign claude attached to the
        session and polls until they release the JSONL fd (or ~3 s
        elapses).

        Returns 200 on success (the FE follows up with the regular focus
        call). Returns 409 if any pid is still attached after the poll
        window — matches the plan's "no SIGTERM/SIGKILL fallback"
        decision: we don't escalate.
        """
        async with Database(db_path) as db:
            detail = await db.get_session_detail(session_id)
        if detail is None:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown session: {session_id}",
            )
        jsonl_path = Path(detail.file_path)
        pids = _session_conflict_pids(session_id, jsonl_path)
        if not pids:
            _unlink_fresh_foreign_sidecar(jsonl_path)
            return {"ok": True, "released_pids": [], "still_held_by": []}
        released: list[int] = []
        for pid in pids:
            try:
                os.kill(pid, signal.SIGINT)
                released.append(pid)
            except ProcessLookupError:
                # Already gone — counts as a release.
                released.append(pid)
            except PermissionError as exc:
                raise HTTPException(
                    status_code=403,
                    detail=(
                        f"Cannot signal pid {pid}: {exc}. "
                        "Take over manually from the owning terminal."
                    ),
                )
        # Poll: claude exits in ~300 ms once SIGINT is processed (see
        # docs/pty-ownership-phase0-findings.md). Budget 3 s.
        end = time.monotonic() + 3.0
        still: list[int] = list(pids)
        while time.monotonic() < end and still:
            await asyncio.sleep(0.1)
            still = _session_conflict_pids(session_id, jsonl_path)
        if still:
            raise HTTPException(
                status_code=409,
                detail={
                    "kind": "pty_takeover_timeout",
                    "session_id": session_id,
                    "still_held_by": still,
                },
            )
        _unlink_fresh_foreign_sidecar(jsonl_path)
        return {"ok": True, "released_pids": released, "still_held_by": []}

    # ------------------------------------------------------------------
    # Static frontend — must be registered LAST (catch-all route)
    # ------------------------------------------------------------------
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        # Hashed JS/CSS assets can be cached aggressively (immutable).
        # index.html must never be cached — its name has no content hash,
        # so browsers would serve stale bundles after a rebuild.
        from fastapi.responses import FileResponse as _FileResponse

        @app.get("/", include_in_schema=False)
        @app.get("/{full_path:path}", include_in_schema=False)
        async def _spa(full_path: str = ""):
            candidate = static_dir / full_path
            if full_path and candidate.exists() and candidate.is_file():
                return _FileResponse(
                    str(candidate),
                    headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
                )
            # Hashed asset misses must 404, NOT fall through to index.html.
            # Otherwise a browser holding a stale chunk hash (e.g. after a
            # rebuild) gets text/html for an /assets/<old-hash>.js request
            # and throws "Failed to fetch dynamically imported module" with
            # no actionable error — see frontend lazyWithRetry which only
            # triggers its reload prompt on a real fetch failure.
            if full_path.startswith("assets/"):
                raise HTTPException(status_code=404)
            index = static_dir / "index.html"
            return _FileResponse(
                str(index),
                headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
            )

    return app


# ---------------------------------------------------------------------------
# Module-level app for `uvicorn agent_decoder.server:app --reload`
# ---------------------------------------------------------------------------


def _build_default_app() -> FastAPI:
    from .config import get_db_path, load_config

    _config = load_config()
    _db_path = get_db_path()
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    return create_app(_config, _db_path)


app = _build_default_app()
