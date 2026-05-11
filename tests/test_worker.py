"""
Tests for agent/worker.py — AgentWorker loop, context management, retry, artifact tracking.
"""
import pytest
from unittest.mock import MagicMock, patch, call

from core.models import ToolResult, EventType
from core.config import LLMConfig
from agent.worker import AgentWorker


@pytest.fixture
def worker(tmp_store, llm_config):
    return AgentWorker(llm_config, tmp_store, max_context_tokens=1000)


class TestBasicLoop:
    def test_text_only_response(self, worker, mock_tool_executor):
        """Worker yields one message and stops when no tool calls."""
        worker.llm.call = MagicMock(return_value={
            "role": "assistant", "content": "Done",
        })
        msgs = list(worker.run("s1", "sys", "do it", [], mock_tool_executor))
        assert len(msgs) == 1
        assert msgs[0].content == "Done"
        assert msgs[0].tool_calls == []

    def test_tool_then_text(self, worker, mock_tool_executor):
        """Worker executes tool calls then stops on text response."""
        worker.llm.call = MagicMock(side_effect=[
            {
                "role": "assistant", "content": "",
                "tool_calls": [{"id": "tc1", "name": "read", "arguments": {"file_path": "x.py"}}],
            },
            {"role": "assistant", "content": "Finished"},
        ])
        msgs = list(worker.run("s1", "sys", "do it", [{"name": "read"}], mock_tool_executor))
        assert len(msgs) == 2
        assert msgs[0].tool_calls is not None
        assert msgs[1].content == "Finished"
        mock_tool_executor.execute.assert_called_once_with("read", {"file_path": "x.py"})

    def test_max_iterations(self, worker, mock_tool_executor):
        """Loop stops at max_iterations even with continuous tool calls."""
        worker.llm.call = MagicMock(return_value={
            "role": "assistant", "content": "",
            "tool_calls": [{"id": "tc1", "name": "bash", "arguments": {"command": "ls"}}],
        })
        msgs = list(worker.run("s1", "sys", "do it", [], mock_tool_executor, max_iterations=3))
        assert len(msgs) == 3


class TestContextManagement:
    def test_estimate_tokens(self):
        msgs = [{"role": "user", "content": "a" * 400}]
        assert AgentWorker._estimate_tokens(msgs) == 100

    def test_estimate_tokens_includes_tool_args(self):
        msgs = [{"role": "assistant", "content": "", "tool_calls": [
            {"arguments": {"key": "v" * 400}}
        ]}]
        # 400 chars from args, ~100 tokens
        assert AgentWorker._estimate_tokens(msgs) >= 90

    def test_truncate_preserves_system(self, worker):
        msgs = [{"role": "system", "content": "sys"}]
        for i in range(30):
            msgs.append({"role": "user", "content": f"msg {i} " * 200})
        result = worker._truncate_messages(msgs, 1000)
        assert result[0]["role"] == "system"
        assert result[0]["content"] == "sys"
        assert len(result) < len(msgs)

    def test_truncate_noop_under_limit(self, worker):
        msgs = [{"role": "system", "content": "hi"}, {"role": "user", "content": "hello"}]
        result = worker._truncate_messages(msgs, 10000)
        assert result == msgs


class TestRetry:
    def test_transient_error_retries(self, worker, mock_tool_executor):
        """Retries on ConnectionError then succeeds."""
        worker.llm.call = MagicMock(side_effect=[
            ConnectionError("refused"),
            {"role": "assistant", "content": "OK"},
        ])
        with patch("agent.worker.time.sleep"):
            msgs = list(worker.run("s1", "sys", "do it", [], mock_tool_executor))
        assert len(msgs) == 1
        assert msgs[0].content == "OK"
        assert worker.llm.call.call_count == 2

    def test_non_transient_raises(self, worker, mock_tool_executor):
        """ValueError is not retried."""
        worker.llm.call = MagicMock(side_effect=ValueError("bad input"))
        with pytest.raises(ValueError):
            list(worker.run("s1", "sys", "do it", [], mock_tool_executor))

    def test_max_retries_exceeded(self, worker, mock_tool_executor):
        """Raises after max retries on persistent transient error."""
        worker.llm.call = MagicMock(side_effect=TimeoutError("timed out"))
        with patch("agent.worker.time.sleep"):
            with pytest.raises(TimeoutError):
                list(worker.run("s1", "sys", "do it", [], mock_tool_executor))
        assert worker.llm.call.call_count == 4  # 1 + 3 retries


class TestRateLimitParsing:
    def test_parse_reset_datetime(self):
        """Parse 'will reset at YYYY-MM-DD HH:MM:SS' pattern."""
        from datetime import datetime, timezone, timedelta
        future = datetime.now(timezone.utc) + timedelta(seconds=120)
        msg = f"429 - rate limit exceeded, will reset at {future.strftime('%Y-%m-%d %H:%M:%S')}"
        wait = AgentWorker._parse_rate_limit_wait(msg)
        assert wait is not None
        assert 100 < wait < 130

    def test_parse_retry_after_seconds(self):
        msg = "429 rate limit, retry-after: 60"
        wait = AgentWorker._parse_rate_limit_wait(msg)
        assert wait == 60.0

    def test_parse_retry_in_seconds(self):
        msg = "rate limited, retry in 30 seconds"
        wait = AgentWorker._parse_rate_limit_wait(msg)
        assert wait == 30.0

    def test_parse_no_match_returns_none(self):
        assert AgentWorker._parse_rate_limit_wait("some other error") is None

    def test_rate_limit_uses_parsed_wait(self, worker, mock_tool_executor):
        """429 error should sleep for parsed duration, not short backoff."""
        worker.llm.call = MagicMock(side_effect=[
            RuntimeError("429 rate limit, retry-after: 60"),
            {"role": "assistant", "content": "done"},
        ])
        with patch("agent.worker.time.sleep") as mock_sleep:
            msgs = list(worker.run("s1", "sys", "do it", [], mock_tool_executor))
        # Should sleep ~61 seconds (parsed + 1 buffer), not 2^0 = 1 second
        assert mock_sleep.call_count == 1
        assert mock_sleep.call_args[0][0] > 30


class TestArtifactTracking:
    def test_tracks_write(self, worker):
        mock_exec = MagicMock()
        mock_exec.execute = MagicMock(return_value=ToolResult(
            tool_call_id="tc1", success=True, output="ok",
        ))
        worker.llm.call = MagicMock(side_effect=[
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "tc1", "name": "write", "arguments": {"file_path": "/tmp/a.py"}}]},
            {"role": "assistant", "content": "done"},
        ])
        list(worker.run("s1", "sys", "do it", [], mock_exec))
        assert worker.artifacts == ["/tmp/a.py"]

    def test_tracks_edit(self, worker):
        mock_exec = MagicMock()
        mock_exec.execute = MagicMock(return_value=ToolResult(
            tool_call_id="tc1", success=True, output="ok",
        ))
        worker.llm.call = MagicMock(side_effect=[
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "tc1", "name": "edit", "arguments": {"file_path": "b.py"}}]},
            {"role": "assistant", "content": "done"},
        ])
        list(worker.run("s1", "sys", "do it", [], mock_exec))
        assert worker.artifacts == ["b.py"]

    def test_no_duplicates(self, worker):
        mock_exec = MagicMock()
        mock_exec.execute = MagicMock(return_value=ToolResult(
            tool_call_id="tc1", success=True, output="ok",
        ))
        worker.llm.call = MagicMock(side_effect=[
            {"role": "assistant", "content": "",
             "tool_calls": [
                 {"id": "tc1", "name": "write", "arguments": {"file_path": "x.py"}},
                 {"id": "tc2", "name": "edit", "arguments": {"file_path": "x.py"}},
             ]},
            {"role": "assistant", "content": "done"},
        ])
        list(worker.run("s1", "sys", "do it", [], mock_exec))
        assert worker.artifacts == ["x.py"]

    def test_resets_on_new_run(self, worker, mock_tool_executor):
        worker.artifacts = ["/old/file.py"]
        worker.llm.call = MagicMock(return_value={"role": "assistant", "content": "ok"})
        list(worker.run("s1", "sys", "do it", [], mock_tool_executor))
        assert worker.artifacts == []

    def test_failed_tool_not_tracked(self, worker):
        mock_exec = MagicMock()
        mock_exec.execute = MagicMock(return_value=ToolResult(
            tool_call_id="tc1", success=False, error="denied",
        ))
        worker.llm.call = MagicMock(side_effect=[
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "tc1", "name": "write", "arguments": {"file_path": "no.py"}}]},
            {"role": "assistant", "content": "done"},
        ])
        list(worker.run("s1", "sys", "do it", [], mock_exec))
        assert worker.artifacts == []
