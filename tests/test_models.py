"""
Tests for core/models.py — DAG models, node status, evaluation results.
"""
import pytest

from core.models import (  # noqa: F401
    DAG, DAGNode, DAGEdge, NodeStatus,
    HandoffArtifact, EvaluationResult, FailureDecision,
    EventType, RiskLevel, PermissionMode,
)


class TestDAGNode:
    def test_defaults(self):
        node = DAGNode(id="n1", agent_type="generator", task_description="do stuff")
        assert node.status == NodeStatus.PENDING
        assert node.output_artifacts == []
        assert node.success_criteria == []
        assert node.eval_feedback == ""
        assert node.max_retries == 3
        assert node.retry_count == 0
        assert node.error == ""

    def test_with_success_criteria(self):
        node = DAGNode(
            id="n1", agent_type="generator", task_description="impl",
            success_criteria=["tests pass", "lint clean"],
        )
        assert node.success_criteria == ["tests pass", "lint clean"]

    def test_auto_id_on_empty(self):
        node = DAGNode(id="", agent_type="generator", task_description="t")
        assert node.id.startswith("node_")


class TestDAG:
    def _make_linear_dag(self):
        dag = DAG(reasoning="test")
        dag.add_node(DAGNode(id="a", agent_type="planner", task_description="plan"))
        dag.add_node(DAGNode(id="b", agent_type="generator", task_description="impl"))
        dag.add_node(DAGNode(id="c", agent_type="evaluator", task_description="eval"))
        dag.add_edge("a", "b")
        dag.add_edge("b", "c")
        return dag

    def test_topological_levels(self):
        dag = self._make_linear_dag()
        levels = dag.topological_levels()
        assert levels == [["a"], ["b"], ["c"]]

    def test_topological_levels_parallel(self):
        dag = DAG()
        dag.add_node(DAGNode(id="a", agent_type="planner", task_description="plan"))
        dag.add_node(DAGNode(id="b", agent_type="generator", task_description="impl1"))
        dag.add_node(DAGNode(id="c", agent_type="generator", task_description="impl2"))
        dag.add_node(DAGNode(id="d", agent_type="evaluator", task_description="eval"))
        dag.add_edge("a", "b")
        dag.add_edge("a", "c")
        dag.add_edge("b", "d")
        dag.add_edge("c", "d")
        levels = dag.topological_levels()
        assert levels[0] == ["a"]
        assert set(levels[1]) == {"b", "c"}
        assert levels[2] == ["d"]

    def test_cycle_detection(self):
        dag = DAG()
        dag.add_node(DAGNode(id="a", agent_type="generator", task_description="a"))
        dag.add_node(DAGNode(id="b", agent_type="generator", task_description="b"))
        dag.add_edge("a", "b")
        dag.add_edge("b", "a")
        with pytest.raises(ValueError, match="Cycle"):
            dag.topological_levels()

    def test_get_dependencies(self):
        dag = self._make_linear_dag()
        assert dag.get_dependencies("a") == []
        assert dag.get_dependencies("b") == ["a"]
        assert dag.get_dependencies("c") == ["b"]

    def test_get_ready_nodes(self):
        dag = self._make_linear_dag()
        assert dag.get_ready_nodes() == ["a"]
        dag.nodes["a"].status = NodeStatus.SUCCESS
        assert dag.get_ready_nodes() == ["b"]

    def test_get_ready_nodes_none_when_all_running(self):
        dag = self._make_linear_dag()
        dag.nodes["a"].status = NodeStatus.RUNNING
        assert dag.get_ready_nodes() == []


class TestEvaluationResult:
    def test_passed(self):
        r = EvaluationResult(passed=True, score=10.0, feedback="OK")
        assert r.passed
        assert r.score == 10.0

    def test_failed(self):
        r = EvaluationResult(passed=False, score=5.0, feedback="Bad", suggestions=["fix X"])
        assert not r.passed
        assert r.suggestions == ["fix X"]


class TestFailureDecision:
    def test_retry(self):
        d = FailureDecision(action="retry", reasoning="transient")
        assert d.action == "retry"

    def test_abort(self):
        d = FailureDecision(action="abort", reasoning="critical")
        assert d.action == "abort"


class TestEnums:
    def test_node_status_values(self):
        assert NodeStatus.PENDING == "pending"
        assert NodeStatus.RETRYING == "retrying"
        assert NodeStatus.SUCCESS == "success"

    def test_event_types(self):
        assert EventType.EVAL_START == "eval.start"
        assert EventType.AGENT_TOOL_USE == "agent.tool_use"

    def test_risk_level_ordering(self):
        assert RiskLevel.LOW < RiskLevel.MEDIUM < RiskLevel.HIGH < RiskLevel.CRITICAL
