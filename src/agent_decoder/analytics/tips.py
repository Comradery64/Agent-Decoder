from __future__ import annotations
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Literal, Protocol

from agent_decoder.models import Message, ToolResultBlock, ToolUseBlock


_SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}
_WRITE_TOOLS = frozenset({"Edit", "MultiEdit", "Write", "NotebookEdit"})


@dataclass
class Tip:
    rule_id: str
    severity: Literal["info", "warning", "error"]
    title: str
    detail: str
    evidence: list[str] = field(default_factory=list)


class TipRule(Protocol):
    def check(self, messages: list[Message]) -> list[Tip]: ...


class TipRegistry:
    """Run all registered rules and return tips sorted by severity."""

    def __init__(self) -> None:
        self._rules: list[TipRule] = []

    def register(self, rule: TipRule) -> None:
        self._rules.append(rule)

    def run(self, messages: list[Message]) -> list[Tip]:
        tips: list[Tip] = []
        for rule in self._rules:
            tips.extend(rule.check(messages))
        return sorted(tips, key=lambda t: _SEVERITY_ORDER.get(t.severity, 99))


class RepeatedFileReadRule:
    """Flag files that are Read >= threshold times without an intervening write."""

    def __init__(self, threshold: int = 3) -> None:
        self._threshold = threshold

    def check(self, messages: list[Message]) -> list[Tip]:
        read_counts: dict[str, int] = defaultdict(int)
        flagged: dict[str, int] = {}

        for msg in messages:
            for block in msg.content_blocks:
                if not isinstance(block, ToolUseBlock):
                    continue
                path = block.input.get("file_path") or block.input.get("path") or ""
                if not path or not isinstance(path, str):
                    continue
                if block.name == "Read":
                    read_counts[path] += 1
                    if read_counts[path] >= self._threshold:
                        flagged[path] = read_counts[path]
                elif block.name in _WRITE_TOOLS:
                    read_counts[path] = 0
                    flagged.pop(path, None)

        tips = []
        for path, count in sorted(flagged.items(), key=lambda x: -x[1]):
            tips.append(
                Tip(
                    rule_id="repeated_file_read",
                    severity="warning",
                    title=f"File read {count}× without edit: {path.split('/')[-1]}",
                    detail=(
                        "Reading the same file repeatedly re-sends its full content on every "
                        "prompt turn. Consider reading it once and referencing it from context, "
                        "or check whether the assistant has already loaded this file."
                    ),
                    evidence=[f"{path} — read {count} times"],
                )
            )
        return tips


def _result_char_count(block: ToolResultBlock) -> int:
    if block.content is None:
        return 0
    if isinstance(block.content, str):
        return len(block.content)
    return sum(
        len(item.get("text", "")) for item in block.content if isinstance(item, dict)
    )


class OversizedToolResultRule:
    """Flag tool results larger than threshold_chars characters."""

    def __init__(self, threshold_chars: int = 50_000) -> None:
        self._threshold = threshold_chars

    def check(self, messages: list[Message]) -> list[Tip]:
        tips = []
        for msg in messages:
            for block in msg.content_blocks:
                if not isinstance(block, ToolResultBlock):
                    continue
                size = _result_char_count(block)
                if size < self._threshold:
                    continue
                size_k = round(size / 1_000)
                tips.append(
                    Tip(
                        rule_id="oversized_tool_result",
                        severity="warning",
                        title=f"Oversized tool result (~{size_k}k chars)",
                        detail=(
                            "This result was included verbatim in the context window. "
                            "Large results consume expensive input tokens on every subsequent "
                            "turn. Consider truncating output, using grep/head, or asking "
                            "the assistant to request only the relevant section."
                        ),
                        evidence=[
                            f"~{size_k}k chars (tool_use_id: {block.tool_use_id})"
                        ],
                    )
                )
        return tips


class LowCacheHitRule:
    """Flag corpora where cache-read tokens are < ratio_threshold of all input-type tokens."""

    def __init__(
        self, ratio_threshold: float = 0.10, min_input_tokens: int = 5_000
    ) -> None:
        self._ratio = ratio_threshold
        self._min_input = min_input_tokens

    def check(self, messages: list[Message]) -> list[Tip]:
        total_input = 0
        total_cache_read = 0
        for msg in messages:
            if msg.role != "assistant" or not msg.usage:
                continue
            total_input += (
                msg.usage.input_tokens
                + msg.usage.cache_read_input_tokens
                + msg.usage.cache_creation_input_tokens
            )
            total_cache_read += msg.usage.cache_read_input_tokens

        if total_input < self._min_input:
            return []

        ratio = total_cache_read / total_input
        if ratio >= self._ratio:
            return []

        pct = round(ratio * 100, 1)
        threshold_pct = round(self._ratio * 100, 1)
        return [
            Tip(
                rule_id="low_cache_hit",
                severity="info",
                title=f"Low cache hit ratio ({pct}%)",
                detail=(
                    f"Less than {threshold_pct:g}% of input tokens came from cache reads. "
                    "Pinning large, stable content (system prompts, file context) with "
                    "cache_control can substantially reduce cost and latency on long sessions."
                ),
                evidence=[
                    f"Cache hit ratio: {pct}% "
                    f"({total_cache_read:,} cache-read tokens vs {total_input:,} total input tokens)"
                ],
            )
        ]
