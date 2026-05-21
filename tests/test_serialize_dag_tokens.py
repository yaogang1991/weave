"""Tests for #688: _serialize_dag includes token fields.

Verifies that estimated_tokens, token_budget, and actual_tokens are
included in the serialized plan JSON output.
"""
from core.models import DAG, DAGNode
from cli.utils import _serialize_dag


def _make_dag_with_token_fields():
    """Create a DAG with token fields set on nodes."""
    dag = DAG(reasoning="test plan")
    node = DAGNode(
        id="impl_core",
        agent_type="generator",
        task_description="implement core module",
        estimated_tokens=5000,
        token_budget=8192,
        actual_tokens=4200,
    )
    dag.add_node(node)
    return dag


class TestSerializeDagTokenFields:
    """#688: _serialize_dag should include token fields."""

    def test_includes_estimated_tokens(self):
        dag = _make_dag_with_token_fields()
        result = _serialize_dag(dag)
        node = result["nodes"][0]
        assert node["estimated_tokens"] == 5000

    def test_includes_token_budget(self):
        dag = _make_dag_with_token_fields()
        result = _serialize_dag(dag)
        node = result["nodes"][0]
        assert node["token_budget"] == 8192

    def test_includes_actual_tokens(self):
        dag = _make_dag_with_token_fields()
        result = _serialize_dag(dag)
        node = result["nodes"][0]
        assert node["actual_tokens"] == 4200

    def test_default_token_values(self):
        """Nodes with default (0) token values are still serialized."""
        dag = DAG(reasoning="test")
        dag.add_node(DAGNode(
            id="plan",
            agent_type="planner",
            task_description="plan the project",
        ))
        result = _serialize_dag(dag)
        node = result["nodes"][0]
        assert "estimated_tokens" in node
        assert "token_budget" in node
        assert "actual_tokens" in node
        assert node["estimated_tokens"] == 0
        assert node["actual_tokens"] == 0

    def test_preserves_existing_fields(self):
        """Token fields don't break existing serialization."""
        dag = _make_dag_with_token_fields()
        result = _serialize_dag(dag)
        node = result["nodes"][0]
        assert node["id"] == "impl_core"
        assert node["agent_type"] == "generator"
        assert node["task"] == "implement core module"
        assert result["reasoning"] == "test plan"
