"""CLI entry point — `ring [options] <command>`.

Commands:
  dashboard   Launch the web UI (default when no command given)
  scan        Rescan all paths and print summary
  today       Show today's token usage and cost
  stats       Print statistical metrics across all sessions
  tips        Print optimization tips
  migrate     Merge/relocate Claude history + configs across machines

Global options:
  --path PATH       Add an extra scan path (repeatable, appended to config paths)
  --port PORT       Override port (default: 4242)
  --host HOST       Bind host (default: 127.0.0.1)
  --expose          Bind to 0.0.0.0 with a security warning
  --no-open         Don't open browser on startup
  --version         Print version and exit
  --enable-edit     Enable message editing and deletion
  --force-refresh   Clear mtime cache and force full rescan
  --since YYYYMMDD  Only include sessions on or after this date
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import threading
import time
import webbrowser
from datetime import date, datetime
from pathlib import Path

import uvicorn

from . import __version__, migrate
from .config import get_db_path, load_config
from .server import create_app


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ring",
        description="Local web viewer and analytics for AI coding assistant chat history",
    )
    parser.add_argument(
        "--path",
        action="append",
        dest="paths",
        metavar="PATH",
        help="Add a scan path (repeatable)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Override the listening port (default from config, fallback 4242)",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Bind host (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--expose",
        action="store_true",
        default=False,
        help="Bind to 0.0.0.0 (accessible on your local network). "
        "Use with caution — anyone on the same network can view your data.",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Don't open a browser window on startup",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"agent-decoder {__version__}",
    )
    parser.add_argument(
        "--enable-edit",
        action="store_true",
        default=False,
        help="Enable message editing and deletion. A backup is created before every write.",
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        default=False,
        help="Clear mtime cache and force a full rescan of all session files.",
    )
    parser.add_argument(
        "--since",
        type=lambda s: datetime.strptime(s, "%Y%m%d").date(),
        metavar="YYYYMMDD",
        help="Only include sessions on or after this date",
    )
    parser.add_argument(
        "--log-level",
        choices=("debug", "info", "warning", "error"),
        default="info",
        help="Verbosity for agent_decoder loggers (independent of uvicorn). "
        "Default 'info' surfaces operational signals (PTY runner warnings, "
        "/btw finalize/timeout, ephemeral persistence). Use 'debug' when "
        "diagnosing a live issue.",
    )
    parser.add_argument(
        "--demo",
        nargs="?",
        const="__AUTO__",
        default=None,
        metavar="PATH",
        help="Launch in isolated demo mode. Ignores your real config and cache; "
        "scans only the given demo dir (defaults to ./demo-data). Useful for "
        "screen recordings.",
    )

    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser(
        "dashboard",
        help="Launch the web UI (default)",
    )
    subparsers.add_parser(
        "scan",
        help="Rescan all paths and print summary",
    )
    subparsers.add_parser(
        "today",
        help="Show today's token usage and cost",
    )
    subparsers.add_parser(
        "stats",
        help="Print statistical metrics across all sessions",
    )
    subparsers.add_parser(
        "tips",
        help="Print optimization tips",
    )
    migrate_parser = subparsers.add_parser(
        "migrate",
        help="Merge/relocate Claude chat history + configs across machines",
        description=(
            "Merge one or more Claude config trees into a destination ~/.claude, "
            "rewriting a project-path prefix so chats stay viewable AND resumable. "
            "Dry-run by default; --apply is gated behind --i-have-a-backup."
        ),
    )
    migrate.add_arguments(migrate_parser)

    return parser


def _resolve_host(args: argparse.Namespace, config=None) -> str:
    """Determine the bind host: CLI flags > saved config > default."""
    if args.expose:
        print(
            "WARNING: --expose binds to 0.0.0.0. "
            "Anyone on your local network can access your chat history.\n"
        )
        return "0.0.0.0"
    if args.host:
        return args.host
    if config is not None:
        return config.host
    return "127.0.0.1"


def _configure_agent_decoder_logging(level_name: str) -> None:
    """Attach an explicit stderr handler to the ``agent_decoder`` logger.

    Uvicorn's default ``log_config`` reconfigures Python logging when
    ``uvicorn.run`` is invoked, which drops the implicit ``lastResort``
    handler that would otherwise route our ``_log.warning(...)`` /
    ``_log.info(...)`` calls to stderr.  Without this, every PTY runner
    signal (e.g. ``_finalize_btw_capture``, ``_btw_stuck_timeout``,
    ``record_ephemeral_*`` failures) goes nowhere — verified during the
    Phase 2 live smoke when 220 s of polling yielded zero log lines.

    ``propagate=False`` keeps uvicorn's root handlers from emitting a
    second copy of every line.
    """
    import logging as _logging

    # When stderr is redirected to a file (e.g. ``nohup ... > /tmp/log 2>&1``)
    # Python defaults to block-buffering, so log lines accumulate in-memory
    # until shutdown.  This was the second half of the Phase 2 smoke
    # invisibility: even after we configured the handler, lines never reached
    # the file until the process exited.  Switch to line buffering so each
    # ``\n`` flushes the underlying fd.
    try:
        sys.stderr.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    except Exception:
        pass
    level = getattr(_logging, level_name.upper(), _logging.INFO)
    logger = _logging.getLogger("agent_decoder")
    logger.setLevel(level)
    if not any(
        isinstance(h, _logging.StreamHandler)
        and getattr(h, "_agent_decoder_attached", False)
        for h in logger.handlers
    ):
        handler = _logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            _logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
        )
        handler._agent_decoder_attached = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
    logger.propagate = False


def _run_dashboard(args: argparse.Namespace, config) -> None:
    """Launch the web UI with uvicorn."""
    _configure_agent_decoder_logging(args.log_level)
    import logging as _logging

    _logging.getLogger("agent_decoder").info(
        "logger configured (level=%s)", args.log_level
    )
    host = _resolve_host(args, config)

    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    if args.force_refresh:
        asyncio.run(_force_refresh(db_path))

    app = create_app(config, db_path)

    if not args.no_open and config.auto_open_browser:
        url = f"http://{host}:{config.port}"

        def _open_browser() -> None:
            import urllib.request

            for _ in range(30):
                time.sleep(0.5)
                try:
                    urllib.request.urlopen(f"{url}/api/health", timeout=1)
                    break
                except Exception:
                    continue
            webbrowser.open(url)

        threading.Thread(target=_open_browser, daemon=True).start()

    print(f"Agent Decoder running at http://{host}:{config.port}", flush=True)
    uvicorn.run(app, host=host, port=config.port, log_level="warning")


async def _force_refresh(db_path: Path) -> None:
    """Clear all stored mtimes so the next scan re-parses every file."""
    from .db import Database

    async with Database(db_path) as db:
        await db.init_schema()
        await db.execute("UPDATE sessions SET file_mtime = NULL")
        await db.commit()


async def _do_scan(db_path: Path, config) -> int:
    """Scan all paths, return number of sessions indexed."""
    from .db import Database
    from .providers import register_builtins, registry

    register_builtins()

    count = 0
    async with Database(db_path) as db:
        await db.init_schema()
        for adapter in registry.all_adapters():
            roots = adapter.configured_roots(config)
            async for project, session_path in adapter.discover(roots):
                try:
                    session, messages = adapter.parse(session_path)
                    session.project_id = project.id
                    project.session_count += 1
                    current_mtime = session_path.stat().st_mtime
                    await db.upsert_project(project)
                    await db.upsert_session(session, file_mtime=current_mtime)
                    await db.upsert_messages(messages)
                    count += 1
                except Exception as exc:
                    print(f"  Warning: skipping {session_path}: {exc}")
    return count


def _run_scan(args: argparse.Namespace, config) -> None:
    """Rescan and print summary."""
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    if args.force_refresh:
        asyncio.run(_force_refresh(db_path))

    print("Scanning...")
    count = asyncio.run(_do_scan(db_path, config))
    print(f"Indexed {count} sessions.")


def _run_today(args: argparse.Namespace, config) -> None:
    """Show today's token usage and cost."""
    from .db import Database
    from .analytics.service import TokenAnalyticsService
    from .analytics.cost import CostEngine
    from .analytics.pricing import CachedPricingStrategy

    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    if args.force_refresh:
        asyncio.run(_force_refresh(db_path))
        asyncio.run(_do_scan(db_path, config))

    async def _query():
        async with Database(db_path) as db:
            await db.init_schema()
            messages = await db.get_all_messages()

        today = args.since or date.today()
        svc = TokenAnalyticsService()
        buckets = svc.daily_buckets(messages)
        target = next((b for b in buckets if b.day >= today), None)
        if target is None:
            return None
        return target

    result = asyncio.run(_query())
    if result is None:
        label = args.since.isoformat() if args.since else "today"
        print(f"No usage data for {label}.")
        return

    bd = result.breakdown
    print(f"Date: {result.day}")
    print(f"  Input tokens:       {bd.input_tokens:,}")
    print(f"  Output tokens:      {bd.output_tokens:,}")
    print(f"  Cache creation:     {bd.cache_creation_tokens:,}")
    print(f"  Cache read:         {bd.cache_read_tokens:,}")
    print(f"  Total tokens:       {bd.total:,}")
    print(f"  Prompt count:       {result.prompt_count}")

    # Cost estimate
    pricing = CachedPricingStrategy()
    asyncio.run(pricing.refresh())
    engine = CostEngine(pricing)
    cost = engine.compute("claude-sonnet-4-6", bd)
    print(f"  Est. cost (Sonnet): ${float(cost.total_usd):.4f}")


def _run_stats(args: argparse.Namespace, config) -> None:
    """Print statistical metrics."""
    from .db import Database
    from .analytics.stats import PromptStatsScanner, ModelUsageScanner, ToolUsageScanner

    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    if args.force_refresh:
        asyncio.run(_force_refresh(db_path))
        asyncio.run(_do_scan(db_path, config))

    async def _query():
        async with Database(db_path) as db:
            await db.init_schema()
            messages = await db.get_all_messages()
        return messages

    messages = asyncio.run(_query())

    stats = PromptStatsScanner().scan(messages)
    print("=== Prompt Stats ===")
    for key, val in sorted(stats.items()):
        if isinstance(val, float):
            print(f"  {key}: {val:.2f}")
        else:
            print(f"  {key}: {val}")

    models = ModelUsageScanner().scan(messages)
    print("\n=== Model Usage ===")
    for m in models:
        print(
            f"  {m['model']}: {m['message_count']} messages, "
            f"{m['input_tokens']:,} input, {m['output_tokens']:,} output"
        )

    tools = ToolUsageScanner().scan(messages)
    print("\n=== Tool Usage ===")
    for t in tools[:10]:
        print(f"  {t['tool']}: {t['count']} calls")


def _run_tips(args: argparse.Namespace, config) -> None:
    """Print optimization tips."""
    from .db import Database
    from .analytics.tips import (
        TipRegistry,
        RepeatedFileReadRule,
        OversizedToolResultRule,
        LowCacheHitRule,
    )

    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    if args.force_refresh:
        asyncio.run(_force_refresh(db_path))
        asyncio.run(_do_scan(db_path, config))

    async def _query():
        async with Database(db_path) as db:
            await db.init_schema()
            messages = await db.get_all_messages()
        return messages

    messages = asyncio.run(_query())

    registry = TipRegistry()
    registry.register(RepeatedFileReadRule())
    registry.register(OversizedToolResultRule())
    registry.register(LowCacheHitRule())
    tips = registry.run(messages)

    if not tips:
        print("No optimization tips — looking good!")
        return

    for t in tips:
        icon = {"info": "ℹ", "warning": "⚠", "critical": "✱"}.get(t.severity, "•")
        print(f"{icon} [{t.severity.upper()}] {t.title}")
        if t.detail:
            print(f"    {t.detail}")


def _apply_demo_mode(args: argparse.Namespace) -> None:
    """Isolate config + cache + scan path so a demo launch can't pick up real state.

    Must run before ``load_config`` and before any ``get_db_path``/``get_config_path``
    call — those functions read ``XDG_CONFIG_HOME`` and ``XDG_CACHE_HOME`` at call
    time, so overriding the env vars here is enough to redirect every consumer
    (load_config, save_config, get_db_path) for the lifetime of the process.
    """
    if args.demo == "__AUTO__":
        demo_path = Path.cwd() / "demo-data"
    else:
        demo_path = Path(args.demo).expanduser().resolve()

    if not demo_path.exists() or not (demo_path / "projects").is_dir():
        print(
            f"--demo: '{demo_path}' is not a valid demo dir. "
            "Expected a directory containing a 'projects/' subdirectory.",
            file=sys.stderr,
        )
        sys.exit(2)

    runtime = demo_path / ".runtime"
    os.environ["XDG_CONFIG_HOME"] = str(runtime / "config")
    os.environ["XDG_CACHE_HOME"] = str(runtime / "cache")
    args.paths = [str(demo_path)]

    print(f"Demo mode: scanning {demo_path} (runtime state at {runtime})")


def main() -> None:
    """Entry point registered in pyproject.toml [project.scripts]."""
    parser = _build_parser()
    args = parser.parse_args()

    if args.demo is not None:
        _apply_demo_mode(args)

    config = load_config(extra_paths=args.paths, port=args.port)

    if args.demo is not None:
        # Replace (not append) so neither the default '~/.claude' fallback nor
        # any leftover profile entries can leak real session data into the demo.
        config = config.model_copy(
            update={
                "data_paths": args.paths,
                "profiles": [],
                "active_profile_id": None,
            }
        )

    if args.enable_edit:
        config = config.model_copy(update={"edit_enabled": True})

    command = args.command or "dashboard"
    dispatch = {
        "dashboard": _run_dashboard,
        "scan": _run_scan,
        "today": _run_today,
        "stats": _run_stats,
        "tips": _run_tips,
        "migrate": _run_migrate,
    }
    dispatch[command](args, config)


def _run_migrate(args, config) -> None:
    """``ring migrate`` -- filesystem merge tool; ignores app config."""
    raise SystemExit(migrate.run(args))
