from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date


@dataclass
class TokenBreakdown:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0

    @property
    def total(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_tokens
            + self.cache_read_tokens
        )

    def __add__(self, other: "TokenBreakdown") -> "TokenBreakdown":
        return TokenBreakdown(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_creation_tokens=self.cache_creation_tokens
            + other.cache_creation_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
        )


@dataclass
class PromptCost:
    """Token breakdown for a single user→assistant turn."""

    user_message_id: str
    assistant_message_id: str
    model: str = "unknown"
    breakdown: TokenBreakdown = field(default_factory=TokenBreakdown)


@dataclass
class DailyBucket:
    """Aggregated token breakdown for a UTC calendar day."""

    day: date
    breakdown: TokenBreakdown = field(default_factory=TokenBreakdown)
    session_count: int = 0
    prompt_count: int = 0
