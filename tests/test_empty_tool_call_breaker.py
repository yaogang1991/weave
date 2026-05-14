"""
Tests for #290: circuit breaker for consecutive empty/invalid tool calls.

When the LLM repeatedly generates tool calls with missing or blank arguments,
the worker loop must break after EMPTY_TOOL_CALL_LIMIT consecutive all-invalid
iterations to prevent infinite cycling (observed 184 empty calls in R20).
"""
import pytest
from unittest.mock import MagicMock

from core.models import ToolResult, EventType
from core.config import LLMConfig
from agent.worker import AgentWorker, EMPTY_TOOL_CALL_LIMIT


@pytest.fixture
def worker(tmp_store, llm_config):
    return AgentWorker(llm_config, tmp_store, max_context_tokens=1000)


def _empty_bash_call(call_id="tc1"):
    """Tool call with missing required argument (command)."""
    return {"id": call_id, "name": "bash", "arguments": {}}


def _blank_bash_call(call_id="tc1"):
    """Tool call with blank required argument (command="")."""
    return {"id": call_id, "name": "bash", "arguments": {"command": ""}}


def _valid_bash_call(call_id="tc1"):
    """Valid tool call."""
    return {"id": call_id, "name": "bash", "arguments": {"command": "ls"}}


def _empty_write_call(call_id="tc1"):
    """Write tool call missing required args."""
    return {"id": call_id, "name": "write", "arguments": {}}


class TestEmptyToolCallBreaker:
    def test_breaks_on_consecutive_missing_args(self, worker):
        """Loop breaks after EMPTY_TOOL_CALL_LIMIT consecutive all-invalid iterations."""
        worker.llm.call = MagicMock(return_value={
            "role": "assistant",
            "content": "",
            "tool_calls": [_empty_bash_call()],
        })
        mock_exec = MagicMock()
        mock_exec.execute = MagicMock(return_value=ToolResult(
            tool_call_id="tc1", success=True, output="ok",
        ))

        msgs = list(worker.run("s1", "sys", "do it", [], mock_exec, max_iterations=100))

        # Should stop at EMPTY_TOOL_CALL_LIMIT, not reach max_iterations
        assert len(msgs) == EMPTY_TOOL_CALL_LIMIT
        # Tool executor should never have been called (all calls invalid)
        mock_exec.execute.assert_not_called()

    def test_breaks_on_consecutive_blank_args(self, worker):
        """Same behavior for blank (empty string) required arguments."""
        worker.llm.call = MagicMock(return_value={
            "role": "assistant",
            "content": "",
            "tool_calls": [_blank_bash_call()],
        })
        mock_exec = MagicMock()
        msgs = list(worker.run("s1", "sys", "do it", [], mock_exec, max_iterations=100))
        assert len(msgs) == EMPTY_TOOL_CALL_LIMIT
        mock_exec.execute.assert_not_called()

    def test_resets_on_successful_tool_call(self, worker, mock_tool_executor):
        """Counter resets when at least one tool call is valid.

        Pattern: 9 invalid → 1 valid → 9 invalid → text response
        Total: 19 iterations, well under max_iterations=100.
        Without reset, the second batch of 9 would trigger the breaker at
        iteration 10.
        """
        # Build sequence: 9 empty, 1 valid, 9 empty, 1 text-only
        responses = []
        for _ in range(9):
            responses.append({
                "role": "assistant", "content": "",
                "tool_calls": [_empty_bash_call()],
            })
        responses.append({
            "role": "assistant", "content": "",
            "tool_calls": [_valid_bash_call()],
        })
        for _ in range(9):
            responses.append({
                "role": "assistant", "content": "",
                "tool_calls": [_empty_bash_call()],
            })
        responses.append({"role": "assistant", "content": "done"})

        worker.llm.call = MagicMock(side_effect=responses)
        msgs = list(worker.run("s1", "sys", "do it", [], mock_tool_executor, max_iterations=100))

        # Should get all 19 tool-call iterations + final text = 20 messages
        assert len(msgs) == 20

    def test_emits_error_event_on_breaker(self, worker, tmp_store):
        """Breaker emits an AGENT_ERROR event with circuit breaker details."""
        worker.llm.call = MagicMock(return_value={
            "role": "assistant",
            "content": "",
            "tool_calls": [_empty_bash_call()],
        })
        mock_exec = MagicMock()
        list(worker.run("s1", "sys", "do it", [], mock_exec, max_iterations=100))

        # Verify AGENT_ERROR event was emitted
        events = tmp_store.get_events("s1")
        error_events = [e for e in events if e.type == EventType.AGENT_ERROR]
        assert len(error_events) == 1
        data = error_events[0].payload
        assert data["error"] == "empty_tool_call_circuit_breaker"
        assert data["consecutive_empty_iterations"] == EMPTY_TOOL_CALL_LIMIT

    def test_mixed_valid_invalid_does_not_trigger(self, worker, mock_tool_executor):
        """If at least one tool call per iteration is valid, breaker doesn't trigger."""
        # Each iteration has one valid and one invalid tool call
        worker.llm.call = MagicMock(return_value={
            "role": "assistant",
            "content": "",
            "tool_calls": [_valid_bash_call("tc1"), _empty_write_call("tc2")],
        })

        # Limit to 15 iterations (past the breaker threshold)
        msgs = list(worker.run("s1", "sys", "do it", [], mock_tool_executor, max_iterations=15))
        assert len(msgs) == 15

    def test_preserves_artifacts_before_breaker(self, worker, tmp_path):
        """Artifacts from earlier successful iterations are preserved when breaker triggers."""
        f = tmp_path / "out.py"
        f.write_text("x = 1\n", encoding="utf-8")

        mock_exec = MagicMock()
        mock_exec.execute = MagicMock(return_value=ToolResult(
            tool_call_id="tc1", success=True, output="ok",
        ))

        # First iteration: valid write, then breaker-triggering empty calls
        responses = [
            {
                "role": "assistant", "content": "",
                "tool_calls": [{"id": "tc1", "name": "write", "arguments": {
                    "file_path": str(f), "content": "x = 1"
                }}],
            },
        ]
        for _ in range(EMPTY_TOOL_CALL_LIMIT):
            responses.append({
                "role": "assistant", "content": "",
                "tool_calls": [_empty_bash_call()],
            })

        worker.llm.call = MagicMock(side_effect=responses)
        list(worker.run("s1", "sys", "do it", [], mock_exec, max_iterations=100))

        # Artifact from the first valid iteration should be preserved
        assert str(f) in worker.artifacts

    def test_multiple_empty_calls_per_iteration(self, worker):
        """Breaker triggers when each iteration has multiple all-invalid tool calls."""
        worker.llm.call = MagicMock(return_value={
            "role": "assistant",
            "content": "",
            "tool_calls": [_empty_bash_call("tc1"), _empty_write_call("tc2")],
        })
        mock_exec = MagicMock()
        msgs = list(worker.run("s1", "sys", "do it", [], mock_exec, max_iterations=100))
        assert len(msgs) == EMPTY_TOOL_CALL_LIMIT
        mock_exec.execute.assert_not_called()

    def test_no_tool_calls_does_not_count_as_empty(self, worker):
        """Iterations with no tool calls (text-only response) don't increment the counter."""
        # All text responses — should run until max_iterations... actually no,
        # text responses break the loop immediately. Test that they don't trigger breaker.
        worker.llm.call = MagicMock(return_value={
            "role": "assistant", "content": "thinking...",
        })
        mock_exec = MagicMock()
        msgs = list(worker.run("s1", "sys", "do it", [], mock_exec, max_iterations=100))
        assert len(msgs) == 1
        mock_exec.execute.assert_not_called()
