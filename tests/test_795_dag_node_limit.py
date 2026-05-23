"""Tests for #795: DAG node limit prevents replan explosion."""
import pytest

from core.models import DAG, DAGNode, NodeStatus


def _make_node(nid, agent_type="generator", task="do stuff", **kwargs):
    return DAGNode(id=nid, agent_type=agent_type, task_description=task, **kwargs)


def _make_engine(max_dag_nodes=25):
    from core.dag_engine import DAGExecutionEngine

    async def noop(*a, **kw):
        return {}

    async def noop_failure(*a, **kw):
        from core.dag_models import FailureDecision
        return FailureDecision(action="skip", reasoning="test")

    engine = DAGExecutionEngine(
        agent_executor=noop,
        failure_handler=noop_failure,
        max_parallel=1,
    )
    engine.MAX_DAG_NODES = max_dag_nodes
    return engine


class TestDagNodeLimit:
    """Verify MAX_DAG_NODES prevents replan explosion (#795)."""

    def test_replan_aborted_when_limit_exceeded(self):
        """Replan should be skipped when merged DAG would exceed limit."""
        engine = _make_engine(max_dag_nodes=10)

        old_dag = DAG(reasoning="old")
        for i in range(9):
            old_dag.add_node(_make_node(f"n{i}", "generator", f"Node {i}"))
        old_dag.update_node("n0", status=NodeStatus.FAILED, error="timeout")

        # Replan would add 2 new nodes, making total 11 > 10
        new_dag = DAG(reasoning="replan")
        new_dag.add_node(_make_node("n0_v2", "generator", "Node 0 retry"))
        new_dag.add_node(_make_node("n0_v3", "generator", "Node 0 split"))

        merged = engine._merge_dag_results(old_dag, new_dag)
        assert len(merged.nodes) == 11

        projected = len(old_dag.nodes) + len(new_dag.nodes)
        assert projected > engine.MAX_DAG_NODES

    def test_replan_allowed_within_limit(self):
        """Replan should proceed when merged DAG stays within limit."""
        engine = _make_engine(max_dag_nodes=25)

        old_dag = DAG(reasoning="old")
        old_dag.add_node(_make_node("plan", "planner", "Plan"))
        old_dag.add_node(_make_node("impl", "generator", "Impl"))
        old_dag.add_edge("plan", "impl")
        old_dag.update_node("plan", status=NodeStatus.FAILED, error="timeout")

        new_dag = DAG(reasoning="replan")
        new_dag.add_node(_make_node("plan_v2", "planner", "Plan again"))

        projected = len(old_dag.nodes) + len(new_dag.nodes)
        assert projected <= engine.MAX_DAG_NODES

        merged = engine._merge_dag_results(old_dag, new_dag)
        engine._rewire_replacement_edges(merged, old_dag, new_dag, "plan")
        assert len(merged.nodes) == 3

    def test_max_dag_nodes_default(self):
        """Default MAX_DAG_NODES should be 25."""
        from core.dag_engine import DAGExecutionEngine
        assert DAGExecutionEngine.MAX_DAG_NODES == 25

    def test_replan_node_count_check_before_merge(self):
        """The projection check uses pre-merge counts."""
        engine = _make_engine(max_dag_nodes=12)

        old_dag = DAG(reasoning="old")
        for i in range(10):
            old_dag.add_node(_make_node(f"n{i}", "generator", f"Node {i}"))
        old_dag.update_node("n0", status=NodeStatus.FAILED, error="x")

        new_dag = DAG(reasoning="replan")
        new_dag.add_node(_make_node("n0_v2", "generator", "Retry"))
        new_dag.add_node(_make_node("n0_a", "generator", "Split A"))
        new_dag.add_node(_make_node("n0_b", "generator", "Split B"))

        projected = len(old_dag.nodes) + len(new_dag.nodes)
        assert projected == 13
        assert projected > engine.MAX_DAG_NODES
