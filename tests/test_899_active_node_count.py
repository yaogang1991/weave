"""Tests for #899: DAG explosion protection should count only active nodes.

In cascade failure scenarios, skipped/failed/superseded nodes inflated the
node count and blocked all recovery replans. The fix counts only
pending/running/retrying nodes.
"""
import pytest
from unittest.mock import AsyncMock

from core.models import DAG, DAGNode, NodeStatus, FailureDecision
from core.dag_engine import DAGExecutionEngine


async def _skip_handler(dag, node_id, error):
    return FailureDecision(action="skip")


async def _replan_handler(dag, failed_id):
    """Produces a small replacement DAG with 1 new pending node."""
    new_dag = DAG(reasoning="replan for " + failed_id)
    new_dag.add_node(DAGNode(
        id=f"{failed_id}_r1", agent_type="generator",
        task_description=f"retry {failed_id}",
    ))
    return new_dag


class TestActiveNodeCount:
    """Verify #899: dead nodes don't count toward the explosion limit."""

    @pytest.mark.asyncio
    async def test_skipped_nodes_not_counted(self):
        """Skipped nodes should not inflate the active count."""
        engine = DAGExecutionEngine(
            agent_executor=AsyncMock(return_value={}),
            failure_handler=_skip_handler,
            replan_handler=_replan_handler,
            max_dag_nodes=25,
        )

        # 20 skipped + 2 failed + 3 pending = 25 total
        # Only 3 are active -> replan should succeed
        dag = DAG(reasoning="test")
        for i in range(20):
            dag.add_node(DAGNode(
                id=f"skip_{i}", agent_type="generator",
                task_description=f"skipped {i}",
                status=NodeStatus.SKIPPED,
            ))
        dag.add_node(DAGNode(
            id="fail_1", agent_type="generator",
            task_description="failed 1",
            status=NodeStatus.FAILED,
        ))
        dag.add_node(DAGNode(
            id="fail_2", agent_type="generator",
            task_description="failed 2",
            status=NodeStatus.FAILED,
        ))
        for i in range(3):
            dag.add_node(DAGNode(
                id=f"pending_{i}", agent_type="generator",
                task_description=f"pending {i}",
            ))

        assert len(dag.nodes) == 25

        result = await engine._try_execute_replan(
            dag, "fail_1", [["fail_1"]], 0, 0,
        )
        _, _, _, _, replanned = result
        assert replanned is True

    @pytest.mark.asyncio
    async def test_superseded_nodes_not_counted(self):
        """Superseded nodes should not inflate the active count."""
        engine = DAGExecutionEngine(
            agent_executor=AsyncMock(return_value={}),
            failure_handler=_skip_handler,
            replan_handler=_replan_handler,
            max_dag_nodes=25,
        )

        dag = DAG(reasoning="test")
        for i in range(24):
            dag.add_node(DAGNode(
                id=f"super_{i}", agent_type="generator",
                task_description=f"superseded {i}",
                status=NodeStatus.SUPERSEDED,
            ))
        dag.add_node(DAGNode(
            id="active", agent_type="generator",
            task_description="active node",
        ))

        assert len(dag.nodes) == 25
        result = await engine._try_execute_replan(
            dag, "active", [["active"]], 0, 0,
        )
        _, _, _, _, replanned = result
        assert replanned is True

    @pytest.mark.asyncio
    async def test_genuinely_too_many_active_nodes_still_blocked(self):
        """Real explosion (many active pending nodes) should still be blocked."""
        engine = DAGExecutionEngine(
            agent_executor=AsyncMock(return_value={}),
            failure_handler=_skip_handler,
            replan_handler=_replan_handler,
            max_dag_nodes=10,
        )

        dag = DAG(reasoning="test")
        for i in range(15):
            dag.add_node(DAGNode(
                id=f"active_{i}", agent_type="generator",
                task_description=f"active {i}",
            ))

        result = await engine._try_execute_replan(
            dag, "active_0", [["active_0"]], 0, 0,
        )
        dag_out, _, _, _, replanned = result
        if not replanned:
            assert len(dag_out.nodes) == 15
