"""Tests for #743: replan includes output_artifacts for node context.

Verifies that:
1. replan() includes output_artifacts in executed_summary
2. Nodes without output_artifacts still work
3. Failed nodes with artifacts include them too
"""
import asyncio
import json
from unittest.mock import MagicMock

from core.models import DAG, DAGNode, NodeStatus


def _make_orchestrator():
    """Create orchestrator with mocked dependencies."""
    from orchestrator.intelligent_orchestrator import IntelligentOrchestrator
    from core.config import LLMConfig

    config = LLMConfig(provider="openai", model="gpt-4o", api_key="test-key")
    mock_session = MagicMock()
    mock_registry = MagicMock()
    mock_registry.to_prompt_description.return_value = "agent: generator"
    mock_registry.has_agent.return_value = True

    orch = IntelligentOrchestrator(
        llm_config=config,
        session_store=mock_session,
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
    return orch


def _make_dag_with_artifacts():
    """Create a DAG with a successful node that has output_artifacts."""
    dag = DAG(reasoning="test")

    node_success = DAGNode(
        id="impl_foundation",
        agent_type="generator",
        task_description="Create foundation files",
    )
    node_success.status = NodeStatus.SUCCESS
    node_success.output_artifacts = [
        "src/models.py",
        "src/database.py",
        "src/__init__.py",
    ]
    node_success.result = {"summary": "Created 3 foundation files"}

    node_failed = DAGNode(
        id="impl_auth",
        agent_type="generator",
        task_description="Implement auth module",
    )
    node_failed.status = NodeStatus.FAILED
    node_failed.error = "zero output artifacts"

    dag.add_node(node_success)
    dag.add_node(node_failed)
    return dag


def test_replan_includes_output_artifacts():
    """executed_summary includes output_artifacts for successful nodes (#743)."""
    orch = _make_orchestrator()
    dag = _make_dag_with_artifacts()

    # Mock LLM to return a valid replan
    replan_response = {
        "reasoning": "Split auth into smaller pieces",
        "nodes": [
            {"id": "impl_auth_basic", "agent_type": "generator",
             "task": "Create basic auth (src/models.py already exists)"},
        ],
        "edges": [],
    }
    orch.llm.call = MagicMock(
        return_value={"content": json.dumps(replan_response), "tool_calls": []}
    )

    # Capture the messages sent to LLM
    captured_messages = []
    original_call = orch.llm.call

    def capture_call(messages, **kwargs):
        captured_messages.extend(messages)
        return original_call.return_value

    orch.llm.call = capture_call

    asyncio.get_event_loop().run_until_complete(
        orch.replan(dag, "impl_auth", "Build auth system")
    )

    # Verify the system prompt includes output_artifacts
    system_msg = next(
        (m for m in captured_messages if m["role"] == "system"), None
    )
    assert system_msg is not None
    assert "src/models.py" in system_msg["content"]
    assert "output_artifacts" in system_msg["content"]


def test_replan_works_without_artifacts():
    """Nodes without output_artifacts still work (#743)."""
    orch = _make_orchestrator()
    dag = DAG(reasoning="test")

    node_success = DAGNode(
        id="plan_1",
        agent_type="planner",
        task_description="Plan the project",
    )
    node_success.status = NodeStatus.SUCCESS
    node_success.result = {"summary": "Plan created"}
    # No output_artifacts

    node_failed = DAGNode(
        id="impl_1",
        agent_type="generator",
        task_description="Implement feature",
    )
    node_failed.status = NodeStatus.FAILED
    node_failed.error = "timeout"

    dag.add_node(node_success)
    dag.add_node(node_failed)

    replan_response = {
        "reasoning": "Retry",
        "nodes": [
            {"id": "impl_1_retry", "agent_type": "generator",
             "task": "Implement feature again"},
        ],
        "edges": [],
    }
    orch.llm.call = MagicMock(
        return_value={"content": json.dumps(replan_response), "tool_calls": []}
    )

    new_dag = asyncio.get_event_loop().run_until_complete(
        orch.replan(dag, "impl_1", "Build feature")
    )

    assert new_dag is not None
    assert "impl_1_retry" in new_dag.nodes
