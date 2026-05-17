"""
Tests for #334: enhanced logging of malformed tool call arguments.

Verifies that when tool calls have missing/blank args, the log messages
include the raw arguments dict for debugging model-specific issues.
"""
import logging
import pytest
from unittest.mock import MagicMock

from core.models import ToolResult
from agent.worker import AgentWorker, EMPTY_CALL_MAX_RETRIES


@pytest.fixture
def worker(tmp_store, llm_config):
    return AgentWorker(llm_config, tmp_store, max_context_tokens=1000)


def _empty_write_call(call_id="tc1"):
    return {"id": call_id, "name": "write", "arguments": {}}


def _valid_write_call(call_id="tc1"):
    return {"id": call_id, "name": "write", "arguments": {
        "file_path": "/tmp/test.py", "content": "x = 1",
    }}


class TestMalformedArgsLogging:
    def test_missing_args_log_includes_raw_args(self, worker, caplog):
        """Missing args warning includes the raw arguments dict (#334)."""
        worker.llm.call = MagicMock(side_effect=[
            {"role": "assistant", "content": "", "tool_calls": [_empty_write_call()]},
        ] * (EMPTY_CALL_MAX_RETRIES + 1) + [
            {"role": "assistant", "content": "done"},
        ])
        mock_exec = MagicMock()
        mock_exec.execute = MagicMock(return_value=ToolResult(
            tool_call_id="tc1", success=True, output="ok",
        ))

        with caplog.at_level(logging.WARNING, logger="agent.worker"):
            list(worker.run("s1", "sys", "do it", [], mock_exec, max_iterations=100))

        # Find the missing args warning
        missing_logs = [
            r for r in caplog.records
            if "missing args" in r.message and "raw args" in r.message
        ]
        assert len(missing_logs) > 0
        # Verify the raw args are included
        assert "{}" in missing_logs[0].message or "arguments" in missing_logs[0].message

    def test_auto_retry_log_includes_raw_calls(self, worker, caplog):
        """Auto-retry warning includes raw tool calls structure (#334)."""
        worker.llm.call = MagicMock(side_effect=[
            {"role": "assistant", "content": "", "tool_calls": [_empty_write_call()]},
        ] * (EMPTY_CALL_MAX_RETRIES + 1) + [
            {"role": "assistant", "content": "done"},
        ])
        mock_exec = MagicMock()

        with caplog.at_level(logging.WARNING, logger="agent.worker"):
            list(worker.run("s1", "sys", "do it", [], mock_exec, max_iterations=100))

        # Find the auto-retry warning
        retry_logs = [
            r for r in caplog.records
            if "Raw calls" in r.message
        ]
        assert len(retry_logs) > 0
        # Verify the raw call structure is included
        assert "write" in retry_logs[0].message

    def test_blank_args_log_includes_raw_args(self, worker, caplog):
        """Blank args warning includes the raw arguments dict (#334)."""
        blank_call = {
            "id": "tc1", "name": "bash",
            "arguments": {"command": ""},
        }
        worker.llm.call = MagicMock(side_effect=[
            {"role": "assistant", "content": "", "tool_calls": [blank_call]},
        ] * (EMPTY_CALL_MAX_RETRIES + 1) + [
            {"role": "assistant", "content": "done"},
        ])
        mock_exec = MagicMock()

        with caplog.at_level(logging.WARNING, logger="agent.worker"):
            list(worker.run("s1", "sys", "do it", [], mock_exec, max_iterations=100))

        blank_logs = [
            r for r in caplog.records
            if "blank args" in r.message and "raw args" in r.message
        ]
        assert len(blank_logs) > 0
