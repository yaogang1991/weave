"""Tests for immutable node transitions via DAG.update_node (#486).

Verify that model_copy pattern creates new nodes rather than mutating originals.
"""
import pytest

from core.models import DAG, DAGNode, NodeStatus


class TestDAGUpdateNode:
    """DAG.update_node creates new nodes, original stays unchanged (#486)."""

    def test_update_node_returns_new_instance(self):
        """update_node returns a new DAGNode, not the original."""
        dag = DAG()
        original = DAGNode(id="n1", agent_type="generator", task_description="test")
        dag.add_node(original)

        updated = dag.update_node("n1", status=NodeStatus.RUNNING)
        assert updated is not original
        assert updated.status == NodeStatus.RUNNING
        assert original.status == NodeStatus.PENDING  # unchanged

    def test_update_node_replaces_in_dict(self):
        """dag.nodes reflects the updated node after update_node."""
        dag = DAG()
        dag.add_node(DAGNode(id="n1", agent_type="generator", task_description="test"))

        dag.update_node("n1", status=NodeStatus.SUCCESS, result={"key": "val"})

        assert dag.nodes["n1"].status == NodeStatus.SUCCESS
        assert dag.nodes["n1"].result == {"key": "val"}

    def test_update_node_multiple_fields(self):
        """Multiple fields updated in single call."""
        dag = DAG()
        dag.add_node(DAGNode(id="n1", agent_type="generator", task_description="test"))

        dag.update_node(
            "n1",
            status=NodeStatus.FAILED,
            error="something broke",
            retry_count=2,
        )

        node = dag.nodes["n1"]
        assert node.status == NodeStatus.FAILED
        assert node.error == "something broke"
        assert node.retry_count == 2

    def test_original_node_not_mutated_after_multiple_updates(self):
        """Series of updates never touch the original node."""
        dag = DAG()
        original = DAGNode(id="n1", agent_type="generator", task_description="test")
        dag.add_node(original)

        dag.update_node("n1", status=NodeStatus.RUNNING)
        dag.update_node("n1", status=NodeStatus.FAILED, error="oops")
        dag.update_node("n1", status=NodeStatus.RETRYING, error="")

        # Original never changed
        assert original.status == NodeStatus.PENDING
        assert original.error == ""
        assert original.retry_count == 0

        # Current state reflects latest update
        assert dag.nodes["n1"].status == NodeStatus.RETRYING
        assert dag.nodes["n1"].error == ""
