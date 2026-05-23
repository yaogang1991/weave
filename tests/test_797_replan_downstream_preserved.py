"""Tests for #797: replan downstream nodes preserved as PENDING after merge.

Verifies that when replan replaces a failed node, downstream nodes in the
original DAG remain PENDING (not SKIPPED) so they execute after edge rewiring.
"""
import pytest

from core.models import DAG, DAGNode, NodeStatus
from core.dag_models import DAGEdge, DependencyType


def _make_node(nid, agent_type="generator", task="do stuff", **kwargs):
    return DAGNode(id=nid, agent_type=agent_type, task_description=task, **kwargs)


def _make_engine():
    from core.dag_engine import DAGExecutionEngine

    async def noop(*a, **kw):
        return {}

    async def noop_failure(*a, **kw):
        from core.dag_models import FailureDecision
        return FailureDecision(action="skip", reasoning="test")

    return DAGExecutionEngine(
        agent_executor=noop,
        failure_handler=noop_failure,
        max_parallel=1,
    )


class TestReplanDownstreamPreserved:
    """Verify downstream nodes stay PENDING after replan merge (#797)."""

    def test_downstream_nodes_remain_pending_after_merge(self):
        """Downstream PENDING nodes should NOT be marked SKIPPED in merge.

        Original: plan(FAILED) -> impl_a(PENDING) -> impl_b(PENDING)
        Replan: plan_v2(PENDING)
        After merge: impl_a and impl_b should remain PENDING.
        """
        engine = _make_engine()

        old_dag = DAG(reasoning="old")
        old_dag.add_node(_make_node("plan", "planner", "Plan"))
        old_dag.add_node(_make_node("impl_a", "generator", "Impl A"))
        old_dag.add_node(_make_node("impl_b", "generator", "Impl B"))
        old_dag.add_edge("plan", "impl_a")
        old_dag.add_edge("impl_a", "impl_b")
        old_dag.update_node("plan", status=NodeStatus.FAILED, error="timeout")

        new_dag = DAG(reasoning="replan")
        new_dag.add_node(_make_node("plan_v2", "planner", "Plan again"))

        merged = engine._merge_dag_results(old_dag, new_dag)

        assert merged.nodes["plan"].status == NodeStatus.FAILED
        assert merged.nodes["plan_v2"].status == NodeStatus.PENDING
        # #797: downstream nodes must remain PENDING, not SKIPPED
        assert merged.nodes["impl_a"].status == NodeStatus.PENDING
        assert merged.nodes["impl_b"].status == NodeStatus.PENDING

    def test_downstream_nodes_execute_after_rewire(self):
        """After merge + rewire, downstream deps point to replacement."""
        engine = _make_engine()

        old_dag = DAG(reasoning="old")
        old_dag.add_node(_make_node("plan", "planner", "Plan"))
        old_dag.add_node(_make_node("impl", "generator", "Impl"))
        old_dag.add_edge("plan", "impl")
        old_dag.update_node("plan", status=NodeStatus.FAILED, error="timeout")

        new_dag = DAG(reasoning="replan")
        new_dag.add_node(_make_node("plan_v2", "planner", "Plan again"))

        merged = engine._merge_dag_results(old_dag, new_dag)
        engine._rewire_replacement_edges(merged, old_dag, new_dag, "plan")

        # impl should now depend on plan_v2, not plan
        edge_pairs = [(e.from_node, e.to_node) for e in merged.edges]
        assert ("plan_v2", "impl") in edge_pairs
        assert ("plan", "impl") not in edge_pairs

        # impl is PENDING and will become ready once plan_v2 succeeds
        assert merged.nodes["impl"].status == NodeStatus.PENDING

    def test_full_pipeline_preserves_downstream(self):
        """Full merge + rewire for multi-level downstream chain."""
        engine = _make_engine()

        old_dag = DAG(reasoning="old")
        old_dag.add_node(_make_node("plan", "planner", "Plan"))
        old_dag.add_node(_make_node("impl", "generator", "Impl"))
        old_dag.add_node(_make_node("tests", "generator", "Tests"))
        old_dag.add_node(_make_node("eval", "evaluator", "Eval"))
        old_dag.add_edge("plan", "impl")
        old_dag.add_edge("impl", "tests")
        old_dag.add_edge("tests", "eval")
        old_dag.update_node("plan", status=NodeStatus.FAILED, error="timeout")

        new_dag = DAG(reasoning="replan")
        new_dag.add_node(_make_node("plan_v2", "planner", "Plan again"))

        merged = engine._merge_dag_results(old_dag, new_dag)
        engine._rewire_replacement_edges(merged, old_dag, new_dag, "plan")

        # All downstream nodes remain PENDING
        assert merged.nodes["impl"].status == NodeStatus.PENDING
        assert merged.nodes["tests"].status == NodeStatus.PENDING
        assert merged.nodes["eval"].status == NodeStatus.PENDING

        # Edge from plan rewired to plan_v2
        edge_pairs = {(e.from_node, e.to_node) for e in merged.edges}
        assert ("plan_v2", "impl") in edge_pairs
        assert ("plan", "impl") not in edge_pairs
        # Other edges preserved
        assert ("impl", "tests") in edge_pairs
        assert ("tests", "eval") in edge_pairs

    def test_non_pending_nodes_preserved_as_is(self):
        """FAILED/SUCCESS/SKIPPED nodes keep their status in merge."""
        engine = _make_engine()

        old_dag = DAG(reasoning="old")
        old_dag.add_node(_make_node("a", "planner", "A"))
        old_dag.add_node(_make_node("b", "generator", "B"))
        old_dag.add_node(_make_node("c", "generator", "C"))
        old_dag.add_node(_make_node("d", "generator", "D"))
        old_dag.update_node("a", status=NodeStatus.SUCCESS)
        old_dag.update_node("b", status=NodeStatus.FAILED, error="err")
        old_dag.update_node("c", status=NodeStatus.SKIPPED)
        old_dag.update_node("d", status=NodeStatus.PENDING)

        new_dag = DAG(reasoning="replan")
        new_dag.add_node(_make_node("b_v2", "generator", "B again"))

        merged = engine._merge_dag_results(old_dag, new_dag)

        assert merged.nodes["a"].status == NodeStatus.SUCCESS
        assert merged.nodes["b"].status == NodeStatus.FAILED
        assert merged.nodes["c"].status == NodeStatus.SKIPPED
        assert merged.nodes["d"].status == NodeStatus.PENDING
