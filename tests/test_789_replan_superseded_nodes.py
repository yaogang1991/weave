"""Tests for #789: replan should mark superseded nodes as SKIPPED."""
import pytest

from core.models import (
    DAG, DAGNode, NodeStatus, ExecutionEvent, FailureDecision,
)


def _make_two_node_dag():
    dag = DAG(reasoning="test")
    dag.add_node(DAGNode(id="impl", agent_type="generator", task_description="impl"))
    dag.add_node(DAGNode(id="eval", agent_type="evaluator", task_description="eval"))
    dag.add_edge("impl", "eval")
    return dag


def _replan_handler_factory(call_log: list):
    """Create a replan handler that replaces 'impl' with sub-nodes."""
    async def handler(dag, failed_id):
        call_log.append(failed_id)
        new_dag = DAG(reasoning="replan")
        new_dag.add_node(DAGNode(
            id=f"{failed_id}_part_a",
            agent_type="generator",
            task_description="part a",
        ))
        new_dag.add_node(DAGNode(
            id=f"{failed_id}_part_b",
            agent_type="generator",
            task_description="part b",
        ))
        return new_dag
    return handler


class TestReplanSuperseded:
    @pytest.mark.asyncio
    async def test_superseded_node_marked_skipped(self):
        """After replan, the original failed node should be SKIPPED (#789)."""
        dag = _make_two_node_dag()
        replan_log = []

        async def fail_impl(node, artifacts, **kw):
            if node.id == "impl":
                raise RuntimeError("boom")
            return {"status": "completed", "summary": "ok", "artifacts": []}

        async def failure_handler(dag, node_id, error):
            return FailureDecision(action="replan", reasoning="try replan")

        from core.dag_engine import DAGExecutionEngine
        engine = DAGExecutionEngine(
            fail_impl, failure_handler,
            replan_handler=_replan_handler_factory(replan_log),
            max_replans=3,
        )

        result = await engine.execute(dag)
        assert result.nodes["impl"].status == NodeStatus.SKIPPED

    @pytest.mark.asyncio
    async def test_no_redundant_replans_for_superseded(self):
        """Superseded node should NOT trigger replan again (#789)."""
        dag = _make_two_node_dag()
        replan_log = []

        async def fail_impl(node, artifacts, **kw):
            if node.id == "impl":
                raise RuntimeError("boom")
            if node.id == "eval":
                raise RuntimeError("eval timeout")
            return {"status": "completed", "summary": "ok", "artifacts": []}

        async def failure_handler(dag, node_id, error):
            return FailureDecision(action="replan", reasoning="try replan")

        from core.dag_engine import DAGExecutionEngine
        engine = DAGExecutionEngine(
            fail_impl, failure_handler,
            replan_handler=_replan_handler_factory(replan_log),
            max_replans=5,
        )

        await engine.execute(dag)
        # impl should only trigger replan once, not multiple times
        assert replan_log.count("impl") == 1, (
            f"Expected 1 replan for impl, got {replan_log.count('impl')}: {replan_log}"
        )

    @pytest.mark.asyncio
    async def test_skipped_nodes_excluded_from_pending(self):
        """SKIPPED nodes should not be re-executed in level processing."""
        dag = DAG(reasoning="test")
        dag.add_node(DAGNode(id="a", agent_type="planner", task_description="a"))
        dag.add_node(DAGNode(id="b", agent_type="generator", task_description="b"))
        execution_log = []

        async def tracking_exec(node, artifacts, **kw):
            execution_log.append(node.id)
            return {"status": "completed", "summary": "ok", "artifacts": []}

        async def noop_failure(dag, nid, err):
            return FailureDecision(action="skip", reasoning="skip")

        from core.dag_engine import DAGExecutionEngine
        engine = DAGExecutionEngine(tracking_exec, noop_failure)

        # Pre-set node 'a' as SKIPPED (simulating superseded state)
        dag.update_node("a", status=NodeStatus.SKIPPED)

        result = await engine.execute(dag)
        assert "a" not in execution_log
        assert "b" in execution_log
        assert result.nodes["a"].status == NodeStatus.SKIPPED
        assert result.nodes["b"].status == NodeStatus.SUCCESS
