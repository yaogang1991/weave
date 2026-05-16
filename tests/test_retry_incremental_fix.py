"""Tests for #353: incremental fix on retry, not full rewrite.

When a generator retries after evaluation failure, it should be
instructed to fix ONLY the specific failing tests, not rewrite
everything from scratch.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from core.models import HandoffArtifact
from session.store import SessionStore


def _make_agent():
    """Create a WorkerAgent with mocked deps."""
    from core.config import LLMConfig
    from core.agent_registry import AgentCapability
    from agent.agent_pool import WorkerAgent

    config = LLMConfig(api_key="test", model="test")
    store = MagicMock(spec=SessionStore)
    tool_reg = MagicMock()
    tool_reg.schemas = []
    tool_reg.base_cwd = None
    cap = AgentCapability(
        id="generator",
        name="Generator",
        description="Generates code",
    )
    return WorkerAgent(
        capability=cap,
        llm_config=config,
        session_store=store,
        tool_registry=tool_reg,
    )


def test_retry_includes_incremental_fix_rules():
    """Retry instruction should mention incremental fixes, not full rewrite."""
    import asyncio

    agent = _make_agent()

    artifacts = [
        HandoffArtifact(
            from_agent="evaluator",
            to_agent="generator",
            content="FAIL test_x: expected 200, got 404",
            metadata={"type": "eval_feedback", "attempt": 2},
        ),
    ]

    captured_prompt = {}

    async def mock_run(prompt, session_id, context=None, **kwargs):
        captured_prompt["prompt"] = prompt
        return {
            "status": "completed",
            "summary": "done",
            "artifacts": [],
            "output": "done",
        }

    agent._run_with_tools = mock_run

    asyncio.run(
        agent._execute_inner(
            task="Fix the failing tests",
            input_artifacts=artifacts,
            session_id="test",
            node_id="impl",
        )
    )

    prompt = captured_prompt["prompt"]
    assert "INCREMENTAL FIX RULES" in prompt
    assert "Do NOT rewrite files from scratch" in prompt
    assert "EDIT tool" in prompt
    assert "PASSING" in prompt


def test_no_retry_instruction_without_eval_feedback():
    """Without eval_feedback, no retry instruction should be injected."""
    import asyncio

    agent = _make_agent()

    captured_prompt = {}

    async def mock_run(prompt, session_id, context=None, **kwargs):
        captured_prompt["prompt"] = prompt
        return {
            "status": "completed",
            "summary": "done",
            "artifacts": [],
            "output": "done",
        }

    agent._run_with_tools = mock_run

    asyncio.run(
        agent._execute_inner(
            task="Write tests",
            input_artifacts=[],
            session_id="test",
            node_id="impl",
        )
    )

    prompt = captured_prompt["prompt"]
    assert "INCREMENTAL FIX RULES" not in prompt


def test_retry_instruction_mentions_edit_tool():
    """Retry instruction should specifically mention using EDIT not WRITE."""
    import asyncio

    agent = _make_agent()

    artifacts = [
        HandoffArtifact(
            from_agent="evaluator",
            to_agent="generator",
            content="FAIL: 2 tests failed",
            metadata={"type": "eval_feedback", "attempt": 1},
        ),
    ]

    captured_prompt = {}

    async def mock_run(prompt, session_id, context=None, **kwargs):
        captured_prompt["prompt"] = prompt
        return {
            "status": "completed",
            "summary": "done",
            "artifacts": [],
            "output": "done",
        }

    agent._run_with_tools = mock_run

    asyncio.run(
        agent._execute_inner(
            task="Fix tests",
            input_artifacts=artifacts,
            session_id="test",
            node_id="impl",
        )
    )

    prompt = captured_prompt["prompt"]
    assert "EDIT tool" in prompt
