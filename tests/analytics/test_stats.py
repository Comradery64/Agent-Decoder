"""Tests for analytics.stats — statistical distributions and scanners."""

from agent_decoder.models import Message, TokenUsage, ToolUseBlock


class TestComputeStats:
    def test_basic_stats(self):
        from agent_decoder.analytics.stats import compute_stats

        s = compute_stats([10, 20, 30, 40, 50])
        assert s["count"] == 5
        assert s["mean"] == 30.0
        assert s["median"] == 30.0
        assert s["min"] == 10
        assert s["max"] == 50

    def test_p95_at_boundary(self):
        from agent_decoder.analytics.stats import compute_stats

        values = list(range(1, 101))  # 1..100
        s = compute_stats(values)
        assert s["p95"] >= 95

    def test_single_value(self):
        from agent_decoder.analytics.stats import compute_stats

        s = compute_stats([42])
        assert s["mean"] == 42.0
        assert s["p95"] == 42

    def test_empty_returns_none(self):
        from agent_decoder.analytics.stats import compute_stats

        assert compute_stats([]) is None


def _asst(id: str, parent: str, input: int, output: int) -> Message:
    return Message(
        id=id,
        session_id="s",
        role="assistant",
        parent_id=parent,
        usage=TokenUsage(input_tokens=input, output_tokens=output),
    )


def _user(id: str) -> Message:
    return Message(id=id, session_id="s", role="user")


class TestPromptStatsScanner:
    def test_scans_prompt_totals(self):
        from agent_decoder.analytics.stats import PromptStatsScanner

        msgs = [
            _user("u1"),
            _asst("a1", "u1", 100, 20),
            _user("u2"),
            _asst("a2", "u2", 200, 40),
        ]
        result = PromptStatsScanner().scan(msgs)
        assert result["prompt_count"] == 2
        assert result["input_tokens"]["mean"] == 150.0
        assert result["input_tokens"]["min"] == 100
        assert result["input_tokens"]["max"] == 200

    def test_output_stats(self):
        from agent_decoder.analytics.stats import PromptStatsScanner

        msgs = [
            _user("u1"),
            _asst("a1", "u1", 10, 50),
            _user("u2"),
            _asst("a2", "u2", 10, 150),
        ]
        result = PromptStatsScanner().scan(msgs)
        assert result["output_tokens"]["median"] == 100.0

    def test_no_prompts_returns_empty(self):
        from agent_decoder.analytics.stats import PromptStatsScanner

        result = PromptStatsScanner().scan([])
        assert result["prompt_count"] == 0
        assert result["input_tokens"] is None


class TestModelUsageScanner:
    def _asst_model(
        self, id: str, model: str, input: int = 10, output: int = 5
    ) -> Message:
        return Message(
            id=id,
            session_id="s",
            role="assistant",
            model=model,
            usage=TokenUsage(input_tokens=input, output_tokens=output),
        )

    def test_groups_by_model(self):
        from agent_decoder.analytics.stats import ModelUsageScanner

        msgs = [
            self._asst_model("a1", "claude-sonnet-4-6", 100, 20),
            self._asst_model("a2", "claude-sonnet-4-6", 200, 30),
            self._asst_model("a3", "claude-opus-4-7", 500, 80),
        ]
        result = ModelUsageScanner().scan(msgs)
        assert len(result) == 2
        sonnet = next(r for r in result if "sonnet" in r["model"])
        assert sonnet["message_count"] == 2
        assert sonnet["input_tokens"] == 300

    def test_no_messages_returns_empty(self):
        from agent_decoder.analytics.stats import ModelUsageScanner

        assert ModelUsageScanner().scan([]) == []

    def test_sorted_by_total_tokens_desc(self):
        from agent_decoder.analytics.stats import ModelUsageScanner

        msgs = [
            self._asst_model("a1", "small-model", 10, 5),
            self._asst_model("a2", "big-model", 1000, 500),
        ]
        result = ModelUsageScanner().scan(msgs)
        assert result[0]["model"] == "big-model"


class TestToolUsageScanner:
    def _msg_with_tools(self, id: str, tools: list[str]) -> Message:
        blocks = [
            ToolUseBlock(id=f"{id}-{i}", name=t, input={}) for i, t in enumerate(tools)
        ]
        return Message(id=id, session_id="s", role="assistant", content_blocks=blocks)

    def test_counts_tool_calls(self):
        from agent_decoder.analytics.stats import ToolUsageScanner

        msgs = [
            self._msg_with_tools("m1", ["Bash", "Read", "Bash"]),
            self._msg_with_tools("m2", ["Write", "Bash"]),
        ]
        result = ToolUsageScanner().scan(msgs)
        bash = next(r for r in result if r["tool"] == "Bash")
        assert bash["count"] == 3

    def test_sorted_by_count_desc(self):
        from agent_decoder.analytics.stats import ToolUsageScanner

        msgs = [self._msg_with_tools("m1", ["Bash", "Bash", "Read"])]
        result = ToolUsageScanner().scan(msgs)
        assert result[0]["tool"] == "Bash"

    def test_no_tools_returns_empty(self):
        from agent_decoder.analytics.stats import ToolUsageScanner

        msg = Message(id="m1", session_id="s", role="assistant")
        assert ToolUsageScanner().scan([msg]) == []


class TestFileTouchScanner:
    def _msg_with_file_tool(self, id: str, tool: str, path: str) -> Message:
        block = ToolUseBlock(id=f"{id}-t", name=tool, input={"file_path": path})
        return Message(id=id, session_id="s", role="assistant", content_blocks=[block])

    def test_counts_file_touches(self):
        from agent_decoder.analytics.stats import FileTouchScanner

        msgs = [
            self._msg_with_file_tool("m1", "Read", "/foo/bar.py"),
            self._msg_with_file_tool("m2", "Edit", "/foo/bar.py"),
            self._msg_with_file_tool("m3", "Read", "/foo/other.py"),
        ]
        result = FileTouchScanner().scan(msgs)
        bar = next(r for r in result if r["file"] == "/foo/bar.py")
        assert bar["count"] == 2

    def test_top_n_limit(self):
        from agent_decoder.analytics.stats import FileTouchScanner

        msgs = [
            self._msg_with_file_tool(f"m{i}", "Read", f"/foo/file{i}.py")
            for i in range(20)
        ]
        result = FileTouchScanner(top_n=10).scan(msgs)
        assert len(result) <= 10

    def test_ignores_tools_without_path(self):
        from agent_decoder.analytics.stats import FileTouchScanner

        block = ToolUseBlock(id="t1", name="Bash", input={"command": "ls"})
        msg = Message(id="m1", session_id="s", role="assistant", content_blocks=[block])
        assert FileTouchScanner().scan([msg]) == []
