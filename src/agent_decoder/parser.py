"""JSONL parser — reads a session file and returns structured messages.

Contract (for Agent 1 to implement):
  parse_session(path: Path) -> tuple[Session, list[Message]]
    - Read every line of the JSONL file
    - Build Session metadata from non-message records (custom-title, permission-mode, etc.)
    - Build Message objects from user/assistant records
    - Return (session, messages) — messages are flat, NOT yet threaded

  build_message_tree(messages: list[Message]) -> list[MessageTree]
    - Walk parentUuid chains to build parent→child tree
    - Sidechain messages (isSidechain=True) are children of their parent
    - Root messages (parentId=None) are top-level entries
    - Preserves chronological order within siblings

SOLID notes:
  - Single Responsibility: this module only parses; it does not write to DB
  - Open/Closed: add new record type handlers without changing existing ones
  - The _RECORD_HANDLERS dict is the extension point
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import (
    ContentBlock,
    ImageBlock,
    Message,
    MessageTree,
    Session,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    TokenUsage,
)

# ---------------------------------------------------------------------------
# UUID validation pattern
# ---------------------------------------------------------------------------

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


# ---------------------------------------------------------------------------
# Shared title inference helper
# ---------------------------------------------------------------------------


def _infer_title_from_messages(messages: list[Message]) -> str | None:
    """Infer a session title from the first non-meta user TextBlock.

    Strips system XML tags, takes the first non-empty line, returns it or None
    if no suitable text is found.  Used by both the Claude parser and the
    CodexAdapter so title logic stays in one place.

    The tag-name class allows underscores (``[a-z0-9_-]``) so wrapper tags like
    Codex's ``<environment_context>…</environment_context>`` — which is the
    entire first user message in real Codex rollouts — get stripped; otherwise
    every Codex session's title would be the literal ``<environment_context>``.
    """
    for m in messages:
        if m.role == "user" and not m.is_meta:
            for block in m.content_blocks:
                if isinstance(block, TextBlock) and block.text.strip():
                    text = re.sub(
                        r"<[a-z][a-z0-9_-]*>[\s\S]*?</[a-z][a-z0-9_-]*>",
                        "",
                        block.text,
                    ).strip()
                    text = text.splitlines()[0].strip() if text else ""
                    if text:
                        return text
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_session(path: Path) -> tuple[Session, list[Message]]:
    """Parse a JSONL session file into a Session + flat message list.

    Session id resolution order:
      1. If the filename stem is a valid UUID, use it.
      2. Otherwise scan the file content for a sessionId field.
         Raise ValueError if no valid UUID can be found.

    Raises:
        FileNotFoundError: if path does not exist
        ValueError: if no valid UUID session ID is found
    """
    if not path.exists():
        raise FileNotFoundError(f"Session file not found: {path}")

    # Derive session id: prefer filename UUID, fall back to content
    try:
        session_id = _derive_session_id(path)
    except ValueError:
        session_id = _session_id_from_content(path)

    project_id = _unmangle_project_id(path.parent.name)

    session = Session(
        id=session_id,
        project_id=project_id,
        file_path=str(path),
    )

    messages: list[Message] = []
    cwd_set = False
    git_branch_set = False
    model_set = False

    with path.open(encoding="utf-8") as fh:
        for raw_line in fh:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError:
                continue

            record_type = record.get("type")

            # --- metadata records ---
            if record_type == "permission-mode":
                session.permission_mode = record.get("permissionMode")
                continue

            if record_type == "custom-title":
                session.title = record.get("customTitle")
                continue

            if record_type == "worktree-state":
                session.is_worktree = True
                continue

            if record_type in ("clau-decode-fork", "clau-decode-backup"):
                session.is_fork = True
                continue

            # --- mid-stream queued commands ---
            # When the user types a message while the assistant is working, the session
            # stores it as an "attachment" record with type "queued_command".
            # We surface it as a regular user message so it appears in the thread.
            if record_type == "attachment":
                attachment = record.get("attachment", {})
                if attachment.get("type") == "queued_command":
                    prompt = attachment.get("prompt", "")
                    # prompt is either a plain string or a list of content blocks
                    if isinstance(prompt, str):
                        blocks_qa = [TextBlock(text=prompt)] if prompt else []
                    elif isinstance(prompt, list):
                        blocks_qa = _parse_content_blocks(prompt)
                    else:
                        blocks_qa = []
                    if blocks_qa:
                        uuid = record.get("uuid", "")
                        parent_uuid = record.get("parentUuid")
                        timestamp_str = record.get("timestamp")
                        timestamp_qa: datetime | None = None
                        if timestamp_str:
                            try:
                                ts = timestamp_str
                                if ts.endswith("Z"):
                                    ts = ts[:-1] + "+00:00"
                                timestamp_qa = datetime.fromisoformat(ts)
                            except ValueError:
                                pass
                        msg = Message(
                            id=uuid,
                            session_id=session_id,
                            parent_id=parent_uuid,
                            role="user",
                            content_blocks=blocks_qa,
                            timestamp=timestamp_qa,
                            is_sidechain=bool(record.get("isSidechain", False)),
                            is_meta=False,
                            cwd=record.get("cwd"),
                            git_branch=record.get("gitBranch"),
                        )
                        messages.append(msg)
                        if timestamp_qa:
                            if (
                                session.started_at is None
                                or timestamp_qa < session.started_at
                            ):
                                session.started_at = timestamp_qa
                            if (
                                session.updated_at is None
                                or timestamp_qa > session.updated_at
                            ):
                                session.updated_at = timestamp_qa
                continue

            # --- message records ---
            if record_type not in ("user", "assistant"):
                continue

            is_meta = bool(record.get("isMeta", False))
            is_sidechain = bool(record.get("isSidechain", False))
            uuid = record.get("uuid", "")
            parent_uuid = record.get("parentUuid")
            timestamp_str = record.get("timestamp")
            timestamp: datetime | None = None
            if timestamp_str:
                # Handle trailing Z or offset
                try:
                    ts = timestamp_str
                    if ts.endswith("Z"):
                        ts = ts[:-1] + "+00:00"
                    timestamp = datetime.fromisoformat(ts)
                except ValueError:
                    pass

            msg_cwd = record.get("cwd")
            msg_git_branch = record.get("gitBranch")
            request_id = record.get("requestId")
            source_tool_uuid = record.get("sourceToolAssistantUUID")

            # Collect cwd / git_branch from first record that has them
            if not cwd_set and msg_cwd:
                session.cwd = msg_cwd
                cwd_set = True
            if not git_branch_set and msg_git_branch:
                session.git_branch = msg_git_branch
                git_branch_set = True

            # Parse content
            if record_type == "assistant":
                msg_record = record.get("message", {})
                raw_content = msg_record.get("content", [])
                msg_model = msg_record.get("model")
                if not model_set and msg_model:
                    session.model = msg_model
                    model_set = True
                content_blocks = _parse_content_blocks(raw_content)
                raw_usage = msg_record.get("usage")
                msg_usage: TokenUsage | None = None
                if isinstance(raw_usage, dict):
                    msg_usage = TokenUsage(
                        input_tokens=raw_usage.get("input_tokens", 0),
                        output_tokens=raw_usage.get("output_tokens", 0),
                        cache_creation_input_tokens=raw_usage.get(
                            "cache_creation_input_tokens", 0
                        ),
                        cache_read_input_tokens=raw_usage.get(
                            "cache_read_input_tokens", 0
                        ),
                    )
                msg = Message(
                    id=uuid,
                    session_id=session_id,
                    parent_id=parent_uuid,
                    role="assistant",
                    content_blocks=content_blocks,
                    timestamp=timestamp,
                    model=msg_model,
                    is_sidechain=is_sidechain,
                    is_meta=is_meta,
                    cwd=msg_cwd,
                    git_branch=msg_git_branch,
                    request_id=request_id,
                    source_tool_assistant_uuid=source_tool_uuid,
                    usage=msg_usage,
                )
            else:
                # user record
                msg_record = record.get("message", {})
                raw_content = msg_record.get("content", [])
                content_blocks = _parse_content_blocks(raw_content)
                msg = Message(
                    id=uuid,
                    session_id=session_id,
                    parent_id=parent_uuid,
                    role="user",
                    content_blocks=content_blocks,
                    timestamp=timestamp,
                    is_sidechain=is_sidechain,
                    is_meta=is_meta,
                    cwd=msg_cwd,
                    git_branch=msg_git_branch,
                    request_id=request_id,
                    source_tool_assistant_uuid=source_tool_uuid,
                )

            messages.append(msg)

            # Track session timestamps
            if timestamp:
                if session.started_at is None or timestamp < session.started_at:
                    session.started_at = timestamp
                if session.updated_at is None or timestamp > session.updated_at:
                    session.updated_at = timestamp

    session.message_count = len(messages)
    session.user_message_count = sum(
        1 for m in messages if m.role == "user" and not m.is_meta
    )

    # Infer title from first user message when no custom-title record exists
    if session.title is None:
        session.title = _infer_title_from_messages(messages)

    return session, messages


def build_message_tree(messages: list[Message]) -> list[MessageTree]:
    """Convert a flat message list into a parent→child tree.

    Root messages (parent_id=None, is_sidechain=False) are returned as the
    top-level list.  Sidechain branches hang off their parent as children.
    """
    # Build node map
    nodes: dict[str, MessageTree] = {m.id: MessageTree(message=m) for m in messages}

    roots: list[MessageTree] = []

    for m in messages:
        node = nodes[m.id]

        if m.is_sidechain:
            # Sidechain: always attach to parent
            if m.parent_id and m.parent_id in nodes:
                nodes[m.parent_id].children.append(node)
            # Fallback for merged sub-agent transcripts (include_subagent_chats):
            # the local-root message of an ingested agent-*.jsonl file has a
            # parent_id that only exists in the PARENT session's own file, so
            # it never resolves against this node map. source_tool_assistant_uuid
            # (set at ingest time from the sub-agent's meta.json toolUseId — see
            # server.py's _ingest_subagent_file) points at the parent assistant
            # message instead; attach there if it resolves.
            elif m.source_tool_assistant_uuid and m.source_tool_assistant_uuid in nodes:
                nodes[m.source_tool_assistant_uuid].children.append(node)
            # Neither resolved: orphan root (edge case)
            else:
                roots.append(node)
        elif m.parent_id is None:
            # True root: no parent, not sidechain
            roots.append(node)
        else:
            # Normal child message with a parent
            if m.parent_id in nodes:
                nodes[m.parent_id].children.append(node)
            else:
                # Orphaned non-root: add to roots as fallback
                roots.append(node)

    # Sort children by timestamp within each node
    def _sort_node(node: MessageTree) -> None:
        node.children.sort(
            key=lambda n: n.message.timestamp or datetime.min.replace(tzinfo=None)
        )
        for child in node.children:
            _sort_node(child)

    for root in roots:
        _sort_node(root)

    # Sort roots by timestamp
    roots.sort(key=lambda n: n.message.timestamp or datetime.min.replace(tzinfo=None))

    return roots


# ---------------------------------------------------------------------------
# Internal helpers — implement these, tests cover them individually
# ---------------------------------------------------------------------------


def _session_id_from_content(path: Path) -> str:
    """Scan the first few lines of a JSONL file for a valid UUID sessionId.

    Raises ValueError if none is found.
    """
    with path.open(encoding="utf-8") as fh:
        for raw_line in fh:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            session_id = record.get("sessionId", "")
            if session_id and _UUID_RE.match(str(session_id)):
                return str(session_id)
    raise ValueError(
        f"No valid UUID sessionId found in '{path}'. "
        "This does not appear to be a valid session file."
    )


def _parse_content_blocks(raw_content: Any) -> list[ContentBlock]:
    """Convert raw message.content (str or list) into typed ContentBlock list."""
    if isinstance(raw_content, str):
        return [TextBlock(text=raw_content)]

    if not isinstance(raw_content, list):
        return []

    blocks: list[ContentBlock] = []
    for item in raw_content:
        if not isinstance(item, dict):
            continue
        block_type = item.get("type")
        if block_type == "text":
            blocks.append(TextBlock(text=item.get("text", "")))
        elif block_type == "thinking":
            blocks.append(
                ThinkingBlock(
                    thinking=item.get("thinking", ""),
                    signature=item.get("signature"),
                )
            )
        elif block_type == "tool_use":
            blocks.append(
                ToolUseBlock(
                    id=item.get("id", ""),
                    name=item.get("name", ""),
                    input=item.get("input", {}),
                )
            )
        elif block_type == "tool_result":
            blocks.append(
                ToolResultBlock(
                    tool_use_id=item.get("tool_use_id", ""),
                    content=item.get("content"),
                    is_error=item.get("is_error", False),
                )
            )
        elif block_type == "image":
            blocks.append(ImageBlock(source=item.get("source", {})))
        # Unknown types are silently skipped

    return blocks


def _derive_session_id(path: Path) -> str:
    """Extract UUID string from filename, raise ValueError if not valid."""
    stem = path.stem  # filename without extension
    if not _UUID_RE.match(stem):
        raise ValueError(
            f"Filename '{path.name}' is not a valid UUID session file. "
            f"Expected pattern: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx.jsonl"
        )
    return stem


def _unmangle_project_id(directory_name: str) -> str:
    """Convert directory name like '-Volumes-ExternalDrive-Work-foo' → 'Volumes/ExternalDrive/Work/foo'.

    Rules:
      - Strip the leading '-'
      - Double hyphens '--' become a literal '-' in the path
      - Single hyphens '-' become '/'
    """
    if directory_name.startswith("-"):
        directory_name = directory_name[1:]

    # Replace double hyphens with a placeholder, convert single hyphens to /,
    # then restore the placeholder as literal hyphens.
    _PLACEHOLDER = "\x00"
    result = directory_name.replace("--", _PLACEHOLDER)
    result = result.replace("-", "/")
    result = result.replace(_PLACEHOLDER, "-")
    return result
