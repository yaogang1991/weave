"""Tests for #829: replan failure should continue processing, not exit early.

When decision.action == "replan" but the replan handler fails or returns
an invalid DAG, the engine must continue processing the level (not return
immediately). Otherwise downstream nodes are left in PENDING forever.
"""
import pytest

from core.models import (
    DAG, DAGNode, NodeStatus, FailureDecision,
)
from core.dag_engine import DAGExecutionEngine


async def _replan_decision_handler(dag, node_id, error):
    """Failure handler that always recommends replan."""
    return FailureDecision(action="replan", reasoning="test replan")


class TestReplanFailContinues:
    """When replan handler fails, engine continues instead of returning."""

    @pytest.mark.asyncio
    async def test_replan_exception_continues_to_downstream(self):
        """Replan handler raises -> engine skips failed node, processes downstream."""
        dag = DAG(reasoning="test")
        dag.add_node(DAGNode(
            id="a", agent_type="generator", task_description="impl",
        ))
        dag.add_node(DAGNode(
            id="b", agent_type="generator", task_description="impl2",
        ))
        dag.add_node(DAGNode(
            id="c", agent_type="evaluator", task_description="eval",
        ))
        dag.add_edge("a", "c")

        async def selective_executor(node, artifacts, **kwargs):
            if node.id == "a":
                raise RuntimeError("A fails")
            return {"status": "completed", "artifacts": []}

        async def failing_replan_handler(dag_ref, failed_id):
            raise RuntimeError("replan handler crashed")

        engine = DAGExecutionEngine(
            selective_executor,
            _replan_decision_handler,
            replan_handler=failing_replan_handler,
            enable_watchdog=False,
        )
        result = await engine.execute(dag)

        assert result.nodes["a"].status == NodeStatus.SKIPPED
        assert result.nodes["b"].status == NodeStatus.SUCCESS
        assert result.nodes["c"].status == NodeStatus.SKIPPED

    @pytest.mark.asyncio
    async def test_replan_returns_none_continues(self):
        """Replan handler returns invalid result -> engine continues."""
        dag = DAG(reasoning="test")
        dag.add_node(DAGNode(
            id="a", agent_type="generator", task_description="impl",
        ))
        dag.add_node(DAGNode(
            id="b", agent_type="generator", task_description="independent",
        ))

        async def failing_executor_selective(node, artifacts, **kwargs):
            if node.id == "a":
                raise RuntimeError("A fails")
            return {"status": "completed", "artifacts": []}

        async def replan_handler_none(dag_ref, failed_id):
            return None

        engine = DAGExecutionEngine(
            failing_executor_selective,
            _replan_decision_handler,
            replan_handler=replan_handler_none,
            enable_watchdog=False,
        )
        result = await engine.execute(dag)

        assert result.nodes["a"].status == NodeStatus.SKIPPED
        assert result.nodes["b"].status == NodeStatus.SUCCESS
