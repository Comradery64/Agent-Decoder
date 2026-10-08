"""Shared domain models — the single source of truth for data shapes across the app."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Content blocks — mirrors API content block types
# ---------------------------------------------------------------------------


class TextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ToolUseBlock(BaseModel):
    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any] = Field(default_factory=dict)


class ToolResultBlock(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: Union[str, list[dict[str, Any]], None] = None
    is_error: bool = False


class ThinkingBlock(BaseModel):
    type: Literal["thinking"] = "thinking"
    thinking: str
    signature: Optional[str] = None


class ImageBlock(BaseModel):
    type: Literal["image"] = "image"
    source: dict[str, Any] = Field(default_factory=dict)


ContentBlock = Union[
    TextBlock, ToolUseBlock, ToolResultBlock, ThinkingBlock, ImageBlock
]


# ---------------------------------------------------------------------------
# Token usage — from API assistant messages
# ---------------------------------------------------------------------------


class TokenUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


# ---------------------------------------------------------------------------
# Message — one turn in the conversation
# ---------------------------------------------------------------------------


class Message(BaseModel):
    id: str
    session_id: str
    parent_id: Optional[str] = None
    role: Literal["user", "assistant", "system"]
    content_blocks: list[ContentBlock] = Field(default_factory=list)
    timestamp: Optional[datetime] = None
    model: Optional[str] = None
    is_sidechain: bool = False
    is_meta: bool = False
    cwd: Optional[str] = None
    git_branch: Optional[str] = None
    request_id: Optional[str] = None
    # Links sub-agent messages to the tool_use that spawned them
    source_tool_assistant_uuid: Optional[str] = None
    usage: Optional[TokenUsage] = None
    provider: str = "claude"


class MessageTree(BaseModel):
    """A message with its direct children attached (for sidechain branches)."""

    message: Message
    children: list["MessageTree"] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Session — one JSONL file
# ---------------------------------------------------------------------------


class Session(BaseModel):
    id: str
    project_id: str
    file_path: str
    title: Optional[str] = None
    # Server-side rename override (issue #11). Frontend overlays this on top
    # of `title` so the original parsed title stays available for display
    # fallback and search debugging.
    custom_title: Optional[str] = None
    # Server-persisted session flags (previously localStorage-only on the FE).
    # ISO-8601 string when set, None when unset.  See Database.set_archived /
    # set_starred / set_viewed_at + db._migrate_add_session_meta_flags.
    archived_at: Optional[str] = None
    starred_at: Optional[str] = None
    viewed_at: Optional[str] = None
    model: Optional[str] = None
    started_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    message_count: int = 0
    user_message_count: int = 0
    cwd: Optional[str] = None
    git_branch: Optional[str] = None
    is_worktree: bool = False
    is_fork: bool = False
    permission_mode: Optional[str] = None
    provider: str = "claude"
    last_message_role: Optional[Literal["user", "assistant", "system"]] = None


class SessionDetail(Session):
    """Session with its full conversation included."""

    messages: list[Message] = Field(default_factory=list)
    total_message_count: Optional[int] = None  # set when messages are truncated
    # Resolved from the session's project ``resolved_path`` in the DB.
    # True if the cwd (working directory) still exists on disk; False if
    # the project directory has been deleted since the session was last
    # scanned. The FE uses this to short-circuit submit with a clear
    # error rather than letting the PTY spawn fail downstream.
    cwd_exists: bool = True


# ---------------------------------------------------------------------------
# Project — one project directory (may contain many sessions)
# ---------------------------------------------------------------------------


class Project(BaseModel):
    id: str  # slug derived from directory path
    display_name: str  # human-readable name
    raw_path: str  # the mangled path string from directory name
    resolved_path: Optional[str] = None  # actual filesystem path if it exists
    data_source: str = ""  # which configured root path this came from
    session_count: int = 0
    last_activity_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


class SearchHit(BaseModel):
    session_id: str
    session_title: Optional[str]
    project_id: str
    message_id: str
    role: str
    snippet: str  # FTS5 highlighted excerpt
    timestamp: Optional[datetime]
    # Distinguishes regular messages from ephemeral (/btw) exchanges.
    source: Literal["message", "ephemeral"] = "message"
    # Only set for ephemeral hits: the kind tag (e.g. "btw") and the
    # responds_to id linking user/assistant pairs.
    kind: Optional[str] = None
    responds_to: Optional[int] = None


# ---------------------------------------------------------------------------
# Config — user-facing settings
# ---------------------------------------------------------------------------


class Profile(BaseModel):
    id: str = Field(default_factory=lambda: __import__("uuid").uuid4().hex[:12])
    name: str = Field(..., min_length=1)
    data_paths: list[str] = Field(default_factory=lambda: ["~/.claude"])
    color: str = "#b8956a"


class AppConfig(BaseModel):
    data_paths: list[str] = Field(
        default_factory=lambda: ["~/.claude"],
        description="Root directories to scan (legacy, used when profiles is empty)",
    )
    profiles: list[Profile] = Field(default_factory=list)
    active_profile_id: Optional[str] = None
    theme: Literal["light", "dark", "system"] = "system"
    auto_open_browser: bool = True
    port: int = 4242
    host: str = "127.0.0.1"
    edit_enabled: bool = True
    # Show a small per-provider tag on non-Claude sidebar rows. Off by default
    # — opt-in, since a tag on every chat adds clutter as more CLIs integrate.
    show_provider_tag: bool = False
    # Ingest Claude Code sub-agent (Task tool) transcripts — stored under
    # <project-dir>/<session>/subagents/agent-*.jsonl — and merge them into
    # their parent session as sidechain branches. Off by default: the scanner
    # already globs *.jsonl non-recursively, so this is a genuinely new data
    # source, not just a rendering change.
    include_subagent_chats: bool = False
    claude_default_permission_mode: str = "default"
    chat_send_shortcut: Literal["enter", "modEnter"] = "enter"
    native_pty_font_family: Literal[
        "monaspace-argon",
        "source-code-pro",
        "fira-code",
        "jetbrains-mono",
        "ioskeley-mono",
        "libertinus-mono",
        "antithesis",
        "thesansmono-condensed",
        "xanh-mono",
        "julia-mono",
        "spline-sans-mono",
        "system-monospace",
    ] = "monaspace-argon"
    native_pty_cols: int = Field(
        default=100,
        ge=20,
        le=400,
        description=(
            "Native PTY width in terminal columns. Single source of truth: the "
            "PTY is spawned at this width (TIOCSWINSZ) and the browser terminal "
            "renders at the same width. The terminal never reflows its width, so "
            "Claude's output is displayed exactly as a native session would."
        ),
    )
    claude_auto_stop_quiet_default_turns: bool = False
    claude_recap_enabled: bool = False
    claude_recap_idle_minutes: int = 5
    codex_data_paths: list[str] = Field(
        default_factory=lambda: ["~/.codex/sessions"],
        description="Root directories to scan for Codex CLI session files (rollout-*.jsonl)",
    )

    def get_all_scan_paths(self) -> list[str]:
        """Collect all data_paths from all profiles (deduplicated, expanded)."""
        from pathlib import Path

        if self.profiles:
            seen: set[str] = set()
            result: list[str] = []
            for p in self.profiles:
                for dp in p.data_paths:
                    expanded = str(Path(dp).expanduser())
                    if expanded not in seen:
                        result.append(dp)
                        seen.add(expanded)
            return result or self.data_paths
        return self.data_paths

    def get_active_data_sources(self) -> list[str] | None:
        """Return expanded paths for the active profile, or None for all.

        Codex data paths are always appended to the active profile's sources:
        profiles partition *Claude* history (e.g. personal vs work), but Codex
        is not part of that partition, so its sessions must stay visible no
        matter which Claude profile is active. Without this, an active profile
        filters out every Codex project (whose ``data_source`` is the ~/.codex
        root) and Codex sessions vanish from the list/search/analytics.
        """
        from pathlib import Path

        if not self.profiles or not self.active_profile_id:
            return None
        codex = [str(Path(dp).expanduser()) for dp in self.codex_data_paths]
        for p in self.profiles:
            if p.id == self.active_profile_id:
                return [str(Path(dp).expanduser()) for dp in p.data_paths] + codex
        return None


# ---------------------------------------------------------------------------
# API response envelopes
# ---------------------------------------------------------------------------


class StatsResponse(BaseModel):
    total_projects: int
    total_sessions: int
    total_messages: int
    data_paths: list[str]
