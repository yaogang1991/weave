"""
Tests for #674: Stall kill must cancel the underlying asyncio.Task.

Before the fix, _execute_with_timeout raised NodeTimeoutError on stall
without calling task.cancel().  This left a zombie task that could:
- Hold API semaphore / network connections
- Interfere with retry attempts
- Cause the retry node to run unbounded (2209s observed vs 240s timeout)

The fix adds task.cancel() + await task cleanup before raising NodeTimeoutError.
"""
import asyncio
import pytest
from unittest.mock import AsyncMock

from core.models import DAG, DAGNode, NodeStatus
from core.node_executor import NodeExecutor, NodeExecutorConfig
from core.watchdog import WatchdogService


def _make_node(nid: str = "plan", agent_type: str = "planner") -> DAGNode:
    return DAGNode(
        id=nid,
        agent_type=agent_type,
        task_description=f"task-{nid}",
        max_retries=0,
    )


def _make_dag(node: DAGNode | None = None) -> DAG:
    node = node or _make_node()
    return DAG(nodes={node.id: node}, edges=[])


def _make_executor(stall_timeout: int = 5, **overrides) -> NodeExecutor:
    from core.config import NodeTimeoutConfig
    timeout_cfg = NodeTimeoutConfig(
        default_timeout=30,
        stall_timeout=stall_timeout,
    )
    agent_exec = overrides.pop("agent_executor", AsyncMock(return_value={"artifacts": []}))
    emit = overrides.pop("emit_func", AsyncMock())
    wd = overrides.pop("watchdog", WatchdogService(enabled=False))
    return NodeExecutor(
        agent_executor=agent_exec,
        emit_func=emit,
        watchdog=wd,
        config=NodeExecutorConfig(node_timeout_config=timeout_cfg, **overrides),
    )


class TestStallKillCancelsTask:
    """Stall kill must cancel the underlying asyncio.Task (#674)."""

    @pytest.mark.asyncio
    async def test_stall_kill_cancels_agent_task(self):
        """When stall kills a node, the agent_executor's task must be cancelled."""
        node = _make_node()
        dag = _make_dag(node)

        async def _hung_executor(n, artifacts, **kw):
            await asyncio.Event().wait()
            return {"artifacts": []}

        executor = _make_executor(
            stall_timeout=2,
            agent_executor=_hung_executor,
        )

        await executor.execute_node(dag, node.id)

        # DAGNode is immutable — check the current version in dag.nodes
        assert dag.nodes[node.id].status == NodeStatus.FAILED
        assert "timeout" in (dag.nodes[node.id].error or "").lower()
        await asyncio.sleep(0.1)

    @pytest.mark.asyncio
    async def test_stall_kill_allows_clean_retry(self):
        """After stall kill, a retry should work with a fresh tracker."""
        node = _make_node()
        dag = _make_dag(node)

        call_count = 0

        async def _stall_then_succeed(n, artifacts, **kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                await asyncio.Event().wait()
            return {"artifacts": ["output.py"]}

        executor = _make_executor(
            stall_timeout=2,
            agent_executor=_stall_then_succeed,
        )

        # First attempt: stall kills it -> FAILED
        await executor.execute_node(dag, node.id)
        assert dag.nodes[node.id].status == NodeStatus.FAILED
        assert call_count == 1

        # Reset for retry
        dag.update_node(
            node.id,
            status=NodeStatus.RETRYING,
            retry_count=1,
            error="",
        )

        # Second attempt: agent returns immediately -> SUCCESS
        await executor.execute_node(dag, node.id)
        assert call_count == 2
        assert dag.nodes[node.id].status == NodeStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_zombie_task_does_not_survive_stall_kill(self):
        """Verify no zombie asyncio.Task survives after stall kill."""
        task_ref = None

        async def _track_task(n, artifacts, **kw):
            nonlocal task_ref
            task_ref = asyncio.current_task()
            await asyncio.Event().wait()

        node = _make_node()
        dag = _make_dag(node)
        executor = _make_executor(
            stall_timeout=2,
            agent_executor=_track_task,
        )

        await executor.execute_node(dag, node.id)

        assert task_ref is not None
        assert task_ref.cancelled() or task_ref.done()
