"""Session file editor — swap-based JSONL mutations.

The edit strategy:
  1. Read the original file.
  2. Write a backup copy at ``<stem>.bak.<ts>.jsonl`` that gets its own new UUID
     as ``sessionId`` in content — so it shows up as a distinct session in the
     sidebar labelled "[original]".
  3. Write the edited content back to the **same path** as the original, keeping
     the original session UUID.  The session can resume as normal because it
     finds the expected file at the expected path.

The backup file includes a ``clau-decode-backup`` metadata record so clau-decode
can mark it ``is_fork = True`` (disabling "Open in Terminal" since its new UUID
is unknown to the CLI's internal index).

Legacy in-place helpers are kept for the test suite only.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid as _uuid_mod
from datetime import datetime, timezone
from pathlib import Path


def swap_session(
    original_path: Path,
    session_id: str,
    *,
    delete_uuid: str | None = None,
    edit_uuid: str | None = None,
    new_content: list[dict] | None = None,
) -> tuple[Path, str, Path, str]:
    """Edit a session by swapping its file content in-place.

    The original file is replaced with the edited version (keeping the same
    filename / session UUID so the session can still be resumed).  The original
    content is preserved as a timestamped backup with a fresh UUID.

    Returns:
        ``(edited_path, session_id, backup_path, backup_session_id)``

        * ``edited_path`` — same as ``original_path``; the live, resumable session.
        * ``session_id``  — unchanged original UUID.
        * ``backup_path`` — the ``.bak.`` file containing the full original.
        * ``backup_session_id`` — new UUID assigned to the backup session.
    """
    lines = _read_lines(original_path)

    # ------------------------------------------------------------------
    # 1.  Backup: original content with a new session UUID in metadata.
    # ------------------------------------------------------------------
    backup_id = str(_uuid_mod.uuid4())
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_path = original_path.with_name(f"{original_path.stem}.bak.{ts}.jsonl")

    backup_lines: list[str] = [
        json.dumps({"type": "clau-decode-backup", "originalSessionId": session_id})
    ]
    for line in lines:
        try:
            rec = json.loads(line)
            # Restamp sessionId references so the backup has its own unique ID.
            if "sessionId" in rec:
                rec["sessionId"] = backup_id
            # Prepend "[original] " to the title so the sidebar is readable.
            if rec.get("type") == "custom-title" and rec.get("customTitle"):
                rec["customTitle"] = f"[original] {rec['customTitle']}"
            backup_lines.append(json.dumps(rec, ensure_ascii=False))
        except json.JSONDecodeError:
            backup_lines.append(line)

    backup_path.write_text("\n".join(backup_lines) + "\n", encoding="utf-8")

    # ------------------------------------------------------------------
    # 2.  Edited file: written back to the original path.
    #     No UUID remapping — same message UUIDs, same session UUID.
    # ------------------------------------------------------------------
    edited_lines: list[str] = []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            edited_lines.append(line)
            continue

        old_uuid = record.get("uuid")

        if delete_uuid and old_uuid == delete_uuid:
            continue  # omit

        if edit_uuid and old_uuid == edit_uuid and new_content is not None:
            record.setdefault("message", {})["content"] = new_content

        edited_lines.append(json.dumps(record, ensure_ascii=False))

    original_path.write_text("\n".join(edited_lines) + "\n", encoding="utf-8")

    # Force-bump mtime to "now" so downstream consumers (watcher,
    # _scan_one's stored_mtime == current_mtime fast-path, and any
    # mtime-based dedupe) always observe the swap as a change, even on
    # filesystems with coarse mtime resolution where two writes in the
    # same second can collide. write_text already touches mtime on APFS,
    # but this guarantees the contract regardless of FS.
    os.utime(original_path, None)

    return original_path, session_id, backup_path, backup_id


# ---------------------------------------------------------------------------
# Legacy in-place helpers (kept for tests; not used by server routes)
# ---------------------------------------------------------------------------


def backup_session(path: Path) -> Path:
    """Copy path to <path>.bak.<YYYYMMDD_HHMMSS>[_N].jsonl and return the backup path."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup = path.with_name(f"{path.stem}.bak.{ts}.jsonl")
    n = 1
    while backup.exists():
        backup = path.with_name(f"{path.stem}.bak.{ts}_{n}.jsonl")
        n += 1
    shutil.copy2(path, backup)
    return backup


def delete_from_session(path: Path, message_uuid: str) -> None:
    """Remove the JSONL line whose uuid == message_uuid. Writes in-place atomically."""
    lines = _read_lines(path)
    kept = [line for line in lines if _get_uuid(line) != message_uuid]
    if len(kept) == len(lines):
        return
    _write_lines(path, kept)


def edit_content_in_session(
    path: Path, message_uuid: str, new_content: list[dict]
) -> None:
    """Replace message.content for the line matching message_uuid. Writes atomically."""
    lines = _read_lines(path)
    result = []
    changed = False
    for line in lines:
        if _get_uuid(line) == message_uuid:
            try:
                record = json.loads(line)
                record.setdefault("message", {})["content"] = new_content
                result.append(json.dumps(record, ensure_ascii=False))
                changed = True
                continue
            except (json.JSONDecodeError, KeyError):
                pass
        result.append(line)
    if changed:
        _write_lines(path, result)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _read_lines(path: Path) -> list[str]:
    return [
        line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def _write_lines(path: Path, lines: list[str]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(path)


def _get_uuid(line: str) -> str | None:
    try:
        return json.loads(line).get("uuid")
    except (json.JSONDecodeError, AttributeError):
        return None
