"""Tests for #756: log warning when output_artifacts is truncated.

Verifies that:
1. Truncation of >30 artifacts logs a warning
2. No warning when artifacts <= 30
"""
import json
import logging
from unittest.mock import MagicMock

from core.models import DAG, DAGNode, NodeStatus


def test_truncation_warning_when_over_30_artifacts(caplog):
    """Warning logged when node has >30 output_artifacts (#756)."""
    from orchestrator.intelligent_orchestrator import IntelligentOrchestrator
    from core.config import LLMConfig

    config = LLMConfig(provider="openai", model="gpt-4o", api_key="test-key")
    mock_registry = MagicMock()
    mock_registry.to_prompt_description.return_value = "agent: generator"
    mock_registry.has_agent.return_value = True

    orch = IntelligentOrchestrator(
        llm_config=config,
        session_store=MagicMock(),
        agent_registry=mock_registry,
        prompt_registry=MagicMock(
            load=MagicMock(
                return_value=(
                    "System: {executed_nodes} {failed_node} "
                    "{failed_error} {agent_descriptions}"
                )
            ),
        ),
    )

    # Create node with 35 artifacts
    dag = DAG(reasoning="test")
    node_success = DAGNode(
        id="impl_1",
        agent_type="generator",
        task_description="Create many files",
    )
    node_success.status = NodeStatus.SUCCESS
    node_success.output_artifacts = [f"src/file_{i}.py" for i in range(35)]
    node_success.result = {"summary": "Created 35 files"}

    node_failed = DAGNode(
        id="impl_2",
        agent_type="generator",
        task_description="More work",
    )
    node_failed.status = NodeStatus.FAILED
    node_failed.error = "timeout"

    dag.add_node(node_success)
    dag.add_node(node_failed)

    replan_response = {
        "reasoning": "Retry",
        "nodes": [{"id": "impl_2_retry", "agent_type": "generator", "task": "Try again"}],
        "edges": [],
    }
    orch.llm.call = MagicMock(
        return_value={"content": json.dumps(replan_response), "tool_calls": []}
    )

    import asyncio
    with caplog.at_level(logging.WARNING):
        asyncio.run(orch.replan(dag, "impl_2", "Build feature"))

    assert any("truncating to 30" in r.message for r in caplog.records)
    assert any("#756" in r.message for r in caplog.records)


def test_no_warning_when_under_30_artifacts(caplog):
    """No warning when node has <=30 output_artifacts (#756)."""
    from orchestrator.intelligent_orchestrator import IntelligentOrchestrator
    from core.config import LLMConfig

    config = LLMConfig(provider="openai", model="gpt-4o", api_key="test-key")
    mock_registry = MagicMock()
    mock_registry.to_prompt_description.return_value = "agent: generator"
    mock_registry.has_agent.return_value = True

    orch = IntelligentOrchestrator(
        llm_config=config,
        session_store=MagicMock(),
        agent_registry=mock_registry,
        prompt_registry=MagicMock(
            load=MagicMock(
                return_value=(
                    "System: {executed_nodes} {failed_node} "
                    "{failed_error} {agent_descriptions}"
                )
            ),
        ),
    )

    dag = DAG(reasoning="test")
    node_success = DAGNode(
        id="impl_1",
        agent_type="generator",
        task_description="Create few files",
    )
    node_success.status = NodeStatus.SUCCESS
    node_success.output_artifacts = ["src/a.py", "src/b.py"]
    node_success.result = {"summary": "Created 2 files"}

    node_failed = DAGNode(
        id="impl_2",
        agent_type="generator",
        task_description="More work",
    )
    node_failed.status = NodeStatus.FAILED
    node_failed.error = "timeout"

    dag.add_node(node_success)
    dag.add_node(node_failed)

    replan_response = {
        "reasoning": "Retry",
        "nodes": [{"id": "impl_2_retry", "agent_type": "generator", "task": "Try again"}],
        "edges": [],
    }
    orch.llm.call = MagicMock(
        return_value={"content": json.dumps(replan_response), "tool_calls": []}
    )

    import asyncio
    with caplog.at_level(logging.WARNING):
        asyncio.run(orch.replan(dag, "impl_2", "Build feature"))

    assert not any("truncating" in r.message for r in caplog.records)
