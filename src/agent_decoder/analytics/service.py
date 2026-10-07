from __future__ import annotations
from agent_decoder.models import Message
from .aggregator import DailyAggregator, SessionAggregator
from .extractor import TokenExtractor
from .models import DailyBucket, PromptCost, TokenBreakdown
from .prompt import PromptIterator


class TokenAnalyticsService:
    """Facade: answers analytics questions about a list of messages."""

    def __init__(self) -> None:
        self._extractor = TokenExtractor()

    def session_totals(self, messages: list[Message]) -> TokenBreakdown:
        return SessionAggregator(self._extractor).aggregate(messages)

    def prompt_breakdown(self, messages: list[Message]) -> list[PromptCost]:
        prompts = list(PromptIterator(messages, self._extractor))
        return sorted(prompts, key=lambda p: p.breakdown.total, reverse=True)

    def daily_buckets(self, messages: list[Message]) -> list[DailyBucket]:
        return DailyAggregator(self._extractor).aggregate(messages)
