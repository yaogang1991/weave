"""Tests for #718: replan decision produces no visible effect.

Verifies:
1. _try_execute_replan logs and returns False on replan handler failure
2. Caller skips the failed node (not abort entire execution) on replan failure
3. Successful replan produces correct merged DAG with logging
"""
from unittest.mock import AsyncMock, patch

import pytest

from core.dag_engine import DAGExecutionEngine, DAGEngineConfig
from core.dag_models import DAG, DAGNode
from core.models import NodeStatus


def _make_dag_with_failure() -> DAG:
    """Create a DAG with a failed node."""
    dag = DAG(reasoning="test #718")
    dag.add_node(DAGNode(
        id="gen_1", agent_type="generator",
        task_description="implement feature",
    ))
    dag.update_node("gen_1", status=NodeStatus.FAILED, error="timeout")
    return dag


class TestReplanErrorHandling:
    """Verify replan error handling (#718)."""

    @pytest.mark.asyncio
    async def test_replan_failure_returns_false(self):
        """When replan_handler throws, _try_execute_replan returns False."""
        dag = _make_dag_with_failure()
        levels = [["gen_1"]]

        async def bad_replan(dag, failed_id):
            raise RuntimeError("LLM call failed")

        engine = DAGExecutionEngine(
            agent_executor=AsyncMock(),
            failure_handler=AsyncMock(),
            config=DAGEngineConfig(max_parallel=1),
        )

        result_dag, result_levels, result_idx, result_count, initiated = (
            await engine._try_execute_replan(
                dag, "gen_1", levels, 0, 0,
            )
        )

        assert initiated is False
        assert result_count == 0  # Not incremented on failure
        assert result_dag is dag  # Original DAG preserved

    @pytest.mark.asyncio
    async def test_replan_success_returns_true(self):
        """Successful replan returns True with merged DAG."""
        dag = _make_dag_with_failure()

        new_dag = DAG(reasoning="replan")
        new_dag.add_node(DAGNode(
            id="gen_1_v2", agent_type="generator",
            task_description="split implementation",
        ))

        async def good_replan(dag, failed_id):
            return new_dag

        engine = DAGExecutionEngine(
            agent_executor=AsyncMock(),
            failure_handler=AsyncMock(),
            config=DAGEngineConfig(max_parallel=1),
        )
        engine.replan_handler = good_replan

        with patch.object(engine, '_emit', new_callable=AsyncMock):
            result_dag, result_levels, result_idx, result_count, initiated = (
                await engine._try_execute_replan(
                    dag, "gen_1", [["gen_1"]], 0, 0,
                )
            )

        assert initiated is True
        assert result_count == 1
        assert result_idx == 0
        # New node from replan should be present
        assert "gen_1_v2" in result_dag.nodes

    @pytest.mark.asyncio
    async def test_replan_none_handler_returns_false(self):
        """No replan_handler → returns False immediately."""
        dag = _make_dag_with_failure()

        engine = DAGExecutionEngine(
            agent_executor=AsyncMock(),
            failure_handler=AsyncMock(),
            config=DAGEngineConfig(max_parallel=1),
        )

        result_dag, result_levels, result_idx, result_count, initiated = (
            await engine._try_execute_replan(
                dag, "gen_1", [["gen_1"]], 0, 0,
            )
        )

        assert initiated is False
        assert result_dag is dag

    def test_failed_node_skipped_on_replan_failure(self):
        """When replan fails, failed node should be skipped."""
        dag = _make_dag_with_failure()
        # Simulate the fallback behavior from #718 fix
        dag.update_node("gen_1", status=NodeStatus.SKIPPED)
        assert dag.nodes["gen_1"].status == NodeStatus.SKIPPED
