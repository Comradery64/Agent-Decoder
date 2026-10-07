from __future__ import annotations
import statistics
from collections import Counter, defaultdict
from typing import Any

from agent_decoder.models import Message, ToolUseBlock
from .extractor import TokenExtractor
from .prompt import PromptIterator


def compute_stats(values: list[int | float]) -> dict[str, Any] | None:
    """Return mean/median/p95/min/max/count for a non-empty list, else None."""
    if not values:
        return None
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    p95_idx = max(0, int(round(0.95 * n)) - 1)
    return {
        "count": n,
        "mean": round(statistics.mean(sorted_vals), 2),
        "median": statistics.median(sorted_vals),
        "p95": sorted_vals[p95_idx],
        "min": sorted_vals[0],
        "max": sorted_vals[-1],
    }


class PromptStatsScanner:
    """Compute per-assistant-response token distribution stats across a message list.

    prompt_count is the number of assistant API responses (each tool-use
    cycle counted separately), not the number of user messages.
    """

    def __init__(self) -> None:
        self._extractor = TokenExtractor()

    def scan(self, messages: list[Message]) -> dict[str, Any]:
        inputs, outputs, totals = [], [], []
        for prompt in PromptIterator(messages, self._extractor):
            bd = prompt.breakdown
            inputs.append(bd.input_tokens)
            outputs.append(bd.output_tokens)
            totals.append(bd.total)
        return {
            "prompt_count": len(inputs),
            "input_tokens": compute_stats(inputs),
            "output_tokens": compute_stats(outputs),
            "total_tokens": compute_stats(totals),
        }


class ModelUsageScanner:
    """Aggregate token usage broken down by model name."""

    def scan(self, messages: list[Message]) -> list[dict[str, Any]]:
        buckets: dict[str, dict[str, int]] = defaultdict(
            lambda: {
                "message_count": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
            }
        )
        for msg in messages:
            if msg.role != "assistant" or not msg.model or not msg.usage:
                continue
            b = buckets[msg.model]
            b["message_count"] += 1
            b["input_tokens"] += msg.usage.input_tokens
            b["output_tokens"] += msg.usage.output_tokens
            b["total_tokens"] += (
                msg.usage.input_tokens
                + msg.usage.output_tokens
                + msg.usage.cache_creation_input_tokens
                + msg.usage.cache_read_input_tokens
            )
        result = [{"model": model, **counts} for model, counts in buckets.items()]
        return sorted(result, key=lambda r: r["total_tokens"], reverse=True)


class ToolUsageScanner:
    """Count tool_use calls by tool name across all messages."""

    def scan(self, messages: list[Message]) -> list[dict[str, Any]]:
        counts: Counter[str] = Counter()
        for msg in messages:
            for block in msg.content_blocks:
                if isinstance(block, ToolUseBlock):
                    counts[block.name] += 1
        return [{"tool": name, "count": count} for name, count in counts.most_common()]


class FileTouchScanner:
    """Count tool invocations by full file path (file_path or path input key).

    Stores the full path so same-named files in different directories are
    not silently merged. The frontend trims for display.
    """

    def __init__(self, top_n: int = 20) -> None:
        self._top_n = top_n

    def scan(self, messages: list[Message]) -> list[dict[str, Any]]:
        counts: Counter[str] = Counter()
        for msg in messages:
            for block in msg.content_blocks:
                if not isinstance(block, ToolUseBlock):
                    continue
                path = block.input.get("file_path") or block.input.get("path") or ""
                if not path or not isinstance(path, str):
                    continue
                counts[path] += 1
        return [
            {"file": path, "count": count}
            for path, count in counts.most_common(self._top_n)
        ]
