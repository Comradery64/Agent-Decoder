from __future__ import annotations
from typing import Iterator
from agent_decoder.models import Message
from .extractor import TokenExtractor
from .models import PromptCost


class PromptIterator:
    """Yield PromptCost for each non-meta user→assistant pair in a message list."""

    def __init__(
        self, messages: list[Message], extractor: TokenExtractor | None = None
    ) -> None:
        self._messages = messages
        self._extractor = extractor or TokenExtractor()

    def __iter__(self) -> Iterator[PromptCost]:
        by_id: dict[str, Message] = {m.id: m for m in self._messages}
        for msg in self._messages:
            if msg.role != "assistant":
                continue
            if msg.is_sidechain:
                continue
            if msg.parent_id is None or msg.parent_id not in by_id:
                continue
            parent = by_id[msg.parent_id]
            if parent.role != "user" or parent.is_meta:
                continue
            yield PromptCost(
                user_message_id=parent.id,
                assistant_message_id=msg.id,
                model=msg.model or "unknown",
                breakdown=self._extractor.extract(msg),
            )
