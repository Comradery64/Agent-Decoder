"""Tests for analytics.tips — tip model, registry, and built-in rules."""

from agent_decoder.models import Message, TokenUsage, ToolUseBlock, ToolResultBlock


def _read_msg(id: str, path: str) -> Message:
    block = ToolUseBlock(id=f"{id}-t", name="Read", input={"file_path": path})
    return Message(id=id, session_id="s", role="assistant", content_blocks=[block])


def _edit_msg(id: str, path: str) -> Message:
    block = ToolUseBlock(id=f"{id}-t", name="Edit", input={"file_path": path})
    return Message(id=id, session_id="s", role="assistant", content_blocks=[block])


def _result_msg(id: str, content: str) -> Message:
    block = ToolResultBlock(tool_use_id=f"{id}-ref", content=content, is_error=False)
    return Message(id=id, session_id="s", role="user", content_blocks=[block])


def _asst_tokens(
    id: str, input: int, cache_read: int = 0, cache_write: int = 0
) -> Message:
    return Message(
        id=id,
        session_id="s",
        role="assistant",
        usage=TokenUsage(
            input_tokens=input,
            output_tokens=5,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=cache_write,
        ),
    )


class TestTipRegistry:
    def _make_rule(self, tips):
        """Stub rule that always returns the given tip list."""

        class StubRule:
            def check(self, messages):
                return tips

        return StubRule()

    def test_empty_registry_returns_no_tips(self):
        from agent_decoder.analytics.tips import TipRegistry

        assert TipRegistry().run([]) == []

    def test_collects_tips_from_all_rules(self):
        from agent_decoder.analytics.tips import Tip, TipRegistry

        t1 = Tip(rule_id="r1", severity="warning", title="A", detail="x", evidence=[])
        t2 = Tip(rule_id="r2", severity="info", title="B", detail="y", evidence=[])
        registry = TipRegistry()
        registry.register(self._make_rule([t1]))
        registry.register(self._make_rule([t2]))
        tips = registry.run([])
        assert len(tips) == 2
        assert {t.rule_id for t in tips} == {"r1", "r2"}

    def test_sorts_by_severity_error_first(self):
        from agent_decoder.analytics.tips import Tip, TipRegistry

        info = Tip(rule_id="r1", severity="info", title="I", detail="", evidence=[])
        warning = Tip(
            rule_id="r2", severity="warning", title="W", detail="", evidence=[]
        )
        error = Tip(rule_id="r3", severity="error", title="E", detail="", evidence=[])
        registry = TipRegistry()
        registry.register(self._make_rule([info, warning, error]))
        tips = registry.run([])
        assert tips[0].severity == "error"
        assert tips[1].severity == "warning"
        assert tips[2].severity == "info"


class TestRepeatedFileReadRule:
    def test_no_tip_below_threshold(self):
        from agent_decoder.analytics.tips import RepeatedFileReadRule

        msgs = [_read_msg(f"m{i}", "/foo/bar.py") for i in range(2)]
        assert RepeatedFileReadRule().check(msgs) == []

    def test_tip_at_threshold(self):
        from agent_decoder.analytics.tips import RepeatedFileReadRule

        msgs = [_read_msg(f"m{i}", "/foo/bar.py") for i in range(3)]
        tips = RepeatedFileReadRule().check(msgs)
        assert len(tips) == 1
        assert tips[0].rule_id == "repeated_file_read"
        assert "/foo/bar.py" in tips[0].evidence[0]

    def test_one_tip_per_offending_file(self):
        from agent_decoder.analytics.tips import RepeatedFileReadRule

        msgs = [_read_msg(f"a{i}", "/a.py") for i in range(4)] + [
            _read_msg(f"b{i}", "/b.py") for i in range(3)
        ]
        tips = RepeatedFileReadRule().check(msgs)
        assert len(tips) == 2

    def test_edit_resets_count(self):
        from agent_decoder.analytics.tips import RepeatedFileReadRule

        # 2 reads, then edit, then 2 more reads — never reaches threshold of 3 in one run
        msgs = [
            _read_msg("r1", "/foo.py"),
            _read_msg("r2", "/foo.py"),
            _edit_msg("e1", "/foo.py"),
            _read_msg("r3", "/foo.py"),
            _read_msg("r4", "/foo.py"),
        ]
        assert RepeatedFileReadRule().check(msgs) == []

    def test_custom_threshold(self):
        from agent_decoder.analytics.tips import RepeatedFileReadRule

        msgs = [_read_msg(f"m{i}", "/foo.py") for i in range(4)]
        # 4 reads with threshold=5 → no tip
        assert RepeatedFileReadRule(threshold=5).check(msgs) == []
        # 5 reads with threshold=5 → 1 tip
        assert (
            len(
                RepeatedFileReadRule(threshold=5).check(
                    msgs + [_read_msg("m4", "/foo.py")]
                )
            )
            == 1
        )

    def test_write_clears_flag(self):
        from agent_decoder.analytics.tips import RepeatedFileReadRule

        # 3 reads (reaches threshold), then a write, then 2 more reads — no tip
        msgs = [
            _read_msg("r1", "/foo.py"),
            _read_msg("r2", "/foo.py"),
            _read_msg("r3", "/foo.py"),
            _edit_msg("e1", "/foo.py"),
            _read_msg("r4", "/foo.py"),
            _read_msg("r5", "/foo.py"),
        ]
        assert RepeatedFileReadRule().check(msgs) == []


class TestOversizedToolResultRule:
    def test_no_tip_below_threshold(self):
        from agent_decoder.analytics.tips import OversizedToolResultRule

        msg = _result_msg("m1", "x" * 10_000)
        assert OversizedToolResultRule().check([msg]) == []

    def test_tip_above_threshold(self):
        from agent_decoder.analytics.tips import OversizedToolResultRule

        msg = _result_msg("m1", "x" * 60_000)
        tips = OversizedToolResultRule().check([msg])
        assert len(tips) == 1
        assert tips[0].rule_id == "oversized_tool_result"
        assert "60" in tips[0].evidence[0]

    def test_multiple_oversized_results(self):
        from agent_decoder.analytics.tips import OversizedToolResultRule

        msgs = [_result_msg(f"m{i}", "x" * (60_000 + i * 1_000)) for i in range(3)]
        tips = OversizedToolResultRule().check(msgs)
        assert len(tips) == 3

    def test_list_content_counted(self):
        from agent_decoder.analytics.tips import OversizedToolResultRule

        block = ToolResultBlock(
            tool_use_id="t1",
            content=[{"type": "text", "text": "x" * 60_000}],
            is_error=False,
        )
        msg = Message(id="m1", session_id="s", role="user", content_blocks=[block])
        tips = OversizedToolResultRule().check([msg])
        assert len(tips) == 1

    def test_custom_threshold(self):
        from agent_decoder.analytics.tips import OversizedToolResultRule

        msg = _result_msg("m1", "x" * 30_000)
        assert OversizedToolResultRule(threshold_chars=20_000).check([msg]) != []


class TestLowCacheHitRule:
    def test_no_tip_below_min_tokens(self):
        from agent_decoder.analytics.tips import LowCacheHitRule

        # 50% cache hit (1k cache-read vs 1k regular input = 50%)
        msgs = [_asst_tokens("m1", input=1_000, cache_read=1_000)]
        # Skipped: total input 2000 < min_input_tokens=5000
        assert LowCacheHitRule().check(msgs) == []

    def test_tip_when_cache_hit_low(self):
        from agent_decoder.analytics.tips import LowCacheHitRule

        # ~2% cache hit across enough data
        msgs = [_asst_tokens("m1", input=10_000, cache_read=200)]
        tips = LowCacheHitRule().check(msgs)
        assert len(tips) == 1
        assert tips[0].rule_id == "low_cache_hit"
        assert tips[0].severity == "info"

    def test_no_tip_above_threshold(self):
        from agent_decoder.analytics.tips import LowCacheHitRule

        # 20% cache hit — above 10% threshold
        msgs = [_asst_tokens("m1", input=8_000, cache_read=2_000)]
        assert LowCacheHitRule().check(msgs) == []

    def test_no_tip_when_insufficient_data(self):
        from agent_decoder.analytics.tips import LowCacheHitRule

        # Only 500 total input tokens — not enough to draw conclusions
        msgs = [_asst_tokens("m1", input=500, cache_read=0)]
        assert LowCacheHitRule().check(msgs) == []

    def test_no_tip_when_no_messages(self):
        from agent_decoder.analytics.tips import LowCacheHitRule

        assert LowCacheHitRule().check([]) == []

    def test_evidence_contains_ratio(self):
        from agent_decoder.analytics.tips import LowCacheHitRule

        msgs = [_asst_tokens("m1", input=10_000, cache_read=200)]
        tip = LowCacheHitRule().check(msgs)[0]
        assert "%" in tip.evidence[0]

    def test_no_tip_when_ratio_at_threshold(self):
        from agent_decoder.analytics.tips import LowCacheHitRule

        # exactly 10% — at threshold, no tip
        msgs = [
            _asst_tokens("m1", input=9_000, cache_read=1_000)
        ]  # 1000/(9000+1000)=10%
        assert LowCacheHitRule().check(msgs) == []

    def test_tip_just_below_threshold(self):
        from agent_decoder.analytics.tips import LowCacheHitRule

        # 9.9% — just below threshold → tip fires
        msgs = [_asst_tokens("m1", input=9_090, cache_read=1_000)]  # ~9.9%
        assert len(LowCacheHitRule().check(msgs)) == 1
