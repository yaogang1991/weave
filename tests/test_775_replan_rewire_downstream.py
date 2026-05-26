"""Tests for #775: replan replacement nodes rewire downstream dependencies.

When replan generates a replacement node (e.g., plan_v2 replacing failed plan),
downstream edges must be rewired from the failed original to the replacement
so the pipeline can continue instead of skipping all downstream nodes.
"""
import pytest

from core.models import DAG, DAGNode, NodeStatus
from core.dag_models import DAGEdge, DependencyType

pytestmark = pytest.mark.asyncio(loop_scope="function")


def _make_node(nid, agent_type="generator", task="do stuff", **kwargs):
    return DAGNode(id=nid, agent_type=agent_type, task_description=task, **kwargs)


def _make_engine():
    from core.dag_engine import DAGExecutionEngine, DAGEngineConfig
    from unittest.mock import AsyncMock

    async def noop(*a, **kw):
        return {}

    async def noop_failure(*a, **kw):
        from core.dag_models import FailureDecision
        return FailureDecision(action="skip", reasoning="test")

    return DAGExecutionEngine(
        agent_executor=noop,
        failure_handler=noop_failure,
        config=DAGEngineConfig(
            max_parallel=1,
        ),
    )


# -- Unit tests for _rewire_replacement_edges --


class TestRewireReplacementEdges:
    def test_rewires_single_downstream_edge(self):
        """plan -> impl rewired to plan_v2 -> impl (#775)."""
        engine = _make_engine()

        old_dag = DAG(reasoning="old")
        old_dag.add_node(_make_node("plan", "planner", "Plan"))
        old_dag.add_node(_make_node("impl", "generator", "Implement"))
        old_dag.add_edge("plan", "impl")
        old_dag.update_node("plan", status=NodeStatus.FAILED, error="timeout")

        new_dag = DAG(reasoning="new")
        new_dag.add_node(_make_node("plan_v2", "planner", "Plan again"))
        new_dag.add_node(_make_node("impl", "generator", "Implement"))

        merged = engine._merge_dag_results(old_dag, new_dag)
        engine._rewire_replacement_edges(merged, old_dag, new_dag, "plan")

        edge_pairs = [(e.from_node, e.to_node) for e in merged.edges]
        assert ("plan_v2", "impl") in edge_pairs
        assert ("plan", "impl") not in edge_pairs

    def test_rewires_multiple_downstream_edges(self):
        """plan -> impl_1, plan -> impl_2 both rewired to plan_v2 (#775)."""
        engine = _make_engine()

        old_dag = DAG(reasoning="old")
        old_dag.add_node(_make_node("plan", "planner"))
        old_dag.add_node(_make_node("impl_1", "generator"))
        old_dag.add_node(_make_node("impl_2", "generator"))
        old_dag.add_edge("plan", "impl_1")
        old_dag.add_edge("plan", "impl_2")
        old_dag.update_node("plan", status=NodeStatus.FAILED, error="timeout")

        new_dag = DAG(reasoning="new")
        new_dag.add_node(_make_node("plan_v2", "planner"))
        new_dag.add_node(_make_node("impl_1", "generator"))
        new_dag.add_node(_make_node("impl_2", "generator"))

        merged = engine._merge_dag_results(old_dag, new_dag)
        engine._rewire_replacement_edges(merged, old_dag, new_dag, "plan")

        edge_pairs = [(e.from_node, e.to_node) for e in merged.edges]
        assert ("plan_v2", "impl_1") in edge_pairs
        assert ("plan_v2", "impl_2") in edge_pairs
        assert ("plan", "impl_1") not in edge_pairs
        assert ("plan", "impl_2") not in edge_pairs

    def test_preserves_dependency_type(self):
        """Rewired edge keeps HARD dependency type (#775)."""
        engine = _make_engine()

        old_dag = DAG(reasoning="old")
        old_dag.add_node(_make_node("plan", "planner"))
        old_dag.add_node(_make_node("impl", "generator"))
        old_dag.add_edge("plan", "impl", DependencyType.HARD)
        old_dag.update_node("plan", status=NodeStatus.FAILED, error="x")

        new_dag = DAG(reasoning="new")
        new_dag.add_node(_make_node("plan_v2", "planner"))
        new_dag.add_node(_make_node("impl", "generator"))

        merged = engine._merge_dag_results(old_dag, new_dag)
        engine._rewire_replacement_edges(merged, old_dag, new_dag, "plan")

        edge = next(e for e in merged.edges if e.from_node == "plan_v2")
        assert edge.dependency_type == DependencyType.HARD

    def test_no_rewire_when_no_replacement(self):
        """No rewiring if new DAG has no node with matching agent_type."""
        engine = _make_engine()

        old_dag = DAG(reasoning="old")
        old_dag.add_node(_make_node("plan", "planner"))
        old_dag.add_node(_make_node("impl", "generator"))
        old_dag.add_edge("plan", "impl")
        old_dag.update_node("plan", status=NodeStatus.FAILED, error="x")

        new_dag = DAG(reasoning="new")
        new_dag.add_node(_make_node("impl", "generator"))

        merged = engine._merge_dag_results(old_dag, new_dag)
        edges_before = [(e.from_node, e.to_node) for e in merged.edges]
        engine._rewire_replacement_edges(merged, old_dag, new_dag, "plan")
        edges_after = [(e.from_node, e.to_node) for e in merged.edges]

        assert edges_before == edges_after

    def test_no_duplicate_edges(self):
        """Doesn't add plan_v2 -> impl if that edge already exists."""
        engine = _make_engine()

        old_dag = DAG(reasoning="old")
        old_dag.add_node(_make_node("plan", "planner"))
        old_dag.add_node(_make_node("impl", "generator"))
        old_dag.add_edge("plan", "impl")
        old_dag.update_node("plan", status=NodeStatus.FAILED, error="x")

        new_dag = DAG(reasoning="new")
        new_dag.add_node(_make_node("plan_v2", "planner"))
        new_dag.add_node(_make_node("impl", "generator"))
        new_dag.add_edge("plan_v2", "impl")

        merged = engine._merge_dag_results(old_dag, new_dag)
        engine._rewire_replacement_edges(merged, old_dag, new_dag, "plan")

        impl_edges = [e for e in merged.edges if e.to_node == "impl"]
        assert len(impl_edges) == 1
        assert impl_edges[0].from_node == "plan_v2"

    def test_noop_for_unknown_failed_id(self):
        """No crash when failed_id is not in old_dag."""
        engine = _make_engine()

        old_dag = DAG(reasoning="old")
        new_dag = DAG(reasoning="new")

        # Should not raise
        engine._rewire_replacement_edges(new_dag, old_dag, new_dag, "nonexistent")
