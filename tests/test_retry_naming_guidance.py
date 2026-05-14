"""Tests for #311: enhanced retry feedback for naming mismatches.

Verifies that:
1. Generator system prompt includes import verification and async rules
2. Retry feedback detects ImportError and adds naming guidance
3. Retry feedback detects TypeError and adds type error guidance
"""

from __future__ import annotations

from unittest.mock import MagicMock


def test_generator_prompt_has_import_verification_rule():
    """Generator system prompt should include import verification rule."""
    from agent.agent_pool import WorkerAgent

    prompts = WorkerAgent.SYSTEM_PROMPTS
    gen_prompt = prompts["generator"]
    assert "IMPORT VERIFICATION" in gen_prompt
    assert "from mylib.module import ClassName" in gen_prompt


def test_generator_prompt_has_async_rule():
    """Generator system prompt should include async awareness rule."""
    from agent.agent_pool import WorkerAgent

    prompts = WorkerAgent.SYSTEM_PROMPTS
    gen_prompt = prompts["generator"]
    assert "ASYNC AWARENESS" in gen_prompt
    assert "asyncio.run" in gen_prompt


def _make_dag_with_feedback(feedback_text, retry_count=1):
    """Create a minimal DAG with a node that has eval_feedback."""
    from core.models import DAG, DAGNode, NodeStatus

    node = DAGNode(
        id="impl",
        agent_type="generator",
        task_description="Implement the module",
    )
    node.eval_feedback = feedback_text
    node.retry_count = retry_count
    node.status = NodeStatus.RETRYING

    return DAG(
        nodes={"impl": node},
        edges=[],
    )


def test_retry_feedback_detects_import_error():
    """Feedback with ImportError should include naming guidance."""
    from core.dag_engine import DAGExecutionEngine

    dag = _make_dag_with_feedback(
        "FAIL tests: ImportError: cannot import name 'AsyncContext' "
        "from 'tracelib.context'"
    )

    # Call the private method directly via a mock engine
    engine = MagicMock(spec=DAGExecutionEngine)
    from core.dag_engine import DAGExecutionEngine as RealEngine

    # Use the actual _collect_input_artifacts method
    artifacts = RealEngine._collect_input_artifacts(
        engine, dag, "impl",
    )

    # Find the eval_feedback artifact
    feedback_arts = [
        a for a in artifacts
        if hasattr(a, "metadata")
        and a.metadata.get("type") == "eval_feedback"
    ]
    assert len(feedback_arts) == 1
    content = feedback_arts[0].content
    assert "NAMING MISMATCH DETECTED" in content
    assert "READ the source files first" in content


def test_retry_feedback_detects_type_error():
    """Feedback with TypeError should include type error guidance."""
    from core.dag_engine import DAGExecutionEngine

    dag = _make_dag_with_feedback(
        "FAIL tests: TypeError: urlsafe_b64decode() got an "
        "unexpected keyword argument 'validate'"
    )

    engine = MagicMock(spec=DAGExecutionEngine)
    from core.dag_engine import DAGExecutionEngine as RealEngine

    artifacts = RealEngine._collect_input_artifacts(
        engine, dag, "impl",
    )

    feedback_arts = [
        a for a in artifacts
        if hasattr(a, "metadata")
        and a.metadata.get("type") == "eval_feedback"
    ]
    assert len(feedback_arts) == 1
    content = feedback_arts[0].content
    assert "TYPE ERROR DETECTED" in content
    assert "async" in content.lower()


def test_retry_feedback_no_guidance_for_other_errors():
    """Feedback without import/type errors should NOT add guidance."""
    from core.dag_engine import DAGExecutionEngine

    dag = _make_dag_with_feedback(
        "FAIL tests: AssertionError: expected 200, got 404"
    )

    engine = MagicMock(spec=DAGExecutionEngine)
    from core.dag_engine import DAGExecutionEngine as RealEngine

    artifacts = RealEngine._collect_input_artifacts(
        engine, dag, "impl",
    )

    feedback_arts = [
        a for a in artifacts
        if hasattr(a, "metadata")
        and a.metadata.get("type") == "eval_feedback"
    ]
    assert len(feedback_arts) == 1
    content = feedback_arts[0].content
    assert "NAMING MISMATCH DETECTED" not in content
    assert "TYPE ERROR DETECTED" not in content
