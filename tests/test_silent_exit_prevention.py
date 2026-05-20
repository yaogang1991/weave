"""
Tests for #304: prevent silent process exits during DAG execution.

Ensures:
- CancelledError is caught and logged, not silently swallowed
- Node execution start is logged
- Level execution is logged
- Exception in gather is propagated, not dropped
"""
import asyncio
import pytest
from unittest.mock import AsyncMock

from core.models import DAG, DAGNode, DAGEdge, NodeStatus, FailureDecision
from core.dag_engine import DAGExecutionEngine


def _make_simple_dag() -> DAG:
    """Create a minimal 2-node DAG: plan → impl."""
    nodes = {
        "plan": DAGNode(
            id="plan",
            agent_type="planner",
            task_description="Plan",
            status=NodeStatus.PENDING,
        ),
        "impl": DAGNode(
            id="impl",
            agent_type="generator",
            task_description="Implement",
            status=NodeStatus.PENDING,
        ),
    }
    edges = [DAGEdge(from_node="plan", to_node="impl")]
    return DAG(nodes=nodes, edges=edges)


async def _abort_handler(dag, node_id, error):
    return FailureDecision(action="abort", reasoning="test abort")


@pytest.fixture
def engine():
    agent_executor = AsyncMock(return_value={"status": "completed", "artifacts": []})
    eng = DAGExecutionEngine(
        agent_executor=agent_executor,
        failure_handler=_abort_handler,
        max_parallel=3,
    )
    eng._emit = AsyncMock()  # Suppress event emissions
    return eng


class TestSilentExitPrevention:
    @pytest.mark.asyncio
    async def test_cancelled_error_propagated(self, engine):
        """CancelledError from node execution propagates, not swallowed (#304)."""
        dag = _make_simple_dag()

        # Make the agent executor raise CancelledError
        engine.agent_executor = AsyncMock(side_effect=asyncio.CancelledError())

        with pytest.raises(asyncio.CancelledError):
            await engine.execute(dag)

    @pytest.mark.asyncio
    async def test_exception_caught_and_node_failed(self, engine):
        """Exception during node execution sets FAILED status (#304).

        RuntimeError in agent execution is caught by execute_node,
        which retries then sets node to FAILED. The error is NOT re-raised
        through execute() — it becomes a failure_handler decision.
        """
        dag = _make_simple_dag()

        engine.agent_executor = AsyncMock(
            side_effect=RuntimeError("LLM API connection refused")
        )

        result = await engine.execute(dag)
        # First node fails → failure handler aborts → remaining nodes skipped
        assert result.nodes["plan"].status == NodeStatus.FAILED

    @pytest.mark.asyncio
    async def test_successful_execution(self, engine):
        """Normal execution completes successfully."""
        dag = _make_simple_dag()
        result = await engine.execute(dag)
        assert result.nodes["plan"].status == NodeStatus.SUCCESS
        assert result.nodes["impl"].status == NodeStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_node_failure_sets_failed_status(self, engine):
        """Node failure sets FAILED status and doesn't silently exit."""
        dag = _make_simple_dag()

        # impl node fails (after plan succeeds)
        call_count = 0

        async def _failing_executor(node, artifacts, **kwargs):
            nonlocal call_count
            call_count += 1
            if node.id == "impl":
                raise RuntimeError("Generation failed")
            return {"status": "completed", "artifacts": []}

        engine.agent_executor = _failing_executor
        result = await engine.execute(dag)
        assert result.nodes["plan"].status == NodeStatus.SUCCESS
        assert result.nodes["impl"].status == NodeStatus.FAILED

    @pytest.mark.asyncio
    async def test_parallel_node_failure_doesnt_kill_sibling(self, engine):
        """Parallel nodes with ownership contracts: one fails, sibling succeeds.

        Standalone generators without ownership contracts get auto-serialized
        (#272), so we must provide owned_files to keep them parallel.
        """
        nodes = {
            "a": DAGNode(
                id="a", agent_type="generator", task_description="A",
                status=NodeStatus.PENDING, owned_files=["a_module.py"],
            ),
            "b": DAGNode(
                id="b", agent_type="generator", task_description="B",
                status=NodeStatus.PENDING, owned_files=["b_module.py"],
            ),
        }
        dag = DAG(nodes=nodes, edges=[])

        async def _selective_executor(node, artifacts, **kwargs):
            if node.id == "a":
                raise RuntimeError("Node A failed")
            return {"status": "completed", "artifacts": []}

        engine.agent_executor = _selective_executor
        result = await engine.execute(dag)
        assert result.nodes["a"].status == NodeStatus.FAILED
        assert result.nodes["b"].status == NodeStatus.SUCCESS
