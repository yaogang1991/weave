"""Tests for #688/#697: _serialize_dag includes token fields + round-trip.

Verifies that estimated_tokens, token_budget, and actual_tokens are
included in the serialized plan JSON output, and that a serialized DAG
can be deserialized back into an equivalent DAG with all fields intact.
"""
import json
import tempfile
from pathlib import Path

from core.models import DAG, DAGNode
from cli.utils import _serialize_dag
from cli.execution import _load_dag_from_file


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


class TestRoundTripDagSerialization:
    """#697: serialize → deserialize preserves all fields."""

    def test_round_trip_single_node_token_fields(self):
        """Serialize a DAG, write to file, load back — token fields match."""
        dag = DAG(reasoning="round trip test")
        dag.add_node(DAGNode(
            id="impl_core",
            agent_type="generator",
            task_description="implement core module",
            success_criteria=["file exists"],
            estimated_tokens=5000,
            token_budget=8192,
            actual_tokens=4200,
        ))

        serialized = _serialize_dag(dag)

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False,
        ) as f:
            json.dump(serialized, f)
            tmp_path = f.name

        try:
            loaded = _load_dag_from_file(tmp_path)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

        assert loaded.reasoning == "round trip test"
        assert len(loaded.nodes) == 1
        node = loaded.nodes["impl_core"]
        assert node.agent_type == "generator"
        assert node.task_description == "implement core module"
        assert node.estimated_tokens == 5000
        assert node.token_budget == 8192
        assert node.actual_tokens == 4200

    def test_round_trip_multi_node_with_edges(self):
        """Round-trip preserves multiple nodes and edges."""
        dag = DAG(reasoning="multi-node plan")
        dag.add_node(DAGNode(
            id="plan_1",
            agent_type="planner",
            task_description="plan the project",
            estimated_tokens=2000,
            token_budget=4096,
            actual_tokens=1800,
        ))
        dag.add_node(DAGNode(
            id="impl_1",
            agent_type="generator",
            task_description="implement feature",
            estimated_tokens=6000,
            token_budget=12288,
            actual_tokens=5500,
        ))
        dag.add_edge("plan_1", "impl_1")

        serialized = _serialize_dag(dag)

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False,
        ) as f:
            json.dump(serialized, f)
            tmp_path = f.name

        try:
            loaded = _load_dag_from_file(tmp_path)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

        assert len(loaded.nodes) == 2
        assert len(loaded.edges) == 1
        assert loaded.edges[0].from_node == "plan_1"
        assert loaded.edges[0].to_node == "impl_1"

        plan_node = loaded.nodes["plan_1"]
        assert plan_node.estimated_tokens == 2000
        assert plan_node.token_budget == 4096

        impl_node = loaded.nodes["impl_1"]
        assert impl_node.estimated_tokens == 6000
        assert impl_node.token_budget == 12288
        assert impl_node.actual_tokens == 5500

    def test_round_trip_default_token_values(self):
        """Nodes with default (0) token values survive round-trip."""
        dag = DAG(reasoning="defaults test")
        dag.add_node(DAGNode(
            id="eval_1",
            agent_type="evaluator",
            task_description="evaluate results",
        ))

        serialized = _serialize_dag(dag)

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False,
        ) as f:
            json.dump(serialized, f)
            tmp_path = f.name

        try:
            loaded = _load_dag_from_file(tmp_path)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

        node = loaded.nodes["eval_1"]
        assert node.estimated_tokens == 0
        assert node.actual_tokens == 0
