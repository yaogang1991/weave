"""Tests for get_execution_summary including eval_feedback (#665)."""
from core.models import DAG, DAGNode, NodeStatus
from core.dag_engine import DAGExecutionEngine


def _make_engine() -> DAGExecutionEngine:
    """Create a minimal DAGExecutionEngine for summary testing."""
    return DAGExecutionEngine(
        agent_executor=lambda *a, **kw: None,
        failure_handler=lambda *a, **kw: None,
        max_parallel=1,
    )


class TestExecutionSummary:
    def test_eval_feedback_included_in_node_details(self):
        """Evaluator findings are reflected in execution summary (#665)."""
        dag = DAG(reasoning="test")
        dag.add_node(DAGNode(
            id="eval_1",
            agent_type="evaluator",
            task_description="evaluate",
        ))
        dag.update_node(
            "eval_1",
            status=NodeStatus.SUCCESS,
            eval_feedback="anyio not in requirements.txt",
        )

        engine = _make_engine()
        summary = engine.get_execution_summary(dag)

        details = summary["node_details"]["eval_1"]
        assert details["status"] == "success"
        assert details["eval_feedback"] == "anyio not in requirements.txt"

    def test_no_eval_feedback_omitted(self):
        """Nodes without eval_feedback don't include the key."""
        dag = DAG(reasoning="test")
        dag.add_node(DAGNode(
            id="gen_1",
            agent_type="generator",
            task_description="implement",
        ))
        dag.update_node("gen_1", status=NodeStatus.SUCCESS)

        engine = _make_engine()
        summary = engine.get_execution_summary(dag)

        details = summary["node_details"]["gen_1"]
        assert "eval_feedback" not in details

    def test_empty_eval_feedback_omitted(self):
        """Nodes with empty string eval_feedback don't include the key."""
        dag = DAG(reasoning="test")
        dag.add_node(DAGNode(
            id="eval_1",
            agent_type="evaluator",
            task_description="evaluate",
        ))
        dag.update_node("eval_1", status=NodeStatus.SUCCESS, eval_feedback="")

        engine = _make_engine()
        summary = engine.get_execution_summary(dag)

        details = summary["node_details"]["eval_1"]
        assert "eval_feedback" not in details
