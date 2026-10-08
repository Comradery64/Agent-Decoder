"""Unit tests for CodexAdapter (Phase 2).

Tests the adapter against the sanitized fixture at
``tests/fixtures/codex/sample_rollout.jsonl``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_decoder.models import (
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from agent_decoder.parser import build_message_tree
from agent_decoder.providers.codex import CodexAdapter

FIXTURES = Path(__file__).parent.parent / "fixtures" / "codex"
FIXTURE = FIXTURES / "sample_rollout.jsonl"

SESSION_ID = "019e901c-ca9b-7303-802a-789af509fde0"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def parsed():
    """Parse the fixture once; share across the module."""
    adapter = CodexAdapter()
    session, messages = adapter.parse(FIXTURE)
    return session, messages


# ---------------------------------------------------------------------------
# Identity / capabilities
# ---------------------------------------------------------------------------


class TestIdentity:
    def test_name(self):
        assert CodexAdapter().name == "codex"

    def test_caps_drivable_send_resume(self):
        # Phase 4e: Codex is drivable via the tmux ProviderDriver. These are the
        # STATIC caps; runtime availability gating happens in the server.
        caps = CodexAdapter().capabilities
        assert caps.can_send is True
        assert caps.can_resume is True
        # fork/edit stay off — no flow, and editing would corrupt the rollout.
        assert caps.can_fork is False
        assert caps.can_edit is False


# ---------------------------------------------------------------------------
# owns_path
# ---------------------------------------------------------------------------


class TestOwnsPath:
    def test_true_for_rollout_jsonl(self):
        path = Path("/home/user/.codex/sessions/rollout-abc123.jsonl")
        assert CodexAdapter().owns_path(path) is True

    def test_false_for_projects_jsonl(self):
        path = Path("/home/user/.claude/projects/-foo/session.jsonl")
        assert CodexAdapter().owns_path(path) is False

    def test_false_for_non_jsonl(self):
        path = Path("/home/user/.codex/sessions/rollout-abc123.txt")
        assert CodexAdapter().owns_path(path) is False

    def test_false_for_plain_jsonl_without_prefix(self):
        path = Path("/home/user/.codex/sessions/plain.jsonl")
        assert CodexAdapter().owns_path(path) is False


# ---------------------------------------------------------------------------
# Session metadata
# ---------------------------------------------------------------------------


class TestSessionMetadata:
    def test_session_id(self, parsed):
        session, _ = parsed
        assert session.id == SESSION_ID

    def test_provider(self, parsed):
        session, _ = parsed
        assert session.provider == "codex"

    def test_model(self, parsed):
        session, _ = parsed
        assert session.model == "gpt-5.5"

    def test_cwd(self, parsed):
        session, _ = parsed
        assert session.cwd == "/Volumes/ExternalDrive/Dev/demo-project"

    def test_git_branch(self, parsed):
        session, _ = parsed
        assert session.git_branch == "main"

    def test_title_xml_stripped_first_line(self, parsed):
        session, _ = parsed
        # User message text: "<environment>noise</environment>Add a Decodeotron test helper\nplease keep it small"
        # After XML strip: "Add a Decodeotron test helper" (first non-empty line)
        assert session.title == "Add a Decodeotron test helper"


# ---------------------------------------------------------------------------
# Message coalescing — no double-emit of assistant turns
# ---------------------------------------------------------------------------


class TestAssistantTurnCoalescing:
    def test_exactly_two_assistant_messages(self, parsed):
        _, messages = parsed
        assistant_msgs = [m for m in messages if m.role == "assistant"]
        assert len(assistant_msgs) == 2

    def test_turn_1_has_text_thinking_tooluse_toolresult(self, parsed):
        _, messages = parsed
        assistant_msgs = [m for m in messages if m.role == "assistant"]
        turn1 = assistant_msgs[0]
        block_types = [type(b).__name__ for b in turn1.content_blocks]
        assert "TextBlock" in block_types
        assert "ThinkingBlock" in block_types
        assert "ToolUseBlock" in block_types
        assert "ToolResultBlock" in block_types

    def test_turn_1_has_exactly_one_thinking_block(self, parsed):
        """Two reasoning records → only ONE ThinkingBlock (deduplication)."""
        _, messages = parsed
        assistant_msgs = [m for m in messages if m.role == "assistant"]
        turn1 = assistant_msgs[0]
        thinking_blocks = [
            b for b in turn1.content_blocks if isinstance(b, ThinkingBlock)
        ]
        assert len(thinking_blocks) == 1


# ---------------------------------------------------------------------------
# Tool call argument parsing
# ---------------------------------------------------------------------------


class TestToolCallParsing:
    def test_call_aaa_parsed_as_dict(self, parsed):
        _, messages = parsed
        assistant_msgs = [m for m in messages if m.role == "assistant"]
        turn1 = assistant_msgs[0]
        tool_use_blocks = [
            b for b in turn1.content_blocks if isinstance(b, ToolUseBlock)
        ]
        call_aaa = next(b for b in tool_use_blocks if b.id == "call_aaa")
        assert isinstance(call_aaa.input, dict)
        assert call_aaa.input["command"] == ["ls", "-la"]

    def test_call_bbb_malformed_wrapped_in_raw(self, parsed):
        _, messages = parsed
        assistant_msgs = [m for m in messages if m.role == "assistant"]
        turn2 = assistant_msgs[1]
        tool_use_blocks = [
            b for b in turn2.content_blocks if isinstance(b, ToolUseBlock)
        ]
        call_bbb = next(b for b in tool_use_blocks if b.id == "call_bbb")
        assert call_bbb.input == {"_raw": "this-is-not-json{{"}


# ---------------------------------------------------------------------------
# ToolResultBlock pairing by call_id
# ---------------------------------------------------------------------------


class TestToolResultPairing:
    def test_call_aaa_result_content(self, parsed):
        _, messages = parsed
        assistant_msgs = [m for m in messages if m.role == "assistant"]
        turn1 = assistant_msgs[0]
        result_blocks = [
            b for b in turn1.content_blocks if isinstance(b, ToolResultBlock)
        ]
        result_aaa = next(b for b in result_blocks if b.tool_use_id == "call_aaa")
        assert "total 8" in result_aaa.content

    def test_call_bbb_result(self, parsed):
        _, messages = parsed
        assistant_msgs = [m for m in messages if m.role == "assistant"]
        turn2 = assistant_msgs[1]
        result_blocks = [
            b for b in turn2.content_blocks if isinstance(b, ToolResultBlock)
        ]
        result_bbb = next(b for b in result_blocks if b.tool_use_id == "call_bbb")
        assert result_bbb is not None


# ---------------------------------------------------------------------------
# Reasoning placeholder — no leaked encrypted_content
# ---------------------------------------------------------------------------


class TestReasoningPlaceholder:
    def test_placeholder_text_exact(self, parsed):
        _, messages = parsed
        all_thinking = [
            b
            for m in messages
            for b in m.content_blocks
            if isinstance(b, ThinkingBlock)
        ]
        assert len(all_thinking) == 1
        assert all_thinking[0].thinking == "🔒 Reasoning (encrypted)"

    def test_no_encrypted_content_leaked(self, parsed):
        _, messages = parsed
        for msg in messages:
            for block in msg.content_blocks:
                block_dict = block.model_dump()
                block_json = json.dumps(block_dict)
                assert "REDACTED_DO_NOT_COMMIT" not in block_json, (
                    f"Encrypted content leaked in block: {block_dict}"
                )


# ---------------------------------------------------------------------------
# Developer message → is_meta=True
# ---------------------------------------------------------------------------


class TestDeveloperMessage:
    def test_developer_message_is_meta(self, parsed):
        _, messages = parsed
        meta_msgs = [m for m in messages if m.is_meta]
        assert len(meta_msgs) >= 1
        assert all(m.role == "user" for m in meta_msgs)


# ---------------------------------------------------------------------------
# Token usage
# ---------------------------------------------------------------------------


class TestTokenUsage:
    def test_turn1_assistant_usage(self, parsed):
        _, messages = parsed
        assistant_msgs = [m for m in messages if m.role == "assistant"]
        turn1 = assistant_msgs[0]
        assert turn1.usage is not None
        assert turn1.usage.input_tokens == 1200
        assert turn1.usage.cache_read_input_tokens == 400
        assert turn1.usage.output_tokens == 80


# ---------------------------------------------------------------------------
# Linear id chain
# ---------------------------------------------------------------------------


class TestLinearIds:
    def test_ids_follow_session_prefix(self, parsed):
        session, messages = parsed
        for i, msg in enumerate(messages):
            assert msg.id == f"{SESSION_ID}-{i:04d}"

    def test_first_message_has_no_parent(self, parsed):
        _, messages = parsed
        assert messages[0].parent_id is None

    def test_parent_chain_links_each_to_previous(self, parsed):
        _, messages = parsed
        for i in range(1, len(messages)):
            assert messages[i].parent_id == messages[i - 1].id

    def test_build_message_tree_single_root_chain(self, parsed):
        _, messages = parsed
        tree = build_message_tree(messages)
        assert len(tree) == 1  # single root

        # Walk the chain — each node should have exactly one child until the leaf
        node = tree[0]
        visited_count = 1
        while node.children:
            assert len(node.children) == 1
            node = node.children[0]
            visited_count += 1
        assert visited_count == len(messages)


# ---------------------------------------------------------------------------
# Fallback title scenarios (inline JSONL)
# ---------------------------------------------------------------------------


class TestFallbackTitles:
    def _write_jsonl(self, tmp_path: Path, records: list[dict]) -> Path:
        p = tmp_path / "rollout-test.jsonl"
        p.write_text("\n".join(json.dumps(r) for r in records) + "\n")
        return p

    def test_fallback_to_task_complete_when_no_user_message(self, tmp_path: Path):
        """No user message → title comes from task_complete.last_agent_message."""
        records = [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "00000000-0000-0000-0000-000000000001",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "cwd": "/tmp/demo",
                    "git": {},
                },
            },
            {
                "timestamp": "2026-01-01T00:00:01Z",
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "turn_id": "t1",
                    "last_agent_message": "I finished the task successfully.",
                },
            },
        ]
        path = self._write_jsonl(tmp_path, records)
        session, _ = CodexAdapter().parse(path)
        assert session.title == "I finished the task successfully."

    def test_fallback_to_hardcoded_when_no_title_sources(self, tmp_path: Path):
        """No user message and no task_complete → title == 'Codex session'."""
        records = [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "00000000-0000-0000-0000-000000000002",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "cwd": "/tmp/demo",
                    "git": {},
                },
            },
        ]
        path = self._write_jsonl(tmp_path, records)
        session, _ = CodexAdapter().parse(path)
        assert session.title == "Codex session"


class TestInjectedContextIsMeta:
    """Codex injects an <environment_context> wrapper as the first user
    message; the real prompt is a later user message. The wrapper must be
    flagged is_meta so it's skipped for the title (and user count), otherwise
    every real Codex session's title would be the literal markup."""

    def _write_jsonl(self, tmp_path: Path, records: list[dict]) -> Path:
        p = tmp_path / "rollout-test.jsonl"
        p.write_text("\n".join(json.dumps(r) for r in records) + "\n")
        return p

    def test_environment_context_skipped_for_title(self, tmp_path: Path):
        records = [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "type": "session_meta",
                "payload": {
                    "id": "00000000-0000-0000-0000-000000000003",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "cwd": "/tmp/demo",
                    "git": {},
                },
            },
            {
                "timestamp": "2026-01-01T00:00:01Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            # nested tags + attributes — pair-regex can't strip this
                            "text": "<environment_context>\n  <cwd>/tmp/demo</cwd>\n"
                            "  <shell>zsh</shell>\n</environment_context>",
                        }
                    ],
                },
            },
            {
                "timestamp": "2026-01-01T00:00:02Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "Refactor the auth module"}
                    ],
                },
            },
        ]
        path = self._write_jsonl(tmp_path, records)
        session, messages = CodexAdapter().parse(path)
        # Title is the real prompt, not the markup wrapper.
        assert session.title == "Refactor the auth module"
        # The wrapper message is meta; the real prompt is not.
        user_msgs = [m for m in messages if m.role == "user"]
        assert user_msgs[0].is_meta is True
        assert user_msgs[1].is_meta is False
        # The wrapper is excluded from the user-message count.
        assert session.user_message_count == 1
