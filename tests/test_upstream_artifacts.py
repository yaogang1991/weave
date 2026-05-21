"""Tests for _collect_upstream_artifacts semantics (#658).

Verifies that _collect_upstream_artifacts only collects from direct
(predecessor) dependencies, not transitive ones.
"""
from core.dag_models import DAG, DAGNode, DAGEdge, DependencyType, NodeStatus
from core.evaluation_pipeline import EvaluationPipeline


def _make_linear_dag():
    """Create DAG: A -> B -> C (all hard edges)."""
    dag = DAG(reasoning="test")
    dag.add_node(DAGNode(id="A", agent_type="generator", task_description="step A"))
    dag.add_node(DAGNode(id="B", agent_type="generator", task_description="step B"))
    dag.add_node(DAGNode(id="C", agent_type="evaluator", task_description="step C"))
    dag.add_edge("A", "B")
    dag.add_edge("B", "C")
    return dag


class TestCollectUpstreamArtifacts:
    def test_only_direct_deps_collected(self):
        """_collect_upstream_artifacts(C) returns B's artifacts, not A's."""
        dag = _make_linear_dag()

        # Mark A and B as completed with artifacts
        dag.update_node("A", status=NodeStatus.SUCCESS, output_artifacts=["a_file.py"])
        dag.update_node("B", status=NodeStatus.SUCCESS, output_artifacts=["b_file.py"])

        artifacts = EvaluationPipeline._collect_upstream_artifacts(dag, "C")
        assert "b_file.py" in artifacts
        assert "a_file.py" not in artifacts  # A is transitive, not direct

    def test_no_artifacts_when_deps_not_completed(self):
        """Returns empty list when upstream nodes have no output_artifacts."""
        dag = _make_linear_dag()
        dag.update_node("B", status=NodeStatus.SUCCESS, output_artifacts=None)
        artifacts = EvaluationPipeline._collect_upstream_artifacts(dag, "C")
        assert artifacts == []

    def test_mixed_hard_soft_deps(self):
        """Collects from both hard and soft dependency predecessors."""
        dag = DAG(reasoning="test")
        dag.add_node(DAGNode(id="gen", agent_type="generator", task_description="gen"))
        dag.add_node(DAGNode(id="util", agent_type="generator", task_description="util"))
        dag.add_node(DAGNode(id="eval", agent_type="evaluator", task_description="eval"))

        dag.edges.append(DAGEdge(
            from_node="gen", to_node="eval",
            dependency_type=DependencyType.HARD,
        ))
        dag.edges.append(DAGEdge(
            from_node="util", to_node="eval",
            dependency_type=DependencyType.SOFT,
        ))

        dag.update_node("gen", status=NodeStatus.SUCCESS, output_artifacts=["gen.py"])
        dag.update_node("util", status=NodeStatus.SUCCESS, output_artifacts=["util.py"])

        artifacts = EvaluationPipeline._collect_upstream_artifacts(dag, "eval")
        assert "gen.py" in artifacts
        assert "util.py" in artifacts
