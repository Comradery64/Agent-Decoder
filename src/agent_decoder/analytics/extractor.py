from __future__ import annotations
from agent_decoder.models import Message
from .models import TokenBreakdown


class TokenExtractor:
    """Extract a TokenBreakdown from a single Message."""

    def extract(self, message: Message) -> TokenBreakdown:
        if message.usage is None:
            return TokenBreakdown()
        u = message.usage
        return TokenBreakdown(
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cache_creation_tokens=u.cache_creation_input_tokens,
            cache_read_tokens=u.cache_read_input_tokens,
        )


class DeduplicatingExtractor:
    """Wraps an extractor; returns zero breakdown for duplicate message IDs."""

    def __init__(self, inner: TokenExtractor) -> None:
        self._inner = inner
        self.seen_ids: set[str] = set()

    def extract(self, message: Message) -> TokenBreakdown:
        if message.id in self.seen_ids:
            return TokenBreakdown()
        self.seen_ids.add(message.id)
        return self._inner.extract(message)
