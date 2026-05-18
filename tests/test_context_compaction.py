"""Tests for ContextManager proactive compaction (#480)."""
from unittest.mock import MagicMock

from core.context import ContextManager


class TestShouldCompact:
    """Threshold-based compaction trigger."""

    def test_below_threshold_no_compact(self):
        """Short message list does not trigger compaction."""
        mgr = ContextManager(max_tokens=100_000)
        messages = [{"role": "user", "content": "hello"}]
        assert not mgr.should_compact(messages)

    def test_above_threshold_triggers_compact(self):
        """Large message list triggers compaction."""
        mgr = ContextManager(max_tokens=100, compact_threshold=0.5)
        # Create messages exceeding 50 tokens (0.5 * 100)
        messages = [{"role": "user", "content": "x" * 500}] * 10
        assert mgr.should_compact(messages)


class TestEstimateTokens:
    """Token estimation."""

    def test_empty_messages(self):
        mgr = ContextManager()
        assert mgr.estimate_tokens([]) == 1  # min 1

    def test_english_content(self):
        mgr = ContextManager()
        messages = [{"role": "user", "content": "a" * 100}]
        tokens = mgr.estimate_tokens(messages)
        assert tokens == 25  # 100 / 4

    def test_cjk_content(self):
        mgr = ContextManager()
        messages = [{"role": "user", "content": "\u4e00" * 100}]
        tokens = mgr.estimate_tokens(messages)
        assert tokens == 50  # 100 / 2


class TestCompact:
    """Compaction replaces early messages with summary."""

    def test_short_messages_not_compacted(self):
        """Messages shorter than keep_recent + 1 are returned as-is."""
        mgr = ContextManager(max_tokens=100_000, keep_recent=20)
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
        ]
        result = mgr.compact(messages, llm_client=MagicMock())
        assert result == messages

    def test_compact_preserves_system_and_recent(self):
        """Compaction keeps system prompt and recent messages."""
        mgr = ContextManager(max_tokens=100_000, keep_recent=3)
        # Create 10 non-system messages
        messages = [{"role": "system", "content": "sys"}]
        messages += [{"role": "user", "content": f"msg {i}"} for i in range(10)]

        mock_llm = MagicMock()
        mock_llm.call.return_value = {"content": "Summary of early conversation"}

        result = mgr.compact(messages, mock_llm)

        # First message should be system
        assert result[0]["role"] == "system"
        # Summary message should appear
        summary_msgs = [m for m in result if "[Context summary" in m.get("content", "")]
        assert len(summary_msgs) == 1
        # Last 3 messages preserved
        assert result[-3:] == [
            {"role": "user", "content": "msg 7"},
            {"role": "user", "content": "msg 8"},
            {"role": "user", "content": "msg 9"},
        ]

    def test_compact_with_llm_failure_uses_fallback(self):
        """When LLM call fails, fallback extraction is used."""
        mgr = ContextManager(max_tokens=100_000, keep_recent=3)
        messages = [{"role": "system", "content": "sys"}]
        messages += [{"role": "user", "content": f"file.py msg {i}"} for i in range(10)]

        mock_llm = MagicMock()
        mock_llm.call.side_effect = Exception("API error")

        result = mgr.compact(messages, mock_llm)
        # Should still have summary (from fallback)
        summary_msgs = [m for m in result if "summary" in m.get("content", "").lower()]
        assert len(summary_msgs) == 1


class TestClearStaleToolResults:
    """Stale tool result clearing."""

    def test_keeps_recent_tool_results(self):
        """Only old tool results are cleared."""
        messages = [
            {"role": "tool", "content": "result 1"},
            {"role": "tool", "content": "result 2"},
            {"role": "tool", "content": "result 3"},
            {"role": "tool", "content": "result 4"},
            {"role": "tool", "content": "result 5"},
        ]
        result = ContextManager._clear_stale_tool_results(messages, keep_last_n=3)
        assert result[0]["content"] == "[cleared]"
        assert result[1]["content"] == "[cleared]"
        assert result[2]["content"] == "result 3"
        assert result[3]["content"] == "result 4"
        assert result[4]["content"] == "result 5"

    def test_no_clearing_when_few_results(self):
        """No clearing when tool results <= keep_last_n."""
        messages = [
            {"role": "tool", "content": "result 1"},
            {"role": "tool", "content": "result 2"},
        ]
        result = ContextManager._clear_stale_tool_results(messages, keep_last_n=5)
        assert result == messages

    def test_preserves_non_tool_messages(self):
        """Non-tool messages pass through unchanged."""
        messages = [
            {"role": "assistant", "content": "thinking"},
            {"role": "tool", "content": "old result"},
            {"role": "tool", "content": "recent result"},
        ]
        result = ContextManager._clear_stale_tool_results(messages, keep_last_n=1)
        assert result[0] == {"role": "assistant", "content": "thinking"}
        assert result[1]["content"] == "[cleared]"
        assert result[2]["content"] == "recent result"

    def test_preserves_tool_call_id_when_cleared(self):
        """Cleared tool results keep tool_call_id to avoid KeyError (#570)."""
        messages = [
            {"role": "tool", "tool_call_id": "call_abc123", "content": "old"},
            {"role": "tool", "tool_call_id": "call_def456", "content": "recent"},
        ]
        result = ContextManager._clear_stale_tool_results(messages, keep_last_n=1)
        assert result[0]["content"] == "[cleared]"
        assert result[0]["tool_call_id"] == "call_abc123"
        assert result[1]["tool_call_id"] == "call_def456"
        assert result[1]["content"] == "recent"

    def test_fallback_tool_call_id_when_missing(self):
        """Missing tool_call_id gets a fallback value (#570)."""
        messages = [
            {"role": "tool", "content": "old no id"},
            {"role": "tool", "tool_call_id": "call_xyz", "content": "recent"},
        ]
        result = ContextManager._clear_stale_tool_results(messages, keep_last_n=1)
        assert result[0]["content"] == "[cleared]"
        assert "tool_call_id" in result[0]
        assert result[0]["tool_call_id"].startswith("cleared_")
