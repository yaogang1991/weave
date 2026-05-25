"""Tests for #829: replan failure should not stop downstream execution.

When the replan handler fails (exception or returns None), the engine
must continue processing remaining failed nodes and downstream levels
instead of returning early with PENDING nodes left behind.
"""

import pytest
from unittest.mock import AsyncMock, patch

from core.dag_models import DAG, DAGNode, DAGEdge, NodeStatus
from core.dag_engine import DAGExecutionEngine, DAGEngineConfig


def _make_node(nid: str, agent_type: str = "generator") -> DAGNode:
    return DAGNode(id=nid, agent_type=agent_type, task_description=f"task {nid}")


def _linear_dag(*nids: str) -> DAG:
    nodes = {nid: _make_node(nid) for nid in nids}
    edges = [DAGEdge(from_node=nids[i], to_node=nids[i + 1])
             for i in range(len(nids) - 1)]
    return DAG(nodes=nodes, edges=edges)


class TestReplanFailureContinues:
    """Verify engine continues after replan handler failure."""

    @pytest.mark.asyncio
    async def test_replan_exception_no_pending_nodes(self):
        """When replan handler raises, no nodes stuck in PENDING."""
        dag = _linear_dag("a", "b", "c")

        async def failing_replan(dag, failed_id):
            raise RuntimeError("replan crashed")

        engine = DAGExecutionEngine(
        agent_executor=AsyncMock(side_effect=Exception("exec fail")),
        failure_handler=AsyncMock(return_value=type(
                "D", (), {"action": "replan", "reasoning": "test"}
            )()),
        replan_handler=failing_replan,
        config=DAGEngineConfig(
            enable_watchdog=False,
        ),
    )

        with patch.object(engine, '_emit', new_callable=AsyncMock):
            result = await engine.execute(dag)

        assert result.nodes["a"].status == NodeStatus.SKIPPED
        pending = [nid for nid, n in result.nodes.items()
                   if n.status == NodeStatus.PENDING]
        assert pending == [], f"Nodes stuck in PENDING: {pending}"

    @pytest.mark.asyncio
    async def test_replan_returns_none_no_pending_nodes(self):
        """When replan handler returns None, no nodes stuck in PENDING."""
        dag = _linear_dag("a", "b", "c")

        async def none_replan(dag, failed_id):
            return None

        engine = DAGExecutionEngine(
        agent_executor=AsyncMock(side_effect=Exception("exec fail")),
        failure_handler=AsyncMock(return_value=type(
                "D", (), {"action": "replan", "reasoning": "test"}
            )()),
        replan_handler=none_replan,
        config=DAGEngineConfig(
            enable_watchdog=False,
        ),
    )

        with patch.object(engine, '_emit', new_callable=AsyncMock):
            result = await engine.execute(dag)

        assert result.nodes["a"].status == NodeStatus.SKIPPED
        pending = [nid for nid, n in result.nodes.items()
                   if n.status == NodeStatus.PENDING]
        assert pending == [], f"Nodes stuck in PENDING: {pending}"
