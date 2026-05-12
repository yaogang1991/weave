"""
Tests for #215: tool call argument validation in agent/worker.py.

Ensures empty/malformed tool calls return clear error messages
instead of proceeding to the tool executor.
"""
import pytest
from unittest.mock import MagicMock, patch, call

from agent.worker import AgentWorker, TOOL_REQUIRED_ARGS
from core.models import ToolResult


@pytest.fixture
def worker(tmp_path):
    from core.config import LLMConfig
    from session.store import SessionStore

    store = SessionStore(base_path=str(tmp_path / "events"))
    config = LLMConfig(model="test-model")
    return AgentWorker(config=config, session_store=store, base_cwd=str(tmp_path))


def _make_llm_sequence(*responses):
    """Create a side_effect for llm.call that returns the given responses in order."""
    return list(responses)


class TestToolRequiredArgsMapping:
    """Verify TOOL_REQUIRED_ARGS covers core tools."""

    def test_write_requires_file_path_and_content(self):
        assert TOOL_REQUIRED_ARGS["write"] == ["file_path", "content"]

    def test_edit_requires_three_args(self):
        assert set(TOOL_REQUIRED_ARGS["edit"]) == {
            "file_path", "old_string", "new_string",
        }

    def test_bash_requires_command(self):
        assert TOOL_REQUIRED_ARGS["bash"] == ["command"]

    def test_unknown_tool_has_no_required_args(self):
        assert TOOL_REQUIRED_ARGS.get("custom_tool", []) == []


class TestEmptyToolCallValidation:
    """When LLM returns tool calls with empty arguments, worker returns
    a clear error instead of passing to executor."""

    def test_write_empty_args(self, worker, tmp_path):
        """Empty {} arguments for write → error about missing file_path, content."""
        tool_executor = MagicMock()

        # First call: LLM returns empty-args write tool call
        # Second call: LLM returns text (no more tool calls)
        with patch.object(worker.llm, "call", side_effect=[
            {"tool_calls": [{"id": "tc1", "name": "write", "arguments": {}}]},
            {"content": "I'll retry with proper arguments."},
        ]):
            messages = list(worker.run(
                session_id="s1",
                system_prompt="You are a helper.",
                user_message="Write a file.",
                tools=[],
                tool_executor=tool_executor,
            ))

        # Executor should NOT be called
        tool_executor.execute.assert_not_called()
        # Should have 2 messages: one with tool call, one final
        assert len(messages) == 2

    def test_write_empty_args_error_in_tool_result(self, worker, tmp_path):
        """The tool result message should contain clear error about missing args."""
        tool_executor = MagicMock()
        captured_messages = []

        def mock_llm_call(messages, tools):
            captured_messages.append(list(messages))
            if len(captured_messages) == 1:
                return {"tool_calls": [{"id": "tc1", "name": "write", "arguments": {}}]}
            return {"content": "OK retrying"}

        with patch.object(worker.llm, "call", side_effect=mock_llm_call):
            list(worker.run(
                session_id="s1",
                system_prompt="You are a helper.",
                user_message="Write a file.",
                tools=[],
                tool_executor=tool_executor,
            ))

        # The second llm.call should have received the error tool result
        second_call_msgs = captured_messages[1]
        tool_result_msg = [m for m in second_call_msgs if m.get("role") == "tool"]
        assert len(tool_result_msg) == 1
        assert "missing" in tool_result_msg[0]["content"].lower()
        assert "file_path" in tool_result_msg[0]["content"]
        assert "content" in tool_result_msg[0]["content"]

    def test_write_missing_content_only(self, worker, tmp_path):
        """write with file_path but no content → error about missing content."""
        tool_executor = MagicMock()
        captured_messages = []

        def mock_llm_call(messages, tools):
            captured_messages.append(list(messages))
            if len(captured_messages) == 1:
                return {"tool_calls": [{
                    "id": "tc2", "name": "write",
                    "arguments": {"file_path": "/tmp/x.py"},
                }]}
            return {"content": "retrying"}

        with patch.object(worker.llm, "call", side_effect=mock_llm_call):
            list(worker.run("s1", "helper", "write file", [], tool_executor))

        tool_result_msg = [m for m in captured_messages[1] if m.get("role") == "tool"]
        assert len(tool_result_msg) == 1
        assert "content" in tool_result_msg[0]["content"]
        tool_executor.execute.assert_not_called()

    def test_bash_empty_command(self, worker, tmp_path):
        """bash with empty command → error about missing command."""
        tool_executor = MagicMock()
        captured_messages = []

        def mock_llm_call(messages, tools):
            captured_messages.append(list(messages))
            if len(captured_messages) == 1:
                return {"tool_calls": [{
                    "id": "tc3", "name": "bash",
                    "arguments": {"command": ""},
                }]}
            return {"content": "retrying"}

        with patch.object(worker.llm, "call", side_effect=mock_llm_call):
            list(worker.run("s1", "helper", "run cmd", [], tool_executor))

        tool_result_msg = [m for m in captured_messages[1] if m.get("role") == "tool"]
        assert "command" in tool_result_msg[0]["content"]
        tool_executor.execute.assert_not_called()

    def test_valid_tool_call_passes_through(self, worker, tmp_path):
        """Valid tool call is NOT intercepted — goes to executor."""
        tool_executor = MagicMock()
        tool_executor.execute.return_value = ToolResult(
            tool_call_id="tc4", success=True, output="ok", error="", duration_ms=10,
        )

        with patch.object(worker.llm, "call", side_effect=[
            {"tool_calls": [{
                "id": "tc4", "name": "write",
                "arguments": {"file_path": "/tmp/x.py", "content": "x = 1"},
            }]},
            {"content": "Done"},
        ]):
            list(worker.run("s1", "helper", "write file", [], tool_executor))

        tool_executor.execute.assert_called_once_with(
            "write", {"file_path": "/tmp/x.py", "content": "x = 1"},
        )

    def test_mixed_valid_and_invalid(self, worker, tmp_path):
        """One invalid + one valid call: invalid returns error, valid executes."""
        tool_executor = MagicMock()
        tool_executor.execute.return_value = ToolResult(
            tool_call_id="good", success=True, output="ok", error="", duration_ms=10,
        )

        with patch.object(worker.llm, "call", side_effect=[
            {"tool_calls": [
                {"id": "bad", "name": "write", "arguments": {}},
                {
                    "id": "good", "name": "write",
                    "arguments": {"file_path": "/tmp/a.py", "content": "a=1"},
                },
            ]},
            {"content": "Done"},
        ]):
            list(worker.run("s1", "helper", "write files", [], tool_executor))

        # Only the valid call should reach executor
        tool_executor.execute.assert_called_once_with(
            "write", {"file_path": "/tmp/a.py", "content": "a=1"},
        )

    def test_unknown_tool_not_validated(self, worker, tmp_path):
        """Unknown/custom tool calls skip validation and go straight to executor."""
        tool_executor = MagicMock()
        tool_executor.execute.return_value = ToolResult(
            tool_call_id="tc5", success=True, output="custom ok", error="", duration_ms=5,
        )

        with patch.object(worker.llm, "call", side_effect=[
            {"tool_calls": [{"id": "tc5", "name": "custom_tool", "arguments": {}}]},
            {"content": "Done"},
        ]):
            list(worker.run("s1", "helper", "do custom", [], tool_executor))

        tool_executor.execute.assert_called_once_with("custom_tool", {})
