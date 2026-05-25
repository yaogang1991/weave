"""
Tests for #432: NodeTimeoutError retry budget + LLM sleep cap.

Fix 1 (P2): NodeTimeoutError should not consume node retry budget,
            matching existing RateLimitError treatment.

Fix 2 (P3): LLM rate-limit sleep should bail early when cumulative
            sleep exceeds 50% of timeout budget.
"""
import pytest
from unittest.mock import MagicMock, patch

from core.models import DAG, DAGNode, NodeStatus
from core.dag_engine import DAGExecutionEngine, DAGEngineConfig
from core.exceptions import NodeTimeoutError, RateLimitError


def _make_dag_with_node(max_retries: int = 2) -> DAG:
    dag = DAG(reasoning="test")
    dag.add_node(DAGNode(
        id="n1",
        agent_type="generator",
        task_description="impl",
        max_retries=max_retries,
    ))
    return dag


async def _noop_failure_handler(dag, node_id, error):
    from core.models import FailureDecision
    return FailureDecision(action="abort", reasoning="test")


# ---------------------------------------------------------------------------
# Fix 1: NodeTimeoutError does NOT consume retry budget
# ---------------------------------------------------------------------------


class TestNodeTimeoutRetryBudget:
    """Verify NodeTimeoutError is treated like RateLimitError (#432 fix 1)."""

    @pytest.mark.asyncio
    async def test_timeout_does_not_increment_retry_count(self):
        """NodeTimeoutError should not increment retry_count."""
        dag = _make_dag_with_node(max_retries=2)

        async def timeout_executor(node, artifacts, **kwargs):
            raise NodeTimeoutError(
                node_id=node.id,
                agent_type=node.agent_type,
                timeout=300,
            )

        engine = DAGExecutionEngine(
            timeout_executor, _noop_failure_handler
        ,
        config=DAGEngineConfig(
            enable_watchdog=False,
        ),
    )
        await engine._node_executor.execute_node(dag, "n1")

        node = dag.nodes["n1"]
        # Key assertion: timeout does NOT consume retry budget
        assert node.retry_count == 0, (
            f"NodeTimeoutError should not consume retry budget, "
            f"but retry_count={node.retry_count}"
        )
        assert node.status == NodeStatus.FAILED

    @pytest.mark.asyncio
    async def test_rate_limit_still_preserves_budget(self):
        """Verify existing RateLimitError behavior is not broken."""
        dag = _make_dag_with_node(max_retries=2)

        async def rate_limit_executor(node, artifacts, **kwargs):
            raise RateLimitError(
                provider="anthropic",
                model="claude-sonnet-4-6",
                retries=3,
            )

        engine = DAGExecutionEngine(
            rate_limit_executor, _noop_failure_handler
        ,
        config=DAGEngineConfig(
            enable_watchdog=False,
        ),
    )
        await engine._node_executor.execute_node(dag, "n1")

        node = dag.nodes["n1"]
        assert node.retry_count == 0
        assert node.status == NodeStatus.FAILED

    @pytest.mark.asyncio
    async def test_generic_exception_still_consumes_budget(self):
        """Regular exceptions should still consume retry budget.

        With max_retries=0 the node fails immediately but retry_count
        should still be incremented (budget consumed).
        """
        dag = _make_dag_with_node(max_retries=0)

        async def fail_executor(node, artifacts, **kwargs):
            raise RuntimeError("something went wrong")

        engine = DAGExecutionEngine(
            fail_executor, _noop_failure_handler
        ,
        config=DAGEngineConfig(
            enable_watchdog=False,
        ),
    )
        await engine._node_executor.execute_node(dag, "n1")

        node = dag.nodes["n1"]
        assert node.retry_count >= 1, (
            "Generic exceptions should consume retry budget"
        )
        assert node.status == NodeStatus.FAILED

    @pytest.mark.asyncio
    async def test_timeout_emits_failed_with_reason(self):
        """NodeTimeoutError should emit event with reason='timeout'."""
        dag = _make_dag_with_node(max_retries=2)
        emitted_events = []

        async def timeout_executor(node, artifacts, **kwargs):
            raise NodeTimeoutError(
                node_id=node.id, agent_type=node.agent_type, timeout=300,
            )

        engine = DAGExecutionEngine(
            timeout_executor, _noop_failure_handler
        ,
        config=DAGEngineConfig(
            enable_watchdog=False,
        ),
    )
        engine.event_handlers.append(lambda e: emitted_events.append(e))

        await engine._node_executor.execute_node(dag, "n1")

        # Should have a "failed" event with timeout reason
        failed_events = [
            e for e in emitted_events
            if hasattr(e, 'event_type') and e.event_type == "failed"
        ]
        assert len(failed_events) >= 1
        details = failed_events[0].details
        assert details.get("reason") == "timeout"
        assert details.get("retry_budget_preserved") is True


# ---------------------------------------------------------------------------
# Fix 2: LLM sleep cap — bail early when sleep exceeds timeout budget
# ---------------------------------------------------------------------------


class TestLLMSleepCap:
    """Verify LLM client bails early when rate-limit sleep dominates timeout."""

    def test_bails_early_when_sleep_exceeds_half_timeout(self):
        """When cumulative sleep > 50% of agent_timeout, raise RateLimitError."""
        from core.llm_client import LLMClient
        from core.config import LLMConfig

        config = LLMConfig(provider="anthropic", model="test", api_key="k")
        client = LLMClient(config, max_retries=5)

        # Simulate persistent 429 errors — each sleep 60s
        # With agent_timeout=100, 50% = 50s. First 60s sleep already exceeds.
        client._call_once = MagicMock(
            side_effect=RuntimeError("429 rate limit, retry-after: 60")
        )

        with patch("core.llm_client.time.sleep"):
            # The first sleep is 60s (capped). Cumulative = 60 > 50.
            # On the next attempt, it should bail with RateLimitError.
            with pytest.raises(RateLimitError):
                client.call(
                    [{"role": "user", "content": "hi"}],
                    [],
                    agent_timeout=100,
                )

    def test_no_bail_when_sleep_within_budget(self):
        """Short sleeps that stay within budget should not trigger early bail."""
        from core.llm_client import LLMClient
        from core.config import LLMConfig

        config = LLMConfig(provider="anthropic", model="test", api_key="k")
        client = LLMClient(config, max_retries=2)

        # First call fails, second succeeds
        client._call_once = MagicMock(side_effect=[
            RuntimeError("429 rate limit, retry-after: 5"),
            {"role": "assistant", "content": "done"},
        ])

        with patch("core.llm_client.time.sleep"):
            result = client.call(
                [{"role": "user", "content": "hi"}],
                [],
                agent_timeout=300,
            )

        assert result["content"] == "done"

    def test_no_agent_timeout_uses_default_behavior(self):
        """When agent_timeout is not provided, old behavior applies."""
        from core.llm_client import LLMClient
        from core.config import LLMConfig

        config = LLMConfig(provider="anthropic", model="test", api_key="k")
        client = LLMClient(config, max_retries=1)

        client._call_once = MagicMock(
            side_effect=RuntimeError("429 rate limit, retry-after: 60")
        )

        with patch("core.llm_client.time.sleep"):
            with pytest.raises(RateLimitError):
                client.call(
                    [{"role": "user", "content": "hi"}],
                    [],
                )
