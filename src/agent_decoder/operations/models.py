"""Provider-neutral records for supervised agent work."""

from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default_factory=lambda: uuid4().hex)
    created_at: str = Field(default_factory=now)
    updated_at: str = Field(default_factory=now)


RunState = Literal[
    "queued",
    "starting",
    "running",
    "awaiting_input",
    "blocked",
    "completed",
    "failed",
    "canceled",
]
TERMINAL_STATES = {"completed", "failed", "canceled"}


class AgentRun(Record):
    provider: str
    session_id: str
    project_id: str
    model: str | None = None
    cwd: str | None = None
    git_branch: str | None = None
    is_worktree: bool = False
    task_id: str | None = None
    parent_run_id: str | None = None
    state: RunState = "blocked"
    state_reason: str = "No live execution observed"
    native_state: str | None = None
    observed_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None


class CreateRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1)


class FinishRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: Literal["completed", "failed", "canceled"]
    reason: str = Field(min_length=1, max_length=2000)
