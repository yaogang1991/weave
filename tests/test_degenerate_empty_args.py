"""Tests for #345: degenerate empty-args circuit breaker.

When the LLM repeatedly produces tool calls with completely empty args {},
the agent loop should break after DEGENERATE_CALL_LIMIT (3) iterations
instead of waiting for the broader EMPTY_TOOL_CALL_LIMIT (10).
"""

from __future__ import annotations

from unittest.mock import MagicMock

from core.config import LLMConfig
from core.models import EventType
from session.store import SessionStore


def _make_worker():
    """Create an AgentWorker with mocked dependencies."""
    from agent.worker import AgentWorker

    config = LLMConfig(api_key="test", model="test-model")
    store = MagicMock(spec=SessionStore)
    return AgentWorker(config=config, session_store=store)


def _empty_args_write_call():
    """Tool call with completely empty arguments dict."""
    return {
        "id": "call_1",
        "name": "write",
        "arguments": {},
    }


def _missing_one_arg_call():
    """Tool call with partially missing args (not degenerate)."""
    return {
        "id": "call_2",
        "name": "write",
        "arguments": {"file_path": "test.py"},
    }


def _valid_write_call():
    """Tool call with all required args."""
    return {
        "id": "call_3",
        "name": "write",
        "arguments": {"file_path": "test.py", "content": "pass"},
    }


def test_degenerate_breaks_at_limit():
    """After DEGENERATE_CALL_LIMIT consecutive empty-args iterations,
    the loop breaks."""
    from agent.worker import DEGENERATE_CALL_LIMIT

    worker = _make_worker()
    worker.llm = MagicMock()

    # Each LLM call returns write with empty args {}
    empty_response = {
        "role": "assistant",
        "content": "",
        "tool_calls": [_empty_args_write_call()],
    }
    worker.llm.call.return_value = empty_response

    tool_executor = MagicMock()
    tool_executor.execute.return_value = MagicMock(
        success=True, output="ok", error="",
    )

    list(worker.run(
        session_id="test",
        system_prompt="You are a test agent.",
        user_message="Write hello.py",
        tools=[{"name": "write", "type": "function", "function": {}}],
        tool_executor=tool_executor,
        max_iterations=50,
    ))

    # Should break at DEGENERATE_CALL_LIMIT, not run all 50 iterations
    assert len(worker.llm.call.call_args_list) <= DEGENERATE_CALL_LIMIT * 4 + 1

    # Verify error event was emitted
    error_events = [
        c for c in worker.session_store.emit_event.call_args_list
        if len(c.args) >= 2 and c.args[1] == EventType.AGENT_ERROR
    ]
    degenerate_events = [
        e for e in error_events
        if e.args[2].get("error") == "degenerate_empty_args_breaker"
    ]
    assert len(degenerate_events) == 1


def test_partial_missing_args_not_degenerate():
    """Tool calls with SOME args missing should NOT trigger the degenerate
    breaker — only completely empty args {} counts."""
    from agent.worker import DEGENERATE_CALL_LIMIT

    worker = _make_worker()
    worker.llm = MagicMock()

    # Response with partially missing args (content missing)
    partial_response = {
        "role": "assistant",
        "content": "",
        "tool_calls": [_missing_one_arg_call()],
    }
    worker.llm.call.return_value = partial_response

    tool_executor = MagicMock()

    list(worker.run(
        session_id="test",
        system_prompt="You are a test agent.",
        user_message="Write hello.py",
        tools=[{"name": "write", "type": "function", "function": {}}],
        tool_executor=tool_executor,
        max_iterations=50,
    ))

    # Should NOT break at DEGENERATE_CALL_LIMIT (3), but at
    # EMPTY_TOOL_CALL_LIMIT (10)
    call_count = len(worker.llm.call.call_args_list)
    assert call_count > DEGENERATE_CALL_LIMIT * 3  # Not the degenerate limit

    # Verify no degenerate error event
    error_events = [
        c for c in worker.session_store.emit_event.call_args_list
        if len(c.args) >= 2 and c.args[1] == EventType.AGENT_ERROR
    ]
    degenerate_events = [
        e for e in error_events
        if e.args[2].get("error") == "degenerate_empty_args_breaker"
    ]
    assert len(degenerate_events) == 0


def test_degenerate_counter_resets_on_valid_call():
    """A valid tool call in between resets the degenerate counter."""
    worker = _make_worker()
    worker.llm = MagicMock()

    valid_response = {
        "role": "assistant",
        "content": "",
        "tool_calls": [_valid_write_call()],
    }
    empty_response = {
        "role": "assistant",
        "content": "",
        "tool_calls": [_empty_args_write_call()],
    }
    # Pattern: empty, empty, valid, empty, empty, empty → degenerate at 3rd
    # after reset
    call_count = [0]
    responses = [
        empty_response,   # iter 0: empty
        empty_response,   # iter 1: empty
        valid_response,   # iter 2: valid → resets degenerate counter
        empty_response,   # iter 3: empty (degenerate=1)
        empty_response,   # iter 4: empty (degenerate=2)
        empty_response,   # iter 5: empty (degenerate=3) → break
    ]

    def next_response(messages, tools=None, max_retries=None):
        idx = min(call_count[0], len(responses) - 1)
        call_count[0] += 1
        return responses[idx]

    worker.llm.call.side_effect = next_response

    tool_executor = MagicMock()
    # Valid write returns success
    tool_executor.execute.return_value = MagicMock(
        success=True, output="written", error="",
    )

    list(worker.run(
        session_id="test",
        system_prompt="You are a test agent.",
        user_message="Write hello.py",
        tools=[{"name": "write", "type": "function", "function": {}}],
        tool_executor=tool_executor,
        max_iterations=50,
    ))

    # The degenerate breaker should have fired
    error_events = [
        c for c in worker.session_store.emit_event.call_args_list
        if len(c.args) >= 2 and c.args[1] == EventType.AGENT_ERROR
    ]
    degenerate_events = [
        e for e in error_events
        if e.args[2].get("error") == "degenerate_empty_args_breaker"
    ]
    assert len(degenerate_events) == 1


def test_degenerate_constant_value():
    """DEGENERATE_CALL_LIMIT should be 3 (lower than EMPTY_TOOL_CALL_LIMIT=10)."""
    from agent.worker import DEGENERATE_CALL_LIMIT, EMPTY_TOOL_CALL_LIMIT

    assert DEGENERATE_CALL_LIMIT == 3
    assert DEGENERATE_CALL_LIMIT < EMPTY_TOOL_CALL_LIMIT


def test_empty_args_skips_llm_retries():
    """Completely empty args {} skip the 3× LLM retry cycle (#541).

    Without the fix, each iteration would try up to 4 LLM calls
    (1 initial + 3 retries). With the fix, empty-args responses
    break out of the retry loop immediately, so each iteration
    only makes 1 LLM call.
    """
    from agent.worker import DEGENERATE_CALL_LIMIT, EMPTY_CALL_MAX_RETRIES

    worker = _make_worker()
    worker.llm = MagicMock()

    empty_response = {
        "role": "assistant",
        "content": "",
        "tool_calls": [_empty_args_write_call()],
    }
    worker.llm.call.return_value = empty_response

    tool_executor = MagicMock()

    list(worker.run(
        session_id="test",
        system_prompt="You are a test agent.",
        user_message="Write hello.py",
        tools=[{"name": "write", "type": "function", "function": {}}],
        tool_executor=tool_executor,
        max_iterations=50,
    ))

    call_count = len(worker.llm.call.call_args_list)
    # With early termination, each iteration makes only 1 LLM call
    # (not EMPTY_CALL_MAX_RETRIES + 1 = 4). Total calls should be
    # close to DEGENERATE_CALL_LIMIT (3 iterations × 1 call each).
    # Allow some margin for boundary effects.
    max_expected = DEGENERATE_CALL_LIMIT + 2
    assert call_count <= max_expected, (
        f"Expected ~{DEGENERATE_CALL_LIMIT} LLM calls with early termination, "
        f"got {call_count} (retries not being skipped?)"
    )
    # Without the fix, would be ~(DEGENERATE_CALL_LIMIT * (EMPTY_CALL_MAX_RETRIES + 1))
    # = 3 * 4 = 12 calls
    assert EMPTY_CALL_MAX_RETRIES == 3  # sanity check
