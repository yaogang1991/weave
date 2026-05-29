"""Tests for eval feedback injection on retry (#599)."""
import pytest

from core.models import DAG, DAGNode, NodeStatus


def _make_dag(nodes=None):
    return DAG(
        nodes=nodes or {},
        edges=[],
    )


def _make_node(node_id="n1", agent_type="generator"):
    return DAGNode(
        id=node_id,
        agent_type=agent_type,
        task_description="test task",
        max_retries=2,
    )


class TestEvalFeedbackPreservedOnRetry:
    """Verify eval_feedback is forwarded when dag_engine retries (#599)."""

    @pytest.mark.asyncio
    async def test_eval_feedback_forwarded_on_normal_retry(self):
        """Normal retry preserves eval_feedback from previous attempt."""
        from core.dag_engine import DAGExecutionEngine, DAGEngineConfig
        from core.models import FailureDecision

        node = _make_node("test_node", "generator")
        node.eval_feedback = "Tests failed: fixture 'db' not found"
        node.status = NodeStatus.FAILED
        dag = _make_dag({"test_node": node})

        retried_node = None

        async def mock_executor(dag_ref, nid):
            nonlocal retried_node
            retried_node = dag_ref.nodes[nid]

        async def mock_failure_handler(d, nid, err):
            return FailureDecision(action="retry", reasoning="test")

        engine = DAGExecutionEngine(
            agent_executor=mock_executor,
            failure_handler=mock_failure_handler,
            config=DAGEngineConfig(
                enable_watchdog=False,
            ),
        )
        # Patch execute_node to avoid full re-execution
        engine._node_executor.execute_node = mock_executor
        await engine.execute(dag)

        assert retried_node is not None
        assert retried_node.eval_feedback == "Tests failed: fixture 'db' not found"

    @pytest.mark.asyncio
    async def test_no_feedback_no_crash(self):
        """Retry with no eval_feedback still works."""
        from core.dag_engine import DAGExecutionEngine, DAGEngineConfig
        from core.models import FailureDecision

        node = _make_node("test_node", "generator")
        node.status = NodeStatus.FAILED
        dag = _make_dag({"test_node": node})

        async def mock_executor(dag_ref, nid):
            pass

        async def mock_failure_handler(d, nid, err):
            return FailureDecision(action="retry", reasoning="test")

        engine = DAGExecutionEngine(
            agent_executor=mock_executor,
            failure_handler=mock_failure_handler,
            config=DAGEngineConfig(
                enable_watchdog=False,
            ),
        )
        engine._node_executor.execute_node = mock_executor
        await engine.execute(dag)
        # Should not crash


class TestRetryInstructionIncludesFixtureGuidance:
    """Verify retry instruction mentions fixture/config issues (#599)."""

    def test_instruction_mentions_fixture(self):
        """Retry instruction includes fixture/config guidance."""
        from pathlib import Path
        source = Path("agent/agent_pool.py").read_text(encoding="utf-8")
        assert "FIXTURE/CONFIG ISSUES" in source
