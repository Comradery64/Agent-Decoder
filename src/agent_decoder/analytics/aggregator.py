from __future__ import annotations
from datetime import date, timezone
from agent_decoder.models import Message
from .extractor import DeduplicatingExtractor, TokenExtractor
from .models import DailyBucket, TokenBreakdown


class SessionAggregator:
    """Aggregate token breakdowns across all messages in a session."""

    def __init__(self, extractor: TokenExtractor) -> None:
        self._extractor = extractor

    def aggregate(self, messages: list[Message]) -> TokenBreakdown:
        dedup = DeduplicatingExtractor(self._extractor)
        total = TokenBreakdown()
        for msg in messages:
            total = total + dedup.extract(msg)
        return total


class DailyAggregator:
    """Bucket messages into UTC calendar days."""

    def __init__(self, extractor: TokenExtractor) -> None:
        self._extractor = extractor

    def aggregate(self, messages: list[Message]) -> list[DailyBucket]:
        dedup = DeduplicatingExtractor(self._extractor)
        by_id: dict[str, Message] = {m.id: m for m in messages}
        buckets: dict[date, DailyBucket] = {}
        seen_sessions: dict[date, set[str]] = {}
        for msg in messages:
            if msg.timestamp is None:
                continue
            day = msg.timestamp.astimezone(timezone.utc).date()
            bd = dedup.extract(msg)
            if bd.total == 0:
                continue
            if day not in buckets:
                buckets[day] = DailyBucket(day=day)
                seen_sessions[day] = set()
            buckets[day].breakdown = buckets[day].breakdown + bd
            seen_sessions[day].add(msg.session_id)
            if (
                msg.role == "assistant"
                and not msg.is_sidechain
                and not msg.is_meta
                and msg.parent_id in by_id
                and by_id[msg.parent_id].role == "user"
            ):
                buckets[day].prompt_count += 1
        for day, bucket in buckets.items():
            bucket.session_count = len(seen_sessions[day])
        return sorted(buckets.values(), key=lambda b: b.day)
