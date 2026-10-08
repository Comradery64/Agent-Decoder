"""File watcher — detects new/changed JSONL files and notifies the server via a queue.

Contract (for Agent 2 to implement):
  watch_paths(root_paths: list[Path], queue: asyncio.Queue) -> None (async, runs forever)
    - Use watchfiles.awatch to watch all root_paths recursively
    - For each *.jsonl change, put a WatchEvent onto the queue
    - Filter to only *.jsonl file changes (ignore everything else)

  WatchEvent dataclass:
    kind: Literal["created", "modified", "deleted"]
    path: Path

  The SSE endpoint in server.py consumes the queue and forwards events to clients.

SOLID notes:
  - No direct knowledge of DB or parser — pure event emission
  - Callers decide what to do with the events
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from watchfiles import Change, awatch


@dataclass
class WatchEvent:
    kind: Literal["created", "modified", "deleted"]
    path: Path


async def watch_paths(root_paths: list[Path], queue: asyncio.Queue) -> None:
    """Watch all root_paths and emit WatchEvents for *.jsonl changes.

    Runs indefinitely until cancelled.  Filters filesystem events to only
    ``*.jsonl`` files and maps ``watchfiles.Change`` values to human-readable
    ``WatchEvent.kind`` strings before putting them on the queue.

    Args:
        root_paths: Directories to watch recursively.  Non-existent paths are
                    silently skipped.
        queue:      Asyncio queue that the server's SSE endpoint drains.
    """
    existing = [str(p) for p in root_paths if p.exists()]
    if not existing:
        return

    _kind_map: dict[Change, Literal["created", "modified", "deleted"]] = {
        Change.added: "created",
        Change.modified: "modified",
        Change.deleted: "deleted",
    }

    async for changes in awatch(*existing, debounce=50):
        for change_type, path_str in changes:
            if not path_str.endswith(".jsonl"):
                continue
            kind = _kind_map.get(change_type, "modified")
            await queue.put(WatchEvent(kind=kind, path=Path(path_str)))
