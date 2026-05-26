"""Tests for #724: execution summary differentiates planning vs implementation.

Verifies:
1. implementation_success only counts generator/worker nodes
2. implementation_total counts all generator/worker nodes
3. Planner success does not inflate implementation_success
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


class TestImplementationSuccessBreakdown:
    """Verify implementation_success/total in summary (#724)."""

    def test_planner_success_not_counted_as_implementation(self):
        """Planner success should not inflate implementation_success."""
        dag = DAG(reasoning="test #724")
        dag.add_node(DAGNode(
            id="plan", agent_type="planner",
            task_description="plan",
        ))
        dag.update_node("plan", status=NodeStatus.SUCCESS)

        engine = _make_engine()
        summary = engine.get_execution_summary(dag)

        assert summary["success"] == 1
        assert summary["implementation_success"] == 0
        assert summary["implementation_total"] == 0

    def test_generator_success_counted_as_implementation(self):
        """Generator success should be counted in implementation_success."""
        dag = DAG(reasoning="test #724")
        dag.add_node(DAGNode(
            id="plan", agent_type="planner",
            task_description="plan",
        ))
        dag.add_node(DAGNode(
            id="gen_1", agent_type="generator",
            task_description="implement",
        ))
        dag.update_node("plan", status=NodeStatus.SUCCESS)
        dag.update_node("gen_1", status=NodeStatus.SUCCESS)

        engine = _make_engine()
        summary = engine.get_execution_summary(dag)

        assert summary["success"] == 2
        assert summary["implementation_success"] == 1
        assert summary["implementation_total"] == 1

    def test_mixed_statuses_implementation_breakdown(self):
        """Implementation total includes all generators regardless of status."""
        dag = DAG(reasoning="test #724")
        dag.add_node(DAGNode(
            id="plan", agent_type="planner",
            task_description="plan",
        ))
        dag.add_node(DAGNode(
            id="gen_1", agent_type="generator",
            task_description="impl feature A",
        ))
        dag.add_node(DAGNode(
            id="gen_2", agent_type="generator",
            task_description="impl feature B",
        ))
        dag.add_node(DAGNode(
            id="gen_3", agent_type="generator",
            task_description="impl feature C",
        ))
        dag.update_node("plan", status=NodeStatus.SUCCESS)
        dag.update_node("gen_1", status=NodeStatus.SUCCESS)
        dag.update_node("gen_2", status=NodeStatus.FAILED)
        dag.update_node("gen_3", status=NodeStatus.SKIPPED)

        engine = _make_engine()
        summary = engine.get_execution_summary(dag)

        assert summary["success"] == 2  # plan + gen_1
        assert summary["implementation_success"] == 1  # gen_1 only
        assert summary["implementation_total"] == 3  # all generators
        assert summary["failed"] == 1
        assert summary["skipped"] == 1

    def test_worker_counted_as_implementation(self):
        """Worker agent type should also be counted as implementation."""
        dag = DAG(reasoning="test #724")
        dag.add_node(DAGNode(
            id="worker_1", agent_type="worker",
            task_description="build",
        ))
        dag.update_node("worker_1", status=NodeStatus.SUCCESS)

        engine = _make_engine()
        summary = engine.get_execution_summary(dag)

        assert summary["implementation_success"] == 1
        assert summary["implementation_total"] == 1

    def test_evaluator_not_counted_as_implementation(self):
        """Evaluator success should not inflate implementation_success."""
        dag = DAG(reasoning="test #724")
        dag.add_node(DAGNode(
            id="eval_1", agent_type="evaluator",
            task_description="evaluate",
        ))
        dag.update_node("eval_1", status=NodeStatus.SUCCESS)

        engine = _make_engine()
        summary = engine.get_execution_summary(dag)

        assert summary["success"] == 1
        assert summary["implementation_success"] == 0
        assert summary["implementation_total"] == 0
