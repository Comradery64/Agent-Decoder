"""Tests for parser.py — Agent 1 must make all of these pass."""

from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


class TestParseSession:
    def test_returns_session_and_messages(self):
        from agent_decoder.parser import parse_session

        session, messages = parse_session(FIXTURES / "simple_session.jsonl")
        assert session is not None
        assert isinstance(messages, list)
        assert len(messages) > 0

    def test_session_id_from_filename(self):
        from agent_decoder.parser import parse_session

        session, _ = parse_session(FIXTURES / "simple_session.jsonl")
        assert session.id == "aaaaaaaa-0000-0000-0000-000000000001"

    def test_session_title_from_custom_title_record(self):
        from agent_decoder.parser import parse_session

        session, _ = parse_session(FIXTURES / "simple_session.jsonl")
        assert session.title == "test-session-fixture"

    def test_session_model_from_first_assistant_message(self):
        from agent_decoder.parser import parse_session

        session, _ = parse_session(FIXTURES / "simple_session.jsonl")
        assert session.model == "claude-sonnet-4-6"

    def test_session_timestamps(self):
        from agent_decoder.parser import parse_session

        session, _ = parse_session(FIXTURES / "simple_session.jsonl")
        assert session.started_at is not None
        assert session.updated_at is not None
        assert session.updated_at >= session.started_at

    def test_session_cwd_and_git_branch(self):
        from agent_decoder.parser import parse_session

        session, _ = parse_session(FIXTURES / "simple_session.jsonl")
        assert session.cwd == "/home/user/project"
        assert session.git_branch == "main"

    def test_session_permission_mode(self):
        from agent_decoder.parser import parse_session

        session, _ = parse_session(FIXTURES / "simple_session.jsonl")
        assert session.permission_mode == "default"

    def test_message_count(self):
        from agent_decoder.parser import parse_session

        session, messages = parse_session(FIXTURES / "simple_session.jsonl")
        assert session.message_count == len(messages)

    def test_user_message_count_excludes_meta(self):
        from agent_decoder.parser import parse_session

        _, messages = parse_session(FIXTURES / "simple_session.jsonl")
        non_meta_user = [m for m in messages if m.role == "user" and not m.is_meta]
        # Only 2 real user prompts in the fixture (not the tool_result meta message)
        assert len(non_meta_user) == 2

    def test_file_not_found_raises(self):
        from agent_decoder.parser import parse_session

        with pytest.raises(FileNotFoundError):
            parse_session(FIXTURES / "nonexistent.jsonl")

    def test_non_uuid_filename_raises(self):
        from agent_decoder.parser import parse_session

        with pytest.raises(ValueError):
            parse_session(
                FIXTURES / "simple_session.jsonl".replace("simple_session", "bad-name")
            )


class TestMessageContent:
    def test_text_content_block(self):
        from agent_decoder.parser import parse_session
        from agent_decoder.models import TextBlock

        _, messages = parse_session(FIXTURES / "simple_session.jsonl")
        assistant_msgs = [m for m in messages if m.role == "assistant"]
        first = assistant_msgs[0]
        assert any(isinstance(b, TextBlock) for b in first.content_blocks)

    def test_thinking_content_block(self):
        from agent_decoder.parser import parse_session
        from agent_decoder.models import ThinkingBlock

        _, messages = parse_session(FIXTURES / "simple_session.jsonl")
        assistant_msgs = [m for m in messages if m.role == "assistant"]
        has_thinking = any(
            isinstance(b, ThinkingBlock)
            for m in assistant_msgs
            for b in m.content_blocks
        )
        assert has_thinking

    def test_tool_use_content_block(self):
        from agent_decoder.parser import parse_session
        from agent_decoder.models import ToolUseBlock

        _, messages = parse_session(FIXTURES / "simple_session.jsonl")
        tool_use_blocks = [
            b for m in messages for b in m.content_blocks if isinstance(b, ToolUseBlock)
        ]
        assert len(tool_use_blocks) == 1
        assert tool_use_blocks[0].name == "Read"

    def test_tool_result_content_block(self):
        from agent_decoder.parser import parse_session
        from agent_decoder.models import ToolResultBlock

        _, messages = parse_session(FIXTURES / "simple_session.jsonl")
        tool_result_blocks = [
            b
            for m in messages
            for b in m.content_blocks
            if isinstance(b, ToolResultBlock)
        ]
        assert len(tool_result_blocks) == 1
        assert tool_result_blocks[0].tool_use_id == "toolu_001"

    def test_string_content_becomes_text_block(self):
        from agent_decoder.parser import _parse_content_blocks
        from agent_decoder.models import TextBlock

        blocks = _parse_content_blocks("Hello world")
        assert len(blocks) == 1
        assert isinstance(blocks[0], TextBlock)
        assert blocks[0].text == "Hello world"


class TestBuildMessageTree:
    def test_root_messages_have_no_parent(self):
        from agent_decoder.parser import parse_session, build_message_tree

        _, messages = parse_session(FIXTURES / "simple_session.jsonl")
        tree = build_message_tree(messages)
        for node in tree:
            assert node.message.parent_id is None or node.message.is_sidechain is False

    def test_sidechain_messages_are_children(self):
        from agent_decoder.parser import parse_session, build_message_tree

        _, messages = parse_session(FIXTURES / "sidechain_session.jsonl")
        tree = build_message_tree(messages)
        # Sidechain messages should NOT be at the root level
        root_ids = {node.message.id for node in tree}
        sidechain_ids = {m.id for m in messages if m.is_sidechain}
        assert not (root_ids & sidechain_ids)

    def test_tree_preserves_order(self):
        from agent_decoder.parser import parse_session, build_message_tree

        _, messages = parse_session(FIXTURES / "simple_session.jsonl")
        tree = build_message_tree(messages)
        timestamps = [node.message.timestamp for node in tree if node.message.timestamp]
        assert timestamps == sorted(timestamps)

    def test_sidechain_falls_back_to_source_tool_assistant_uuid(self):
        """A merged sub-agent transcript (include_subagent_chats): its local
        root message's parent_id lives in the PARENT session's own file, so
        it never resolves against this node map. It should nest under the
        node matching source_tool_assistant_uuid instead of becoming a root.
        """
        from agent_decoder.models import Message
        from agent_decoder.parser import build_message_tree

        parent_assistant = Message(
            id="main-0002",
            session_id="s1",
            parent_id="main-0001",
            role="assistant",
        )
        subagent_root = Message(
            id="side-0001",
            session_id="s1",
            parent_id="unresolvable-parent-uuid-in-another-file",
            role="user",
            is_sidechain=True,
            source_tool_assistant_uuid="main-0002",
        )
        tree = build_message_tree([parent_assistant, subagent_root])

        root_ids = {node.message.id for node in tree}
        assert "side-0001" not in root_ids
        assert root_ids == {"main-0002"}
        parent_node = next(n for n in tree if n.message.id == "main-0002")
        assert [c.message.id for c in parent_node.children] == ["side-0001"]

    def test_sidechain_orphan_root_when_neither_resolves(self):
        """If parent_id AND source_tool_assistant_uuid both fail to resolve,
        the message still falls back to being an orphan root (no crash)."""
        from agent_decoder.models import Message
        from agent_decoder.parser import build_message_tree

        orphan = Message(
            id="side-0001",
            session_id="s1",
            parent_id="unresolvable",
            role="user",
            is_sidechain=True,
            source_tool_assistant_uuid="also-unresolvable",
        )
        tree = build_message_tree([orphan])
        assert [node.message.id for node in tree] == ["side-0001"]


class TestHelpers:
    def test_unmangle_project_id(self):
        from agent_decoder.parser import _unmangle_project_id

        assert (
            _unmangle_project_id("-Volumes-ExternalDrive-Work-foo")
            == "Volumes/ExternalDrive/Work/foo"
        )
        assert _unmangle_project_id("-Users-me-project") == "Users/me/project"

    def test_unmangle_preserves_non_leading_hyphens(self):
        from agent_decoder.parser import _unmangle_project_id

        # Directory names with double hyphens indicate literal hyphens in the path
        result = _unmangle_project_id("-Volumes-ExternalDrive-Work-my--project")
        assert "my-project" in result

    def test_derive_session_id_valid(self):
        from agent_decoder.parser import _derive_session_id

        p = Path("aaaaaaaa-0000-0000-0000-000000000001.jsonl")
        assert _derive_session_id(p) == "aaaaaaaa-0000-0000-0000-000000000001"

    def test_derive_session_id_invalid_raises(self):
        from agent_decoder.parser import _derive_session_id

        with pytest.raises(ValueError):
            _derive_session_id(Path("not-a-uuid.jsonl"))


class TestTokenUsage:
    def test_token_usage_model_defaults(self):
        from agent_decoder.models import TokenUsage

        u = TokenUsage()
        assert u.input_tokens == 0
        assert u.output_tokens == 0
        assert u.cache_creation_input_tokens == 0
        assert u.cache_read_input_tokens == 0

    def test_message_has_usage_field(self):
        from agent_decoder.models import Message, TokenUsage

        msg = Message(id="x", session_id="s", role="assistant")
        assert msg.usage is None
        msg2 = Message(
            id="x",
            session_id="s",
            role="assistant",
            usage=TokenUsage(input_tokens=10, output_tokens=5),
        )
        assert msg2.usage.input_tokens == 10

    def test_parser_extracts_usage_from_assistant(self):
        from agent_decoder.parser import parse_session

        _, messages = parse_session(FIXTURES / "session_with_usage.jsonl")
        assistant_msgs = [m for m in messages if m.role == "assistant"]
        assert len(assistant_msgs) == 2
        first = assistant_msgs[0]
        assert first.usage is not None
        assert first.usage.input_tokens == 12
        assert first.usage.output_tokens == 3
        assert first.usage.cache_creation_input_tokens == 0
        assert first.usage.cache_read_input_tokens == 0

    def test_parser_extracts_cache_usage(self):
        from agent_decoder.parser import parse_session

        _, messages = parse_session(FIXTURES / "session_with_usage.jsonl")
        assistant_msgs = [m for m in messages if m.role == "assistant"]
        second = assistant_msgs[1]
        assert second.usage is not None
        assert second.usage.input_tokens == 20
        assert second.usage.output_tokens == 2
        assert second.usage.cache_creation_input_tokens == 100
        assert second.usage.cache_read_input_tokens == 50

    def test_parser_usage_is_none_for_user_messages(self):
        from agent_decoder.parser import parse_session

        _, messages = parse_session(FIXTURES / "session_with_usage.jsonl")
        user_msgs = [m for m in messages if m.role == "user"]
        assert all(m.usage is None for m in user_msgs)

    def test_parser_usage_is_none_when_absent(self):
        from agent_decoder.parser import parse_session

        _, messages = parse_session(FIXTURES / "simple_session.jsonl")
        # User messages never carry usage regardless of fixture content
        user_msgs = [m for m in messages if m.role == "user"]
        assert all(m.usage is None for m in user_msgs)
