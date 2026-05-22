"""Tests for #739: tool execution reports progress to prevent stall false positive.

Verifies that:
1. _execute_tool_calls reports progress to progress_tracker on tool execution
2. Progress report includes tool name and success status
3. Multiple bash-only tool calls keep the progress tracker alive
"""
from unittest.mock import MagicMock, patch

from core.progress import ProgressTracker


def _make_worker():
    """Create AgentWorker with mocked dependencies."""
    from core.config import LLMConfig
    from agent.worker import AgentWorker

    config = LLMConfig(provider="openai", model="gpt-4o", api_key="test-key")
    mock_session = MagicMock()
    return AgentWorker(config=config, session_store=mock_session)


def _mock_tool_result(success=True, output="done", error=""):
    result = MagicMock()
    result.success = success
    result.output = output
    result.error = error
    result.duration_ms = 100
    return result


def test_tool_exec_reports_progress_to_tracker():
    """Each tool execution reports progress to progress_tracker (#739)."""
    worker = _make_worker()
    tracker = ProgressTracker(stall_timeout=300)

    tool_executor = MagicMock()
    tool_executor.execute.return_value = _mock_tool_result()

    assistant_message = {
        "tool_calls": [
            {
                "id": "tc_1",
                "name": "bash",
                "arguments": {"command": "pytest tests/"},
            },
        ],
    }

    # Patch LLMClient.call and session_store
    with patch.object(worker.session_store, 'emit_event'):
        worker._execute_tool_calls(
            assistant_message,
            "test-session",
            tool_executor,
            progress_tracker=tracker,
        )

    # Progress tracker should have recent progress
    assert tracker.has_recent_progress(window=5.0)


def test_bash_only_loop_keeps_tracker_alive():
    """Multiple bash-only calls (debug loop) prevent stall timeout (#739)."""
    worker = _make_worker()
    tracker = ProgressTracker(stall_timeout=10)

    tool_executor = MagicMock()
    tool_executor.execute.return_value = _mock_tool_result(
        output="1 passed"
    )

    # Simulate 5 bash test commands in sequence
    for i in range(5):
        assistant_message = {
            "tool_calls": [
                {
                    "id": f"tc_{i}",
                    "name": "bash",
                    "arguments": {"command": f"pytest test_{i}.py"},
                },
            ],
        }
        with patch.object(worker.session_store, 'emit_event'):
            worker._execute_tool_calls(
                assistant_message,
                "test-session",
                tool_executor,
                progress_tracker=tracker,
            )

    # Should NOT be killed — all activity counts as progress
    should_kill, reason = tracker.should_kill()
    assert not should_kill, f"False positive stall: {reason}"


def test_progress_report_contains_tool_info():
    """Progress report includes tool name and result status (#739)."""
    worker = _make_worker()
    tracker = ProgressTracker(stall_timeout=300)
    audit_logger = MagicMock()
    tracker._observers.append(audit_logger)

    tool_executor = MagicMock()
    tool_executor.execute.return_value = _mock_tool_result(
        success=True, output="ok"
    )

    assistant_message = {
        "tool_calls": [
            {
                "id": "tc_1",
                "name": "bash",
                "arguments": {"command": "python -m pytest"},
            },
        ],
    }

    with patch.object(worker.session_store, 'emit_event'):
        worker._execute_tool_calls(
            assistant_message,
            "test-session",
            tool_executor,
            progress_tracker=tracker,
        )

    # Verify progress report was emitted with correct phase
    calls = audit_logger.on_progress.call_args_list
    assert any(
        c[0][0].phase == "tool_exec" and "bash" in c[0][0].message
        for c in calls
    ), f"Expected tool_exec progress, got: {calls}"
