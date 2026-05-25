"""Tests for #789: replan-replaced failed nodes are marked SUPERSEDED.

Verifies:
1. _try_execute_replan marks the original failed node as SUPERSEDED
2. SUPERSEDED nodes are excluded from `pending` list in execution
3. When a different node fails, the superseded node is NOT re-triggered for replan
4. If replan re-includes the original node, it is NOT marked SUPERSEDED
"""
from unittest.mock import AsyncMock, patch

import pytest

from core.dag_engine import DAGExecutionEngine, DAGEngineConfig
from core.dag_models import DAG, DAGNode
from core.models import NodeStatus, FailureDecision


def _make_dag_with_failed_and_pending() -> DAG:
    """DAG: plan(succeeded) -> impl(failed) -> eval(pending)."""
    dag = DAG(reasoning="test #789")
    dag.add_node(DAGNode(
        id="plan", agent_type="planner", task_description="plan",
    ))
    dag.add_node(DAGNode(
        id="impl", agent_type="generator", task_description="impl",
    ))
    dag.add_node(DAGNode(
        id="eval", agent_type="evaluator", task_description="eval",
    ))
    dag.add_edge("plan", "impl")
    dag.add_edge("impl", "eval")
    dag.update_node("plan", status=NodeStatus.SUCCESS)
    dag.update_node("impl", status=NodeStatus.FAILED, error="timeout")
    return dag


class TestSupersededAfterReplan:
    """Verify superseded marking (#789)."""

    @pytest.mark.asyncio
    async def test_replan_marks_original_superseded(self):
        """After replan, original failed node becomes SUPERSEDED."""
        dag = _make_dag_with_failed_and_pending()

        new_dag = DAG(reasoning="replan")
        new_dag.add_node(DAGNode(
            id="impl_v2", agent_type="generator",
            task_description="split implementation",
        ))

        async def good_replan(old_dag, failed_id):
            return new_dag

        engine = DAGExecutionEngine(
        agent_executor=AsyncMock(),
        failure_handler=AsyncMock(),
        replan_handler=good_replan,
        config=DAGEngineConfig(
            max_parallel=1,
        ),
    )

        with patch.object(engine, '_emit', new_callable=AsyncMock):
            result_dag, *_ = await engine._try_execute_replan(
                dag, "impl", [["plan"], ["impl"], ["eval"]], 1, 0,
            )

        assert result_dag.nodes["impl"].status == NodeStatus.SUPERSEDED
        assert "impl_v2" in result_dag.nodes

    @pytest.mark.asyncio
    async def test_replan_preserves_original_if_re_included(self):
        """If replan includes the original node, it stays as-is."""
        dag = _make_dag_with_failed_and_pending()

        new_dag = DAG(reasoning="replan")
        new_dag.add_node(DAGNode(
            id="impl", agent_type="generator",
            task_description="retry implementation",
        ))

        async def replan_with_original(old_dag, failed_id):
            return new_dag

        engine = DAGExecutionEngine(
        agent_executor=AsyncMock(),
        failure_handler=AsyncMock(),
        replan_handler=replan_with_original,
        config=DAGEngineConfig(
            max_parallel=1,
        ),
    )

        with patch.object(engine, '_emit', new_callable=AsyncMock):
            result_dag, *_ = await engine._try_execute_replan(
                dag, "impl", [["plan"], ["impl"], ["eval"]], 1, 0,
            )

        assert result_dag.nodes["impl"].status != NodeStatus.SUPERSEDED

    @pytest.mark.asyncio
    async def test_superseded_not_re_triggered_for_replan(self):
        """Full execution: superseded node doesn't trigger replan again."""
        dag = DAG(reasoning="test #789 full")
        dag.add_node(DAGNode(
            id="impl", agent_type="generator", task_description="impl",
        ))
        dag.add_node(DAGNode(
            id="eval", agent_type="evaluator", task_description="eval",
        ))
        dag.add_edge("impl", "eval")

        replan_new_dag = DAG(reasoning="replan")
        replan_new_dag.add_node(DAGNode(
            id="impl_v2", agent_type="generator",
            task_description="split implementation",
        ))

        call_count = {"replan": 0, "failure": []}

        async def counting_executor(node, artifacts, **kwargs):
            if node.id == "impl":
                raise RuntimeError("impl failed")
            return {
                "status": "completed",
                "summary": "done",
                "artifacts": [],
                "output": "ok",
            }

        async def counting_failure_handler(dag, node_id, error):
            call_count["failure"].append(node_id)
            if node_id == "impl" and call_count["replan"] == 0:
                return FailureDecision(
                    action="replan", reasoning="split impl",
                )
            return FailureDecision(action="skip", reasoning="give up")

        async def counting_replan(old_dag, failed_id):
            call_count["replan"] += 1
            return replan_new_dag

        engine = DAGExecutionEngine(
        agent_executor=counting_executor,
        failure_handler=counting_failure_handler,
        replan_handler=counting_replan,
        config=DAGEngineConfig(
            max_parallel=1,
        ),
    )

        result = await engine.execute(dag)

        assert result.nodes["impl"].status == NodeStatus.SUPERSEDED
        impl_failures = [f for f in call_count["failure"] if f == "impl"]
        assert impl_failures == ["impl"], (
            f"impl triggered failure_handler {len(impl_failures)} times, "
            f"expected 1. All calls: {call_count['failure']}"
        )
