"""
Tests for #282: auto-retry for empty tool call args in parallel execution.

When ALL tool calls in an LLM response have missing/blank args, the worker
re-requests the LLM (up to EMPTY_CALL_MAX_RETRIES = 3) before advancing
to the next outer iteration. This prevents cascading failures where some
models produce empty args under concurrent load (64 empty calls in R16).
"""
import pytest
from unittest.mock import MagicMock

from core.models import ToolResult
from agent.worker import AgentWorker, EMPTY_CALL_MAX_RETRIES, DEGENERATE_CALL_LIMIT


@pytest.fixture
def worker(tmp_store, llm_config):
    return AgentWorker(llm_config, tmp_store, max_context_tokens=1000)


def _empty_bash_call(call_id="tc1"):
    return {"id": call_id, "name": "bash", "arguments": {}}


def _blank_bash_call(call_id="tc1"):
    """Tool call with blank (empty string) required argument.

    Unlike _empty_bash_call (args={}), blank args do NOT trigger #541
    early termination, so the auto-retry mechanism is still exercised.
    """
    return {"id": call_id, "name": "bash", "arguments": {"command": ""}}


def _valid_bash_call(call_id="tc1"):
    return {"id": call_id, "name": "bash", "arguments": {"command": "ls"}}


class TestEmptyCallAutoRetry:
    def test_auto_retry_succeeds_on_second_attempt(self, worker, mock_tool_executor):
        """LLM produces blank args first, then valid args on retry."""
        responses = [
            {"role": "assistant", "content": "", "tool_calls": [_blank_bash_call()]},
            {"role": "assistant", "content": "", "tool_calls": [_valid_bash_call()]},
            {"role": "assistant", "content": "done"},
        ]
        worker.llm.call = MagicMock(side_effect=responses)
        msgs = list(worker.run("s1", "sys", "do it", [], mock_tool_executor))

        # 1st LLM call: blank → retry
        # 2nd LLM call: valid → execute
        # 3rd LLM call: text → done
        assert len(msgs) == 2  # valid tool call + text response
        mock_tool_executor.execute.assert_called_once_with("bash", {"command": "ls"})

    def test_auto_retry_exhausted_then_breaker(self, worker):
        """After EMPTY_CALL_MAX_RETRIES, degenerate breaker fires."""
        worker.llm.call = MagicMock(return_value={
            "role": "assistant", "content": "",
            "tool_calls": [_empty_bash_call()],
        })
        mock_exec = MagicMock()
        msgs = list(worker.run("s1", "sys", "do it", [], mock_exec, max_iterations=100))
        assert len(msgs) == DEGENERATE_CALL_LIMIT

    def test_auto_retry_three_attempts_per_iteration(self, worker, mock_tool_executor):
        """Verify exactly EMPTY_CALL_MAX_RETRIES retries happen per blank iteration."""
        call_count = 0

        def counting_call(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= EMPTY_CALL_MAX_RETRIES:
                return {"role": "assistant", "content": "", "tool_calls": [_blank_bash_call()]}
            return {"role": "assistant", "content": "", "tool_calls": [_valid_bash_call()]}

        worker.llm.call = MagicMock(side_effect=counting_call)
        msgs = list(worker.run("s1", "sys", "do it", [], mock_tool_executor, max_iterations=1))

        # Should have made EMPTY_CALL_MAX_RETRIES + 1 LLM calls total
        assert call_count == EMPTY_CALL_MAX_RETRIES + 1
        assert len(msgs) == 1  # one yielded message for the valid iteration

    def test_auto_retry_preserves_artifacts(self, worker, tmp_path):
        """Artifacts from successful retry are preserved."""
        f = tmp_path / "out.py"
        f.write_text("x = 1\n", encoding="utf-8")

        mock_exec = MagicMock()
        mock_exec.execute = MagicMock(return_value=ToolResult(
            tool_call_id="tc1", success=True, output="ok",
        ))

        responses = [
            {"role": "assistant", "content": "", "tool_calls": [_empty_bash_call()]},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "tc1", "name": "write", "arguments": {
                    "file_path": str(f), "content": "x = 1"
                }}
            ]},
            {"role": "assistant", "content": "done"},
        ]
        worker.llm.call = MagicMock(side_effect=responses)
        list(worker.run("s1", "sys", "do it", [], mock_exec))
        assert str(f) in worker.artifacts

    def test_valid_first_call_skips_retry(self, worker, mock_tool_executor):
        """If first LLM call has valid args, no retry happens."""
        responses = [
            {"role": "assistant", "content": "", "tool_calls": [_valid_bash_call()]},
            {"role": "assistant", "content": "done"},
        ]
        worker.llm.call = MagicMock(side_effect=responses)
        msgs = list(worker.run("s1", "sys", "do it", [], mock_tool_executor))

        assert len(msgs) == 2
        # Only 2 LLM calls total (no retry)
        assert worker.llm.call.call_count == 2
