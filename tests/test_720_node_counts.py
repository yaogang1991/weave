"""Tests for #720: execution summary shows correct node counts after replan.

Verifies that _merge_dag_results preserves old DAG nodes that are not in
the new DAG, so the summary counts all nodes not just the replan subset.
"""
from core.dag_engine import DAGExecutionEngine
from core.dag_models import DAG, DAGNode
from core.models import NodeStatus


def _make_engine() -> DAGExecutionEngine:
    """Create a minimal engine for merge testing."""
    return DAGExecutionEngine(
        agent_executor=lambda *a, **kw: None,
        failure_handler=lambda *a, **kw: None,
        max_parallel=1,
    )


def _make_11_node_dag() -> DAG:
    """Create a DAG with 11 nodes simulating the issue scenario."""
    dag = DAG(reasoning="11-node DAG")
    nodes = [
        ("plan", "planner"),
        ("impl_foundation", "generator"),
        ("impl_tasks", "generator"),
        ("impl_auth", "generator"),
        ("impl_ws", "generator"),
        ("impl_taskqueue", "generator"),
        ("test_foundation", "generator"),
        ("test_tasks", "generator"),
        ("test_auth_ws", "generator"),
        ("test_taskqueue", "generator"),
        ("eval", "evaluator"),
    ]
    for nid, atype in nodes:
        dag.add_node(DAGNode(id=nid, agent_type=atype, task_description=nid))
    return dag


class TestMergePreservesOldNodes:
    """Verify _merge_dag_results preserves all old nodes (#720)."""

    def test_old_nodes_preserved_in_merge(self):
        """Nodes from old DAG that are not in new DAG are preserved."""
        engine = _make_engine()
        old_dag = _make_11_node_dag()

        # Simulate some nodes succeeded, some failed, rest pending
        old_dag.update_node("plan", status=NodeStatus.SUCCESS)
        old_dag.update_node("impl_foundation", status=NodeStatus.SUCCESS)
        old_dag.update_node("impl_ws", status=NodeStatus.SUCCESS)
        old_dag.update_node("impl_tasks", status=NodeStatus.FAILED)

        # New (replan) DAG has only 5 nodes
        new_dag = DAG(reasoning="replan")
        for nid in ["plan", "impl_foundation", "impl_ws", "impl_retry", "eval"]:
            new_dag.add_node(DAGNode(
                id=nid, agent_type="generator",
                task_description=nid,
            ))

        merged = engine._merge_dag_results(old_dag, new_dag)

        # Should have all nodes from both DAGs
        # 11 old + 5 new - 4 overlap (plan, impl_foundation, impl_ws, eval)
        assert len(merged.nodes) == 12
        # Old nodes that succeeded should be preserved
        assert merged.nodes["plan"].status == NodeStatus.SUCCESS
        assert merged.nodes["impl_foundation"].status == NodeStatus.SUCCESS
        # Old nodes not in new DAG: FAILED stays FAILED
        assert merged.nodes["impl_tasks"].status == NodeStatus.FAILED
        # Old pending nodes not in new DAG become SKIPPED
        assert merged.nodes["impl_auth"].status == NodeStatus.SKIPPED
        assert merged.nodes["impl_taskqueue"].status == NodeStatus.SKIPPED

    def test_summary_counts_all_nodes(self):
        """get_execution_summary should count all nodes after replan merge."""
        engine = _make_engine()
        old_dag = _make_11_node_dag()
        old_dag.update_node("plan", status=NodeStatus.SUCCESS)
        old_dag.update_node("impl_foundation", status=NodeStatus.SUCCESS)
        old_dag.update_node("impl_ws", status=NodeStatus.SUCCESS)

        new_dag = DAG(reasoning="replan")
        for nid in ["plan", "impl_foundation", "impl_ws", "impl_retry", "eval"]:
            new_dag.add_node(DAGNode(
                id=nid, agent_type="generator",
                task_description=nid,
            ))

        merged = engine._merge_dag_results(old_dag, new_dag)
        summary = engine.get_execution_summary(merged)

        # Should count ALL nodes, not just the 5 from new_dag
        # 11 old + 5 new - 4 overlap = 12
        assert summary["total_nodes"] == 12
        assert summary["success"] == 3  # plan, impl_foundation, impl_ws

    def test_merge_preserves_success_in_both_dags(self):
        """Node in both DAGs: success state from old DAG is preserved."""
        engine = _make_engine()
        old_dag = DAG(reasoning="old")
        old_dag.add_node(DAGNode(
            id="plan", agent_type="planner",
            task_description="plan",
        ))
        old_dag.update_node("plan", status=NodeStatus.SUCCESS)

        new_dag = DAG(reasoning="new")
        new_dag.add_node(DAGNode(
            id="plan", agent_type="planner",
            task_description="plan",
        ))

        merged = engine._merge_dag_results(old_dag, new_dag)
        assert merged.nodes["plan"].status == NodeStatus.SUCCESS
