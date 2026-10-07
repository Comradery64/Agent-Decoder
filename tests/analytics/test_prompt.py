"""Tests for PromptIterator — pairs user prompts with assistant responses."""

from agent_decoder.models import Message, TokenUsage


def _user(id: str, parent: str | None = None) -> Message:
    return Message(id=id, session_id="s", role="user", parent_id=parent)


def _asst(id: str, parent: str, input: int = 10, output: int = 5) -> Message:
    return Message(
        id=id,
        session_id="s",
        role="assistant",
        parent_id=parent,
        usage=TokenUsage(input_tokens=input, output_tokens=output),
    )


class TestPromptIterator:
    def test_pairs_user_with_following_assistant(self):
        from agent_decoder.analytics.prompt import PromptIterator

        msgs = [_user("u1"), _asst("a1", parent="u1")]
        pairs = list(PromptIterator(msgs))
        assert len(pairs) == 1
        assert pairs[0].user_message_id == "u1"
        assert pairs[0].assistant_message_id == "a1"

    def test_multiple_turns(self):
        from agent_decoder.analytics.prompt import PromptIterator

        msgs = [
            _user("u1"),
            _asst("a1", parent="u1"),
            _user("u2", parent="a1"),
            _asst("a2", parent="u2"),
        ]
        pairs = list(PromptIterator(msgs))
        assert len(pairs) == 2

    def test_skips_meta_user_messages(self):
        from agent_decoder.analytics.prompt import PromptIterator

        meta = Message(id="meta1", session_id="s", role="user", is_meta=True)
        asst = _asst("a1", parent="meta1")
        pairs = list(PromptIterator([meta, asst]))
        assert len(pairs) == 0

    def test_assistant_without_user_parent_skipped(self):
        from agent_decoder.analytics.prompt import PromptIterator

        asst = _asst("a1", parent="unknown-id")
        pairs = list(PromptIterator([asst]))
        assert len(pairs) == 0

    def test_prompt_cost_carries_breakdown(self):
        from agent_decoder.analytics.prompt import PromptIterator

        msgs = [_user("u1"), _asst("a1", parent="u1", input=42, output=7)]
        pairs = list(PromptIterator(msgs))
        assert pairs[0].breakdown.input_tokens == 42
        assert pairs[0].breakdown.output_tokens == 7

    def test_sidechain_assistant_skipped(self):
        from agent_decoder.analytics.prompt import PromptIterator

        user = Message(id="u1", session_id="s", role="user")
        sidechain_asst = Message(
            id="a1",
            session_id="s",
            role="assistant",
            parent_id="u1",
            is_sidechain=True,
            usage=TokenUsage(input_tokens=10, output_tokens=5),
        )
        pairs = list(PromptIterator([user, sidechain_asst]))
        assert len(pairs) == 0
