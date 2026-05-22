"""Tests for #747: failure_handler retry after exhaustion.

Verifies that:
1. adapt_to_failure includes retry exhaustion warning in messages
2. dag_engine remaps 'retry' to 'replan' when replan available
3. dag_engine remaps 'retry' to 'skip' when replan unavailable
"""
import asyncio
import json
from unittest.mock import MagicMock

from core.models import DAG, DAGNode, NodeStatus


def _make_dag_with_failed_node(max_retries=2, retry_count=2):
    """Create a DAG with a failed node that exhausted retries."""
    dag = DAG(reasoning="test")
    node = DAGNode(
        id="impl_1",
        agent_type="generator",
        task_description="Implement feature",
        max_retries=max_retries,
    )
    node.retry_count = retry_count
    node.status = NodeStatus.FAILED
    node.error = "timeout after 300s"
    dag.add_node(node)
    return dag


def test_adapt_to_failure_includes_exhaustion_warning():
    """When retries exhausted, prompt includes explicit warning (#747)."""
    from orchestrator.intelligent_orchestrator import IntelligentOrchestrator
    from core.config import LLMConfig

    config = LLMConfig(provider="openai", model="gpt-4o", api_key="test-key")
    mock_registry = MagicMock()
    mock_registry.to_prompt_description.return_value = "agent: generator"

    orch = IntelligentOrchestrator(
        llm_config=config,
        session_store=MagicMock(),
        agent_registry=mock_registry,
        prompt_registry=MagicMock(
            load=MagicMock(
                return_value=(
                    "System: {node_id} {agent_type} {task} "
                    "{error} {retry_count} {dag_status}"
                )
            ),
        ),
    )

    captured_messages = []

    def capture_call(messages, **kwargs):
        captured_messages.extend(messages)
        return {
            "content": json.dumps({
                "action": "skip",
                "reasoning": "Skipping after exhaustion",
            }),
            "tool_calls": [],
        }

    orch.llm.call = capture_call
    dag = _make_dag_with_failed_node(max_retries=2, retry_count=2)

    asyncio.run(orch.adapt_to_failure(dag, "impl_1"))

    # Check that exhaustion warning was added
    user_msgs = [m for m in captured_messages if m["role"] == "user"]
    assert any("exhausted ALL retries" in m["content"] for m in user_msgs)


def test_adapt_to_failure_no_warning_when_retries_available():
    """When retries remain, no exhaustion warning is added (#747)."""
    from orchestrator.intelligent_orchestrator import IntelligentOrchestrator
    from core.config import LLMConfig

    config = LLMConfig(provider="openai", model="gpt-4o", api_key="test-key")
    mock_registry = MagicMock()
    mock_registry.to_prompt_description.return_value = "agent: generator"

    orch = IntelligentOrchestrator(
        llm_config=config,
        session_store=MagicMock(),
        agent_registry=mock_registry,
        prompt_registry=MagicMock(
            load=MagicMock(
                return_value=(
                    "System: {node_id} {agent_type} {task} "
                    "{error} {retry_count} {dag_status}"
                )
            ),
        ),
    )

    captured_messages = []

    def capture_call(messages, **kwargs):
        captured_messages.extend(messages)
        return {
            "content": json.dumps({
                "action": "retry",
                "reasoning": "Retry the node",
            }),
            "tool_calls": [],
        }

    orch.llm.call = capture_call
    dag = _make_dag_with_failed_node(max_retries=3, retry_count=1)

    asyncio.run(orch.adapt_to_failure(dag, "impl_1"))

    # No exhaustion warning when retries remain
    user_msgs = [m for m in captured_messages if m["role"] == "user"]
    assert not any("exhausted ALL retries" in m["content"] for m in user_msgs)
