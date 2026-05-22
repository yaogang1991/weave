"""Tests for #750: planner timeout circuit breaker.

Verifies that:
1. Consecutive planner failures trigger circuit breaker after threshold
2. Non-planner failures reset the counter
3. Circuit breaker allows replan below threshold
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

from core.models import DAG, DAGNode, NodeStatus
from core.dag_engine import DAGExecutionEngine


def _make_engine(max_replans=3):
    """Create a DAGExecutionEngine with mocks."""
    mock_executor = MagicMock()
    mock_executor.execute_node = AsyncMock()
    mock_failure = AsyncMock(return_value=None)

    # replan_handler returns a proper DAG
    def make_replan_dag(dag_ref, failed_id):
        new_dag = DAG(reasoning="replanned")
        new_dag.add_node(DAGNode(
            id="plan_fix",
            agent_type="planner",
            task_description="replan",
        ))
        return new_dag

    mock_replan = AsyncMock(side_effect=make_replan_dag)

    engine = DAGExecutionEngine(
        agent_executor=mock_executor,
        failure_handler=mock_failure,
        replan_handler=mock_replan,
        max_replans=max_replans,
    )
    return engine, mock_replan


def test_circuit_breaker_triggers_after_threshold():
    """After 3 consecutive planner failures, replan is blocked (#750)."""
    engine, mock_replan = _make_engine()

    async def _run():
        # Simulate 3 consecutive planner failures
        for i in range(3):
            dag = DAG(reasoning="test")
            dag.add_node(DAGNode(
                id=f"plan_{i}",
                agent_type="planner",
                task_description="plan stuff",
            ))
            dag.update_node(f"plan_{i}", status=NodeStatus.FAILED)

            result = await engine._try_execute_replan(
                dag, f"plan_{i}", [[f"plan_{i}"]], 0, i,
            )
            initiated = result[4]

            if i < 2:
                # Below threshold, replan IS attempted
                assert initiated is True
            else:
                # At threshold, circuit breaker blocks replan
                assert initiated is False

        # Verify counter reached threshold
        assert engine._consecutive_planner_timeouts == 3
        # replan_handler called only 2 times (blocked on 3rd)
        assert mock_replan.call_count == 2

    asyncio.run(_run())


def test_non_planner_failure_resets_counter():
    """Non-planner node failure resets the consecutive counter (#750)."""
    engine, _ = _make_engine()

    async def _run():
        # 2 planner failures (below threshold)
        for i in range(2):
            dag = DAG(reasoning="test")
            dag.add_node(DAGNode(
                id=f"plan_{i}",
                agent_type="planner",
                task_description="plan",
            ))
            dag.update_node(f"plan_{i}", status=NodeStatus.FAILED)
            await engine._try_execute_replan(
                dag, f"plan_{i}", [[f"plan_{i}"]], 0, i,
            )

        assert engine._consecutive_planner_timeouts == 2

        # Non-planner failure resets counter
        dag = DAG(reasoning="test")
        dag.add_node(DAGNode(
            id="impl_1",
            agent_type="generator",
            task_description="implement",
        ))
        dag.update_node("impl_1", status=NodeStatus.FAILED)
        await engine._try_execute_replan(
            dag, "impl_1", [["impl_1"]], 0, 2,
        )

        assert engine._consecutive_planner_timeouts == 0

    asyncio.run(_run())


def test_circuit_breaker_allows_replan_below_threshold():
    """Below threshold, planner failures still trigger replan (#750)."""
    engine, mock_replan = _make_engine()

    async def _run():
        dag = DAG(reasoning="test")
        dag.add_node(DAGNode(
            id="plan_0",
            agent_type="planner",
            task_description="plan",
        ))
        dag.update_node("plan_0", status=NodeStatus.FAILED)

        result = await engine._try_execute_replan(
            dag, "plan_0", [["plan_0"]], 0, 0,
        )
        # Below threshold, replan_handler is called
        assert mock_replan.call_count == 1
        assert result[4] is True  # initiated
        assert engine._consecutive_planner_timeouts == 1

    asyncio.run(_run())
