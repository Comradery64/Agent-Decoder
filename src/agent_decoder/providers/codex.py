"""OpenAI Codex CLI provider adapter (read-only, Phase 2).

Decodes Codex rollout JSONL files (rollout-*.jsonl) into clau-decode's
normalized domain model.  Interactive operations (send / resume / fork /
edit) are disabled in this phase; Phase 4 will flip can_send / can_resume.

Rollout file structure (one JSON object per line, each with
``{"timestamp", "type", "payload"}``):

  session_meta   — session id, cwd, git branch, originator
  turn_context   — model name (first one wins)
  response_item  — the canonical message content (type in {message,
                   reasoning, function_call, function_call_output})
  event_msg      — UI telemetry; only token_count + task_complete are
                   ingested; the rest are ignored
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any

from ..models import (
    AppConfig,
    Message,
    Project,
    Session,
    TextBlock,
    ThinkingBlock,
    TokenUsage,
    ToolResultBlock,
    ToolUseBlock,
)
from ..parser import _infer_title_from_messages
from .base import ProviderAdapter, ProviderCaps

# Placeholder text emitted for encrypted reasoning blocks.
# The real encrypted_content is NEVER read or stored.
_REASONING_PLACEHOLDER = "🔒 Reasoning (encrypted)"

# Codex injects environment/state context as ``user``-role messages whose text
# is an XML wrapper (no human prose) — the actual user prompt arrives in a
# later user message. We flag these as ``is_meta`` so they're excluded from
# title inference and the user-message count, mirroring how the Claude parser
# marks injected context. Detection: the message text begins with one of these
# wrapper tags. ``<turn_aborted>`` is Codex's interruption note (also meta).
_CODEX_META_WRAPPERS = (
    "<environment_context",
    "<workspace_roots",
    "<permission_profile",
    "<user_instructions",
    "<turn_aborted",
)


def _is_injected_context(blocks: list[TextBlock]) -> bool:
    """True if a user message is Codex-injected context rather than a prompt."""
    text = "".join(b.text for b in blocks).lstrip()
    return text.startswith(_CODEX_META_WRAPPERS)


# ---------------------------------------------------------------------------
# Project helper
# ---------------------------------------------------------------------------


def _codex_project(cwd: str | None, data_source: str) -> Project:
    """Build a Project from a Codex session's working directory.

    Args:
        cwd:         The ``cwd`` field from ``session_meta.payload``, or
                     ``None`` if the field was absent.
        data_source: The configured root path this session was found under.

    Returns:
        A Project with a stable 16-hex-char id and human-readable display_name.
    """
    if cwd:
        project_id = hashlib.sha256(
            f"{cwd}\0{data_source}\0codex".encode()
        ).hexdigest()[:16]
        parts = [p for p in cwd.split("/") if p]
        if len(parts) >= 2:
            display_name = "/".join(parts[-2:])
        elif parts:
            display_name = parts[0]
        else:
            display_name = cwd
        resolved: str | None = cwd if Path(cwd).exists() else None
        return Project(
            id=project_id,
            display_name=display_name,
            raw_path=cwd,
            resolved_path=resolved,
            data_source=data_source,
        )
    else:
        sentinel = "codex/(no project)"
        project_id = hashlib.sha256(
            f"{sentinel}\0{data_source}\0codex".encode()
        ).hexdigest()[:16]
        return Project(
            id=project_id,
            display_name=sentinel,
            raw_path=sentinel,
            resolved_path=None,
            data_source=data_source,
        )


# ---------------------------------------------------------------------------
# Timestamp helper
# ---------------------------------------------------------------------------


def _parse_ts(ts: str) -> datetime:
    """Parse an ISO-8601 timestamp string, handling trailing ``Z``."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class CodexAdapter(ProviderAdapter):
    """Adapter for OpenAI Codex CLI rollout JSONL files (read-only).

    Session layout::

        <root>/**/rollout-<id>.jsonl

    All four interactive operations are disabled (read-only Phase 2).
    """

    name = "codex"

    # -- Capability declaration -----------------------------------------------

    @property
    def capabilities(self) -> ProviderCaps:
        # Phase 4e: Codex is drivable (send + resume) via the tmux ProviderDriver.
        # These are STATIC caps; the server reconciles them with runtime
        # availability (``_effective_caps``) so a box without tmux/codex still
        # degrades to read-only. fork/edit stay False — no flow exists yet and
        # the Claude-JSONL editor would corrupt a Codex rollout.
        return ProviderCaps(
            can_send=True, can_resume=True, can_fork=False, can_edit=False
        )

    # -- Config-aware root resolution -----------------------------------------

    def configured_roots(self, config: AppConfig) -> list[Path]:
        """Return the expanded root directories configured for Codex sessions."""
        return [Path(p).expanduser() for p in config.codex_data_paths]

    # -- Path ownership -------------------------------------------------------

    def owns_path(self, path: Path) -> bool:
        """Return True for ``rollout-*.jsonl`` files."""
        return path.suffix == ".jsonl" and path.name.startswith("rollout-")

    # -- Discovery (async generator) ------------------------------------------

    async def discover(
        self, roots: list[Path], include_subagents: bool = False
    ) -> AsyncIterator[tuple[Project, Path]]:
        """Yield ``(Project, session_file_path)`` pairs found under *roots*.

        Reads only the first non-empty line of each file to extract the cwd;
        no full parse is performed during discovery.

        ``include_subagents`` is accepted for interface parity with
        ``ProviderAdapter.discover`` but ignored — Codex rollouts have no
        sub-agent transcript concept.
        """
        for root in roots:
            if not root.exists():
                continue
            for path in sorted(root.rglob("rollout-*.jsonl")):
                cwd: str | None = None
                try:
                    with path.open(encoding="utf-8") as fh:
                        for raw in fh:
                            raw = raw.strip()
                            if not raw:
                                continue
                            record: dict[str, Any] = json.loads(raw)
                            if record.get("type") == "session_meta":
                                cwd = record.get("payload", {}).get("cwd")
                            break  # only the first non-empty line
                except (OSError, json.JSONDecodeError):
                    pass
                project = _codex_project(cwd, str(root))
                yield project, path

    # -- Parsing (synchronous) ------------------------------------------------

    def parse(self, path: Path) -> tuple[Session, list[Message]]:
        """Parse a Codex rollout JSONL file into ``(Session, list[Message])``.

        Builds messages from ``response_item`` records only.  ``event_msg``
        records are used for token usage (``token_count``) and fallback title
        (``task_complete``).  Reasoning blocks carry a placeholder instead of
        the encrypted content.

        Linear ``id``/``parent_id`` chains are synthesised so that
        ``build_message_tree`` yields a single flat chronological thread.
        """
        session_id: str | None = None
        cwd: str | None = None
        git_branch: str | None = None
        session_started_at: datetime | None = None
        model: str | None = None
        last_usage: TokenUsage | None = None
        fallback_title: str | None = None

        # Pending assistant turn state
        _turn_blocks: list[Any] = []
        _turn_ts: datetime | None = None
        _turn_has_reasoning: bool = False

        messages: list[Message] = []

        def flush() -> None:
            """Finalise the open assistant turn into one Message."""
            nonlocal _turn_blocks, _turn_ts, _turn_has_reasoning
            if not _turn_blocks:
                return
            msg = Message(
                id="__placeholder__",  # replaced after all messages collected
                session_id=session_id or "",
                role="assistant",
                content_blocks=list(_turn_blocks),
                timestamp=_turn_ts,
                model=model,
                usage=last_usage,
                provider="codex",
            )
            messages.append(msg)
            _turn_blocks = []
            _turn_ts = None
            _turn_has_reasoning = False

        def open_turn(ts: datetime) -> None:
            """Start an assistant turn if none is open."""
            nonlocal _turn_ts
            if _turn_ts is None:
                _turn_ts = ts

        with path.open(encoding="utf-8") as fh:
            for raw_line in fh:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    record: dict[str, Any] = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue

                rec_type = record.get("type")
                payload: dict[str, Any] = record.get("payload", {}) or {}
                ts_str: str = record.get("timestamp", "")
                ts: datetime | None = None
                if ts_str:
                    try:
                        ts = _parse_ts(ts_str)
                    except ValueError:
                        pass

                # ── session_meta ───────────────────────────────────────────
                if rec_type == "session_meta":
                    session_id = payload.get("id")
                    cwd = payload.get("cwd")
                    git_branch = (payload.get("git") or {}).get("branch")
                    meta_ts_str = payload.get("timestamp", "")
                    if meta_ts_str:
                        try:
                            session_started_at = _parse_ts(meta_ts_str)
                        except ValueError:
                            pass

                # ── turn_context ───────────────────────────────────────────
                elif rec_type == "turn_context":
                    if model is None:
                        model = payload.get("model")

                # ── response_item ──────────────────────────────────────────
                elif rec_type == "response_item":
                    item_type = payload.get("type")

                    if item_type == "message":
                        role = payload.get("role", "")
                        content = payload.get("content") or []

                        if role == "user":
                            flush()
                            blocks = [
                                TextBlock(text=c["text"])
                                for c in content
                                if c.get("type") == "input_text"
                            ]
                            msg = Message(
                                id="__placeholder__",
                                session_id=session_id or "",
                                role="user",
                                is_meta=_is_injected_context(blocks),
                                content_blocks=blocks,
                                timestamp=ts,
                                provider="codex",
                            )
                            messages.append(msg)

                        elif role == "developer":
                            flush()
                            blocks = [
                                TextBlock(text=c["text"])
                                for c in content
                                if c.get("type") == "input_text"
                            ]
                            msg = Message(
                                id="__placeholder__",
                                session_id=session_id or "",
                                role="user",
                                is_meta=True,
                                content_blocks=blocks,
                                timestamp=ts,
                                provider="codex",
                            )
                            messages.append(msg)

                        elif role == "assistant":
                            if ts is not None:
                                open_turn(ts)
                            for c in content:
                                if c.get("type") == "output_text":
                                    _turn_blocks.append(TextBlock(text=c["text"]))

                    elif item_type == "reasoning":
                        if ts is not None:
                            open_turn(ts)
                        if not _turn_has_reasoning:
                            _turn_blocks.append(
                                ThinkingBlock(thinking=_REASONING_PLACEHOLDER)
                            )
                            _turn_has_reasoning = True

                    elif item_type == "function_call":
                        if ts is not None:
                            open_turn(ts)
                        call_id: str = payload.get("call_id", "")
                        name: str = payload.get("name", "")
                        arguments: str = payload.get("arguments", "")
                        try:
                            parsed_args = json.loads(arguments)
                            if not isinstance(parsed_args, dict):
                                raise ValueError("not a dict")
                        except (json.JSONDecodeError, ValueError):
                            parsed_args = {"_raw": arguments}
                        _turn_blocks.append(
                            ToolUseBlock(id=call_id, name=name, input=parsed_args)
                        )

                    elif item_type == "function_call_output":
                        if ts is not None:
                            open_turn(ts)
                        call_id = payload.get("call_id", "")
                        output: str = payload.get("output", "")
                        _turn_blocks.append(
                            ToolResultBlock(tool_use_id=call_id, content=output)
                        )

                # ── event_msg ──────────────────────────────────────────────
                elif rec_type == "event_msg":
                    ev_type = payload.get("type")

                    if ev_type == "token_count":
                        info = payload.get("info")
                        if isinstance(info, dict):
                            ltu = info.get("last_token_usage")
                            if isinstance(ltu, dict):
                                last_usage = TokenUsage(
                                    input_tokens=ltu.get("input_tokens", 0),
                                    cache_read_input_tokens=ltu.get(
                                        "cached_input_tokens", 0
                                    ),
                                    output_tokens=ltu.get("output_tokens", 0),
                                    cache_creation_input_tokens=0,
                                )

                    elif ev_type == "task_complete":
                        msg_text = payload.get("last_agent_message")
                        if msg_text:
                            fallback_title = msg_text

        # Finalise any open assistant turn
        flush()

        # Assign linear ids and parent_id chain
        if not session_id:
            session_id = path.stem  # last resort: use filename stem

        for i, msg in enumerate(messages):
            msg.id = f"{session_id}-{i:04d}"
            msg.session_id = session_id
            msg.parent_id = messages[i - 1].id if i > 0 else None

        # Build session timestamps (min/max over message timestamps)
        all_ts = [m.timestamp for m in messages if m.timestamp is not None]
        if session_started_at is not None:
            all_ts.append(session_started_at)

        started_at: datetime | None = session_started_at
        updated_at: datetime | None = None
        if all_ts:
            started_at = min(all_ts)
            updated_at = max(all_ts)

        # Build project for project_id (best-effort; server stamps authoritative id)
        project = _codex_project(cwd, "")

        session = Session(
            id=session_id,
            project_id=project.id,
            file_path=str(path),
            cwd=cwd,
            git_branch=git_branch,
            model=model,
            started_at=started_at,
            updated_at=updated_at,
            provider="codex",
            message_count=len(messages),
            user_message_count=sum(
                1 for m in messages if m.role == "user" and not m.is_meta
            ),
        )

        # Title: infer from first user message, fallback to task_complete, then hardcoded
        session.title = (
            _infer_title_from_messages(messages) or fallback_title or "Codex session"
        )

        return session, messages
