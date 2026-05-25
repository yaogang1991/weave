"""Tests for #797: replan replacement nodes must not skip downstream.

When replan generates replacement nodes, _merge_dag_results must preserve
old PENDING nodes as PENDING (not SKIPPED) so that _rewire_replacement_edges
can reconnect them to the replacement and they execute normally.
"""

import pytest
from unittest.mock import AsyncMock, patch

from core.dag_models import DAG, DAGNode, DAGEdge, NodeStatus
from core.dag_engine import DAGExecutionEngine, DAGEngineConfig


def _make_node(nid: str, agent_type: str = "generator") -> DAGNode:
    return DAGNode(id=nid, agent_type=agent_type, task_description=f"task {nid}")


def _linear_dag(*nids: str, agent_types: dict | None = None) -> DAG:
    types = agent_types or {}
    nodes = {nid: _make_node(nid, agent_type=types.get(nid, "generator"))
             for nid in nids}
    edges = [DAGEdge(from_node=nids[i], to_node=nids[i + 1])
             for i in range(len(nids) - 1)]
    return DAG(nodes=nodes, edges=edges)


class TestReplanDownstreamPreserved:
    """Verify PENDING nodes preserved after replan merge."""

    def test_merge_preserves_pending_status(self):
        """PENDING nodes in old DAG should stay PENDING after merge."""
        old_dag = _linear_dag("plan", "impl", "test")
        old_dag.update_node("plan", status=NodeStatus.FAILED)

        new_dag = DAG(
            nodes={"plan_v2": _make_node("plan_v2", "planner")},
            edges=[],
        )

        engine = DAGExecutionEngine(
            agent_executor=AsyncMock(),
            failure_handler=AsyncMock(),
        )
        merged = engine._merge_dag_results(old_dag, new_dag)

        assert merged.nodes["impl"].status == NodeStatus.PENDING
        assert merged.nodes["test"].status == NodeStatus.PENDING
        assert merged.nodes["plan_v2"].status == NodeStatus.PENDING

    def test_merge_preserves_success_status(self):
        """SUCCEEDED nodes should keep their status."""
        old_dag = _linear_dag("plan", "impl", "test")
        old_dag.update_node("plan", status=NodeStatus.SUCCESS)
        old_dag.update_node("impl", status=NodeStatus.SUCCESS)
        old_dag.update_node("test", status=NodeStatus.PENDING)

        new_dag = DAG(
            nodes={"impl_v2": _make_node("impl_v2")},
            edges=[],
        )

        engine = DAGExecutionEngine(
            agent_executor=AsyncMock(),
            failure_handler=AsyncMock(),
        )
        merged = engine._merge_dag_results(old_dag, new_dag)

        assert merged.nodes["plan"].status == NodeStatus.SUCCESS
        assert merged.nodes["impl"].status == NodeStatus.SUCCESS
        assert merged.nodes["test"].status == NodeStatus.PENDING

    @pytest.mark.asyncio
    async def test_replan_rewire_then_no_pending(self):
        """After replan + rewire, no downstream nodes stuck in PENDING."""
        old_dag = _linear_dag(
            "plan", "impl", "test",
            agent_types={"plan": "planner"},
        )

        async def replan_handler(dag, failed_id):
            return DAG(
                nodes={"plan_v2": _make_node("plan_v2", "planner")},
                edges=[],
            )

        engine = DAGExecutionEngine(
        agent_executor=AsyncMock(return_value={"output": "ok"}),
        failure_handler=AsyncMock(return_value=type(
                "D", (), {"action": "replan", "reasoning": "test"}
            )()),
        replan_handler=replan_handler,
        config=DAGEngineConfig(
            enable_watchdog=False,
        ),
    )

        # Make plan fail on first attempt so replan triggers
        original_execute = engine._node_executor.execute_node
        first_call = True

        async def patched_execute(dag, node_id):
            nonlocal first_call
            if first_call and node_id == "plan":
                first_call = False
                dag.update_node(node_id, status=NodeStatus.FAILED, error="timeout")
                return
            await original_execute(dag, node_id)

        with patch.object(
            engine._node_executor, 'execute_node', side_effect=patched_execute,
        ), patch.object(engine, '_emit', new_callable=AsyncMock):
            result_dag = await engine.execute(old_dag)

        assert result_dag.nodes["plan_v2"].status == NodeStatus.SUCCESS
        pending = [nid for nid, n in result_dag.nodes.items()
                   if n.status == NodeStatus.PENDING]
        assert pending == [], f"Nodes stuck in PENDING: {pending}"
