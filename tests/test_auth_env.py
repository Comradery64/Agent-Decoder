from __future__ import annotations

import pytest

from agent_decoder._auth_env import _subscription_env, spawn_env


def test_subscription_env_strips_session_identity(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-stripped")
    monkeypatch.setenv("CLAUDE_CODE_CHILD_SESSION", "1")
    env = _subscription_env()
    assert "ANTHROPIC_API_KEY" not in env
    assert "CLAUDE_CODE_CHILD_SESSION" not in env


@pytest.mark.asyncio
async def test_spawn_env_strips_session_identity_for_unknown_auth(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_CHILD_SESSION", "1")

    async def _fake_auth_method(_bin_name: str) -> str:
        return ""

    monkeypatch.setattr("agent_decoder._auth_env._bin_auth_method", _fake_auth_method)
    env = await spawn_env("claude")
    assert "CLAUDE_CODE_CHILD_SESSION" not in env


@pytest.mark.asyncio
async def test_spawn_env_strips_session_identity_for_claude_ai_auth(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_CHILD_SESSION", "1")

    async def _fake_auth_method(_bin_name: str) -> str:
        return "claude.ai"

    monkeypatch.setattr("agent_decoder._auth_env._bin_auth_method", _fake_auth_method)
    env = await spawn_env("claude")
    assert "CLAUDE_CODE_CHILD_SESSION" not in env
