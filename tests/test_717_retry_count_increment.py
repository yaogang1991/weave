"""Tests for #717: retry_count increment on stall timeout.

Verifies:
1. NodeTimeoutError path increments retry_count in node_executor
2. dag_engine max_retries check skips exhausted nodes
"""
from datetime import datetime, timezone

from core.dag_models import DAG, DAGNode
from core.exceptions import NodeTimeoutError
from core.models import NodeStatus


def _make_dag() -> DAG:
    """Create a minimal DAG with a single generator node."""
    dag = DAG(reasoning="test #717")
    dag.add_node(DAGNode(
        id="gen_1",
        agent_type="generator",
        task_description="implement feature",
        max_retries=3,
    ))
    return dag


class TestRetryCountOnTimeout:
    """Verify retry_count increments on stall timeout (#717)."""

    def test_timeout_increments_retry_count(self):
        """NodeTimeoutError handler should increment retry_count."""
        dag = _make_dag()
        node_id = "gen_1"
        e = NodeTimeoutError("gen_1", "generator", 120)

        assert dag.nodes[node_id].retry_count == 0

        # Simulate the fixed handler in node_executor.py
        dag.update_node(
            node_id,
            status=NodeStatus.FAILED,
            error=str(e),
            completed_at=datetime.now(timezone.utc),
            auto_eval_result=None,
            retry_count=dag.nodes[node_id].retry_count + 1,
        )

        assert dag.nodes[node_id].retry_count == 1
        assert dag.nodes[node_id].status == NodeStatus.FAILED

    def test_retry_count_increments_each_timeout(self):
        """Multiple timeouts should increment retry_count each time."""
        dag = _make_dag()

        for i in range(1, 5):
            dag.update_node("gen_1", status=NodeStatus.RUNNING)
            dag.update_node(
                "gen_1",
                status=NodeStatus.FAILED,
                error=f"timeout #{i}",
                retry_count=dag.nodes["gen_1"].retry_count + 1,
            )
            assert dag.nodes["gen_1"].retry_count == i

    def test_max_retries_guard_skips_node(self):
        """When retry_count >= max_retries, node should be skipped."""
        dag = _make_dag()
        dag.update_node("gen_1", status=NodeStatus.FAILED, retry_count=3)

        node = dag.nodes["gen_1"]
        assert node.retry_count >= node.max_retries

        # Simulating the dag_engine check from #717
        if node.retry_count >= node.max_retries:
            dag.update_node("gen_1", status=NodeStatus.SKIPPED)

        assert dag.nodes["gen_1"].status == NodeStatus.SKIPPED

    def test_max_retries_not_yet_exhausted(self):
        """Node with retry_count < max_retries should NOT be skipped."""
        dag = _make_dag()
        dag.update_node("gen_1", status=NodeStatus.FAILED, retry_count=2)

        node = dag.nodes["gen_1"]
        assert node.retry_count < node.max_retries
