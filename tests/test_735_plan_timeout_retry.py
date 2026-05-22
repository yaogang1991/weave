"""Tests for #735: orchestrator plan() retries on LLM hard timeout.

Verifies that:
1. plan() retries on TimeoutError (parent of _HardTimeoutError)
2. plan() returns successfully if retry succeeds
3. plan() raises TimeoutError after exhausting retries
4. plan() does NOT retry non-timeout errors (ValueError, etc.)
"""
import pytest
from unittest.mock import MagicMock, patch

pytestmark = pytest.mark.asyncio(loop_scope="function")

from core.llm_client import _HardTimeoutError


def _make_orchestrator():
    """Create an IntelligentOrchestrator with mocked dependencies."""
    from orchestrator.intelligent_orchestrator import IntelligentOrchestrator
    from core.config import LLMConfig

    config = LLMConfig(provider="openai", model="gpt-4o", api_key="test-key")
    mock_session = MagicMock()
    mock_registry = MagicMock()
    mock_registry.to_prompt_description.return_value = "agent: planner"
    mock_registry.has_agent.return_value = True

    orch = IntelligentOrchestrator(
        llm_config=config,
        session_store=mock_session,
        agent_registry=mock_registry,
        prompt_registry=MagicMock(
            load=MagicMock(return_value="System: {agent_descriptions}")
        ),
    )
    return orch


def _valid_plan_json():
    """Return a minimal valid plan JSON string."""
    return (
        '{"nodes": [{"id": "plan_1", "agent_type": "planner", '
        '"task_description": "Plan the project"}], '
        '"edges": [], "reasoning": "test"}'
    )


async def test_plan_retries_on_hard_timeout_and_succeeds():
    """plan() retries on _HardTimeoutError and succeeds on second attempt."""
    orch = _make_orchestrator()

    call_count = 0

    def mock_call(messages, tools=None, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise _HardTimeoutError("LLM call exceeded hard timeout of 150s")
        return {"content": _valid_plan_json(), "tool_calls": []}

    orch.llm.call = mock_call

    # Patch PlanValidator to auto-fix, and _estimate_dag_tokens to no-op
    with patch(
        "orchestrator.intelligent_orchestrator.PlanValidator"
    ) as MockValidator:
        mock_validator = MagicMock()
        mock_validator.validate.return_value = {
            "nodes": [{"id": "plan_1", "agent_type": "planner",
                       "task_description": "Plan"}],
            "edges": [],
            "reasoning": "test",
        }
        mock_validator.warnings = []
        MockValidator.return_value = mock_validator

        dag = await orch.plan("Build a REST API")

    assert dag is not None
    assert call_count == 2  # Failed once, succeeded on retry


async def test_plan_raises_after_exhausting_retries():
    """plan() raises TimeoutError after all retries exhausted."""
    orch = _make_orchestrator()

    def mock_call(messages, tools=None, **kwargs):
        raise _HardTimeoutError("LLM call exceeded hard timeout of 150s")

    orch.llm.call = mock_call

    with pytest.raises(TimeoutError, match="hard timeout"):
        await orch.plan("Build a REST API")


async def test_plan_does_not_retry_non_timeout_errors():
    """plan() does NOT retry on non-TimeoutError exceptions.

    _plan_structured_output catches all exceptions and returns None,
    then _plan_free_text calls llm.call again and raises ValueError.
    Both LLM calls happen within a single plan attempt (no timeout retry).
    """
    orch = _make_orchestrator()

    call_count = 0

    def mock_call(messages, tools=None, **kwargs):
        nonlocal call_count
        call_count += 1
        raise ValueError("Some non-timeout error")

    orch.llm.call = mock_call

    with pytest.raises(ValueError, match="non-timeout"):
        await orch.plan("Build a REST API")
    # 2 calls: _plan_structured_output (swallowed) + _plan_free_text (raised)
    # No timeout retry loop — both are within the same attempt
    assert call_count == 2


async def test_plan_succeeds_first_attempt_no_retry():
    """plan() returns successfully on first attempt without retry."""
    orch = _make_orchestrator()

    call_count = 0

    def mock_call(messages, tools=None, **kwargs):
        nonlocal call_count
        call_count += 1
        return {"content": _valid_plan_json(), "tool_calls": []}

    orch.llm.call = mock_call

    with patch(
        "orchestrator.intelligent_orchestrator.PlanValidator"
    ) as MockValidator:
        mock_validator = MagicMock()
        mock_validator.validate.return_value = {
            "nodes": [{"id": "plan_1", "agent_type": "planner",
                       "task_description": "Plan"}],
            "edges": [],
            "reasoning": "test",
        }
        mock_validator.warnings = []
        MockValidator.return_value = mock_validator

        dag = await orch.plan("Build a REST API")

    assert dag is not None
    # _plan_structured_output calls once (returns None for no tool_calls),
    # then _plan_free_text calls once — total 2 LLM calls, but no timeout retries
    assert call_count == 2
