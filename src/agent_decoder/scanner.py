"""File system scanner — discovers JSONL session files under configured root paths.

Contract (for Agent 2 to implement):
  scan_paths(root_paths: list[Path]) -> AsyncIterator[tuple[Project, Path]]
    - For each root path, walk projects/ subdirectory
    - Each immediate child of projects/ is a project directory (mangled path name)
    - Each *.jsonl file inside a project dir is a session
    - Yield (project, session_file_path) tuples
    - Also yield sessions from root_path/history.jsonl as a special "_history" project

  build_project_from_dir(dir_name: str, data_source: str) -> Project
    - Convert the mangled directory name into a Project object
    - Use _unmangle_project_id from parser to get a display name

  resolve_path(mangled: str) -> str | None
    - Try to resolve the mangled path back to a real filesystem path
    - Return None if the path doesn't exist

SOLID notes:
  - Single Responsibility: scanning only; no parsing, no DB writes
  - Callers (server.py) compose scanner + parser + db; this module doesn't know about them
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import AsyncIterator

from .models import Project
from .parser import _unmangle_project_id


def resolve_path(mangled: str) -> str | None:
    """Attempt to reconstruct and verify the original filesystem path.

    Strips the leading dash then converts remaining dashes to slashes,
    treating double-dashes as literal hyphens (matching _unmangle_project_id).

    Args:
        mangled: A directory name like '-Volumes-ExternalDrive-Work-foo' or
                 '-Users-alice-my--project'.

    Returns:
        The absolute path string if it exists on disk, otherwise None.
    """
    unmangled = _unmangle_project_id(mangled)
    path = Path("/" + unmangled)
    if path.exists():
        return str(path)
    return None


def build_project_from_dir(dir_name: str, data_source: str) -> Project:
    """Create a Project from a mangled project directory name.

    Args:
        dir_name:    The raw directory name as it appears on disk
                     (e.g. '-Users-alice-project-foo').
        data_source: The configured root path this project was found under
                     (e.g. '~/.claude').

    Returns:
        A Project with a stable 16-hex-char id, a human-readable display_name
        showing the last two path components, and a resolved_path if the
        underlying directory exists.
    """
    project_id = hashlib.sha256(f"{dir_name}\0{data_source}".encode()).hexdigest()[:16]

    unmangled = _unmangle_project_id(dir_name)
    parts = [p for p in unmangled.split("/") if p]
    if len(parts) >= 2:
        display_name = "/".join(parts[-2:])
    elif parts:
        display_name = parts[0]
    else:
        display_name = dir_name

    resolved = resolve_path(dir_name)

    return Project(
        id=project_id,
        display_name=display_name,
        raw_path=dir_name,
        resolved_path=resolved,
        data_source=data_source,
        session_count=0,
    )


async def scan_paths(
    root_paths: list[Path], include_subagents: bool = False
) -> AsyncIterator[tuple[Project, Path]]:
    """Yield (project, session_path) for every session file found under root_paths.

    For each root path:
      - Skips paths that don't exist on disk.
      - Looks for a ``projects/`` subdirectory.
      - Iterates over every immediate child directory inside ``projects/``.
      - Within each project directory, yields one tuple per ``*.jsonl`` file.
      - If ``include_subagents`` is set, also yields sub-agent (Task tool)
        transcripts nested under ``<session>/subagents/*.jsonl`` — these are
        skipped by default since the top-level glob is intentionally
        non-recursive.

    This is an async generator so it can be consumed with ``async for``.

    Args:
        root_paths: List of root directories to scan (e.g. ``[Path("~/.claude")]``).
        include_subagents: Opt-in second pass that also yields sub-agent
            transcript files (see ``AppConfig.include_subagent_chats``).

    Yields:
        Tuples of (Project, Path) where Path points to a ``.jsonl`` session file.
    """
    for root_path in root_paths:
        if not root_path.exists():
            continue

        projects_dir = root_path / "projects"
        if not projects_dir.is_dir():
            continue

        for project_dir in sorted(projects_dir.iterdir()):
            if not project_dir.is_dir():
                continue

            project = build_project_from_dir(project_dir.name, str(root_path))

            for jsonl_file in sorted(
                f for f in project_dir.glob("*.jsonl") if ".bak." not in f.name
            ):
                yield project, jsonl_file

            if include_subagents:
                for jsonl_file in sorted(project_dir.glob("*/subagents/*.jsonl")):
                    if ".bak." in jsonl_file.name:
                        continue
                    yield project, jsonl_file
