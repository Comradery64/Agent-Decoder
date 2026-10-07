"""Tests for SessionAggregator and DailyAggregator."""

from agent_decoder.models import Message, TokenUsage


def _asst(id: str, input: int, output: int) -> Message:
    return Message(
        id=id,
        session_id="s1",
        role="assistant",
        usage=TokenUsage(input_tokens=input, output_tokens=output),
    )


class TestSessionAggregator:
    def test_aggregates_all_messages(self):
        from agent_decoder.analytics.aggregator import SessionAggregator
        from agent_decoder.analytics.extractor import TokenExtractor

        agg = SessionAggregator(TokenExtractor())
        messages = [_asst("m1", 10, 5), _asst("m2", 20, 3)]
        bd = agg.aggregate(messages)
        assert bd.input_tokens == 30
        assert bd.output_tokens == 8

    def test_aggregator_uses_dedup(self):
        from agent_decoder.analytics.aggregator import SessionAggregator
        from agent_decoder.analytics.extractor import TokenExtractor

        agg = SessionAggregator(TokenExtractor())
        msg = _asst("dup", 10, 5)
        bd = agg.aggregate([msg, msg])  # duplicate in list
        assert bd.input_tokens == 10  # counted once

    def test_empty_message_list_returns_zero(self):
        from agent_decoder.analytics.aggregator import SessionAggregator
        from agent_decoder.analytics.extractor import TokenExtractor

        agg = SessionAggregator(TokenExtractor())
        bd = agg.aggregate([])
        assert bd.total == 0

    def test_user_messages_contribute_zero(self):
        from agent_decoder.analytics.aggregator import SessionAggregator
        from agent_decoder.analytics.extractor import TokenExtractor

        agg = SessionAggregator(TokenExtractor())
        user = Message(id="u1", session_id="s1", role="user")
        asst = _asst("a1", 10, 5)
        bd = agg.aggregate([user, asst])
        assert bd.input_tokens == 10


from datetime import datetime, timezone, date  # noqa: E402


def _asst_with_ts(id: str, input: int, output: int, ts: datetime) -> Message:
    return Message(
        id=id,
        session_id="s1",
        role="assistant",
        timestamp=ts,
        usage=TokenUsage(input_tokens=input, output_tokens=output),
    )


class TestDailyAggregator:
    def test_groups_by_utc_day(self):
        from agent_decoder.analytics.aggregator import DailyAggregator
        from agent_decoder.analytics.extractor import TokenExtractor

        agg = DailyAggregator(TokenExtractor())
        messages = [
            _asst_with_ts(
                "m1", 10, 5, datetime(2026, 1, 1, 23, 0, tzinfo=timezone.utc)
            ),
            _asst_with_ts("m2", 20, 3, datetime(2026, 1, 2, 1, 0, tzinfo=timezone.utc)),
            _asst_with_ts(
                "m3", 30, 7, datetime(2026, 1, 2, 15, 0, tzinfo=timezone.utc)
            ),
        ]
        buckets = agg.aggregate(messages)
        assert len(buckets) == 2
        days = [b.day for b in buckets]
        assert date(2026, 1, 1) in days
        assert date(2026, 1, 2) in days
        jan2 = next(b for b in buckets if b.day == date(2026, 1, 2))
        assert jan2.breakdown.input_tokens == 50

    def test_messages_without_timestamp_are_ignored(self):
        from agent_decoder.analytics.aggregator import DailyAggregator
        from agent_decoder.analytics.extractor import TokenExtractor

        agg = DailyAggregator(TokenExtractor())
        msg = _asst("no-ts", 10, 5)  # no timestamp
        buckets = agg.aggregate([msg])
        assert buckets == []

    def test_buckets_are_chronologically_ordered(self):
        from agent_decoder.analytics.aggregator import DailyAggregator
        from agent_decoder.analytics.extractor import TokenExtractor

        agg = DailyAggregator(TokenExtractor())
        messages = [
            _asst_with_ts("m2", 5, 1, datetime(2026, 1, 3, tzinfo=timezone.utc)),
            _asst_with_ts("m1", 5, 1, datetime(2026, 1, 1, tzinfo=timezone.utc)),
        ]
        buckets = agg.aggregate(messages)
        assert buckets[0].day < buckets[1].day

    def test_session_count_increments(self):
        from agent_decoder.analytics.aggregator import DailyAggregator
        from agent_decoder.analytics.extractor import TokenExtractor

        agg = DailyAggregator(TokenExtractor())
        ts = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        messages = [
            Message(
                id="m1",
                session_id="s1",
                role="assistant",
                timestamp=ts,
                usage=TokenUsage(input_tokens=10, output_tokens=5),
            ),
            Message(
                id="m2",
                session_id="s2",
                role="assistant",
                timestamp=ts,
                usage=TokenUsage(input_tokens=20, output_tokens=3),
            ),
        ]
        buckets = agg.aggregate(messages)
        assert len(buckets) == 1
        assert buckets[0].session_count == 2

    def test_prompt_count_counts_pairs(self):
        from agent_decoder.analytics.aggregator import DailyAggregator
        from agent_decoder.analytics.extractor import TokenExtractor

        agg = DailyAggregator(TokenExtractor())
        ts = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        user = Message(id="u1", session_id="s1", role="user", timestamp=ts)
        asst = Message(
            id="a1",
            session_id="s1",
            role="assistant",
            parent_id="u1",
            timestamp=ts,
            usage=TokenUsage(input_tokens=10, output_tokens=5),
        )
        orphan = Message(
            id="a2",
            session_id="s1",
            role="assistant",
            timestamp=ts,
            usage=TokenUsage(input_tokens=5, output_tokens=2),
        )
        buckets = agg.aggregate([user, asst, orphan])
        assert len(buckets) == 1
        assert buckets[0].prompt_count == 1
