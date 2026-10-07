"""Tests for analytics.extractor — token extraction and deduplication."""


class TestTokenBreakdown:
    def test_breakdown_defaults_to_zero(self):
        from agent_decoder.analytics.models import TokenBreakdown

        b = TokenBreakdown()
        assert b.input_tokens == 0
        assert b.output_tokens == 0
        assert b.cache_creation_tokens == 0
        assert b.cache_read_tokens == 0

    def test_breakdown_total(self):
        from agent_decoder.analytics.models import TokenBreakdown

        b = TokenBreakdown(
            input_tokens=10,
            output_tokens=5,
            cache_creation_tokens=100,
            cache_read_tokens=50,
        )
        assert b.total == 165

    def test_breakdown_add(self):
        from agent_decoder.analytics.models import TokenBreakdown

        a = TokenBreakdown(input_tokens=10, output_tokens=5)
        b = TokenBreakdown(input_tokens=20, output_tokens=3)
        c = a + b
        assert c.input_tokens == 30
        assert c.output_tokens == 8


from agent_decoder.models import Message, TokenUsage  # noqa: E402


def _make_assistant(
    id: str, input: int, output: int, cache_create: int = 0, cache_read: int = 0
) -> Message:
    return Message(
        id=id,
        session_id="s1",
        role="assistant",
        usage=TokenUsage(
            input_tokens=input,
            output_tokens=output,
            cache_creation_input_tokens=cache_create,
            cache_read_input_tokens=cache_read,
        ),
    )


class TestTokenExtractor:
    def test_extract_from_assistant_message(self):
        from agent_decoder.analytics.extractor import TokenExtractor

        msg = _make_assistant("m1", input=10, output=5)
        bd = TokenExtractor().extract(msg)
        assert bd.input_tokens == 10
        assert bd.output_tokens == 5

    def test_extract_returns_zero_for_user_message(self):
        from agent_decoder.analytics.extractor import TokenExtractor

        msg = Message(id="u1", session_id="s1", role="user")
        bd = TokenExtractor().extract(msg)
        assert bd.total == 0

    def test_extract_returns_zero_when_usage_is_none(self):
        from agent_decoder.analytics.extractor import TokenExtractor

        msg = Message(id="a1", session_id="s1", role="assistant")
        assert msg.usage is None
        bd = TokenExtractor().extract(msg)
        assert bd.total == 0

    def test_extract_cache_tokens(self):
        from agent_decoder.analytics.extractor import TokenExtractor

        msg = _make_assistant("m2", input=20, output=2, cache_create=100, cache_read=50)
        bd = TokenExtractor().extract(msg)
        assert bd.cache_creation_tokens == 100
        assert bd.cache_read_tokens == 50


class TestDeduplicatingExtractor:
    def test_dedup_skips_already_seen_message_id(self):
        from agent_decoder.analytics.extractor import (
            TokenExtractor,
            DeduplicatingExtractor,
        )

        inner = TokenExtractor()
        dedup = DeduplicatingExtractor(inner)
        msg = _make_assistant("dup-001", input=10, output=5)
        first = dedup.extract(msg)
        second = dedup.extract(msg)  # same id — should be zero
        assert first.input_tokens == 10
        assert second.total == 0

    def test_dedup_counts_distinct_ids(self):
        from agent_decoder.analytics.extractor import (
            TokenExtractor,
            DeduplicatingExtractor,
        )

        dedup = DeduplicatingExtractor(TokenExtractor())
        m1 = _make_assistant("id-1", input=10, output=5)
        m2 = _make_assistant("id-2", input=20, output=3)
        b1 = dedup.extract(m1)
        b2 = dedup.extract(m2)
        assert b1.input_tokens == 10
        assert b2.input_tokens == 20

    def test_dedup_is_stateful(self):
        from agent_decoder.analytics.extractor import (
            TokenExtractor,
            DeduplicatingExtractor,
        )

        dedup = DeduplicatingExtractor(TokenExtractor())
        msg = _make_assistant("same-id", input=50, output=10)
        dedup.extract(msg)
        dedup.extract(msg)
        dedup.extract(msg)
        # Only first counts — seen_ids should have exactly 1 entry
        assert len(dedup.seen_ids) == 1
