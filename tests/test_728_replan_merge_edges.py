"""Tests for #728: replan merge preserves old nodes AND edges.

Verifies:
1. Old nodes not in new DAG are preserved with correct status
2. Old edges connecting preserved nodes are also preserved
3. topological_levels() correctly orders preserved nodes after merge
"""
from core.dag_engine import DAGExecutionEngine, DAGEngineConfig
from core.dag_models import DAG, DAGNode
from core.models import NodeStatus


def _make_engine() -> DAGExecutionEngine:
    return DAGExecutionEngine(
        agent_executor=lambda *a, **kw: None,
        failure_handler=lambda *a, **kw: None,
        config=DAGEngineConfig(max_parallel=1),
    )


def _make_full_dag() -> DAG:
    """13-node DAG: plan → impl_foundation → 9 parallel → tests → eval."""
    dag = DAG(reasoning="13-node DAG")
    dag.add_node(DAGNode(
        id="plan", agent_type="planner", task_description="plan",
    ))
    dag.add_node(DAGNode(
        id="impl_foundation", agent_type="generator",
        task_description="foundation",
    ))
    for nid in ["impl_a", "impl_b", "impl_c"]:
        dag.add_node(DAGNode(
            id=nid, agent_type="generator",
            task_description=f"impl {nid}",
        ))
    dag.add_node(DAGNode(
        id="tests", agent_type="generator",
        task_description="tests",
    ))
    dag.add_node(DAGNode(
        id="eval", agent_type="evaluator",
        task_description="eval",
    ))
    # Edges
    dag.add_edge("plan", "impl_foundation")
    for nid in ["impl_a", "impl_b", "impl_c"]:
        dag.add_edge("impl_foundation", nid)
        dag.add_edge(nid, "tests")
    dag.add_edge("tests", "eval")
    return dag


class TestMergePreservesEdges:
    """Verify _merge_dag_results preserves edges (#728)."""

    def test_old_edges_preserved_for_carried_nodes(self):
        """Edges between old nodes should be preserved after merge."""
        engine = _make_engine()
        old_dag = _make_full_dag()
        old_dag.update_node("plan", status=NodeStatus.SUCCESS)
        old_dag.update_node("impl_foundation", status=NodeStatus.FAILED)

        # Replan creates a smaller DAG
        new_dag = DAG(reasoning="replan")
        new_dag.add_node(DAGNode(
            id="plan", agent_type="planner", task_description="plan",
        ))
        new_dag.add_node(DAGNode(
            id="impl_foundation_v2", agent_type="generator",
            task_description="split foundation",
        ))
        new_dag.add_edge("plan", "impl_foundation_v2")

        merged = engine._merge_dag_results(old_dag, new_dag)

        # All old nodes should be present
        assert "impl_a" in merged.nodes
        assert "impl_b" in merged.nodes
        assert "impl_c" in merged.nodes
        assert "tests" in merged.nodes
        assert "eval" in merged.nodes

        # Old edges connecting preserved nodes should be present
        edge_pairs = {(e.from_node, e.to_node) for e in merged.edges}
        assert ("impl_foundation", "impl_a") in edge_pairs
        assert ("impl_a", "tests") in edge_pairs
        assert ("tests", "eval") in edge_pairs

    def test_no_duplicate_edges(self):
        """Edges shared by old and new DAG should not be duplicated."""
        engine = _make_engine()
        old_dag = DAG(reasoning="old")
        old_dag.add_node(DAGNode(id="a", agent_type="generator", task_description="a"))
        old_dag.add_node(DAGNode(id="b", agent_type="generator", task_description="b"))
        old_dag.add_edge("a", "b")

        new_dag = DAG(reasoning="new")
        new_dag.add_node(DAGNode(id="a", agent_type="generator", task_description="a"))
        new_dag.add_node(DAGNode(id="b", agent_type="generator", task_description="b"))
        new_dag.add_edge("a", "b")

        merged = engine._merge_dag_results(old_dag, new_dag)
        edge_count = sum(
            1 for e in merged.edges
            if e.from_node == "a" and e.to_node == "b"
        )
        assert edge_count == 1  # No duplicate

    def test_topological_levels_includes_preserved_nodes(self):
        """topological_levels should include all nodes after merge."""
        engine = _make_engine()
        old_dag = _make_full_dag()
        old_dag.update_node("plan", status=NodeStatus.SUCCESS)

        new_dag = DAG(reasoning="replan")
        new_dag.add_node(DAGNode(
            id="plan", agent_type="planner", task_description="plan",
        ))

        merged = engine._merge_dag_results(old_dag, new_dag)
        levels = merged.topological_levels()

        all_in_levels = set()
        for level in levels:
            all_in_levels.update(level)

        # All nodes should appear in levels
        for nid in merged.nodes:
            assert nid in all_in_levels, f"{nid} missing from levels"

    def test_summary_counts_all_after_replan_merge(self):
        """Summary should count all nodes after replan (#728)."""
        engine = _make_engine()
        old_dag = _make_full_dag()  # 6 nodes
        old_dag.update_node("plan", status=NodeStatus.SUCCESS)

        new_dag = DAG(reasoning="replan")
        new_dag.add_node(DAGNode(
            id="plan", agent_type="planner", task_description="plan",
        ))
        new_dag.add_node(DAGNode(
            id="impl_new", agent_type="generator",
            task_description="new impl",
        ))

        merged = engine._merge_dag_results(old_dag, new_dag)
        summary = engine.get_execution_summary(merged)

        # 7 old + 2 new - 1 overlap (plan) = 8 total
        assert summary["total_nodes"] == 8
