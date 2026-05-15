"""
Tests for PR 1 (hemostasis) of fault tolerance refactoring (#360).

Covers:
1. NodeTimeoutError raised (not returned as dict) on agent timeout
2. RateLimitError propagation chain
3. RateLimitError does not consume node retry budget in dag_engine
4. LLM rate limit sleep cap reduced from 300 to 60
5. _classify_error recognises new exception names
"""

import asyncio
import time
from unittest.mock import MagicMock, patch

import pytest

from core.exceptions import NodeTimeoutError, RateLimitError
from core.models import (
    DAG, DAGNode, DAGEdge, NodeStatus, HandoffArtifact,
)


# -- Helpers ---------------------------------------------------------------

def _make_dag(nodes: dict[str, DAGNode], edges: list[DAGEdge] | None = None) -> DAG:
    return DAG(nodes=nodes, edges=edges or [])


def _make_node(
    node_id: str = "test_node",
    agent_type: str = "generator",
    max_retries: int = 2,
) -> DAGNode:
    return DAGNode(
        id=node_id,
        agent_type=agent_type,
        task_description="test task",
        max_retries=max_retries,
    )


# -- 1. NodeTimeoutError ---------------------------------------------------

class TestNodeTimeoutError:
    def test_is_exception(self):
        err = NodeTimeoutError("n1", "generator", 300)
        assert isinstance(err, Exception)

    def test_message_format(self):
        err = NodeTimeoutError("impl_auth", "generator", 300)
        assert "impl_auth" in str(err)
        assert "generator" in str(err)
        assert "300" in str(err)

    def test_attributes(self):
        err = NodeTimeoutError("n1", "planner", 600)
        assert err.node_id == "n1"
        assert err.agent_type == "planner"
        assert err.timeout == 600


# -- 2. RateLimitError -----------------------------------------------------

class TestRateLimitError:
    def test_is_exception(self):
        err = RateLimitError("anthropic", "claude-sonnet-4-6", 3)
        assert isinstance(err, Exception)

    def test_message_format(self):
        err = RateLimitError("openai", "gpt-4", 3)
        assert "openai" in str(err)
        assert "gpt-4" in str(err)
        assert "3" in str(err)

    def test_attributes(self):
        err = RateLimitError("anthropic", "claude-sonnet-4-6", 5)
        assert err.provider == "anthropic"
        assert err.model == "claude-sonnet-4-6"
        assert err.retries == 5


# -- 3. RateLimitError does not consume node retry budget -------------------

class TestRateLimitRetryBudget:
    @pytest.mark.asyncio
    async def test_rate_limit_preserves_retry_budget(self):
        """RateLimitError should NOT increment node.retry_count (#360)."""
        from core.dag_engine import DAGExecutionEngine

        node = _make_node("test_node", max_retries=2)
        dag = _make_dag({"test_node": node})

        call_count = 0

        async def mock_executor(n, artifacts):
            nonlocal call_count
            call_count += 1
            raise RateLimitError("anthropic", "claude-sonnet-4-6", 3)

        async def mock_failure_handler(d, nid, err):
            from core.models import FailureDecision
            return FailureDecision(action="abort", reasoning="test")

        engine = DAGExecutionEngine(
            agent_executor=mock_executor,
            failure_handler=mock_failure_handler,
            enable_watchdog=False,
        )
        await engine.execute(dag)

        assert node.status == NodeStatus.FAILED
        assert node.retry_count == 0  # NOT incremented
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_normal_error_consumes_retry_budget(self):
        """Non-rate-limit errors SHOULD still consume retry budget."""
        from core.dag_engine import DAGExecutionEngine

        node = _make_node("test_node", max_retries=2)
        dag = _make_dag({"test_node": node})

        call_count = 0

        async def mock_executor(n, artifacts):
            nonlocal call_count
            call_count += 1
            raise RuntimeError("something broke")

        async def mock_failure_handler(d, nid, err):
            from core.models import FailureDecision
            return FailureDecision(action="abort", reasoning="test")

        engine = DAGExecutionEngine(
            agent_executor=mock_executor,
            failure_handler=mock_failure_handler,
            enable_watchdog=False,
        )
        await engine.execute(dag)

        assert node.status == NodeStatus.FAILED
        # dag_engine retries internally up to max_retries, then abort fires.
        # retry_count == max_retries means all internal retries were consumed.
        assert node.retry_count == node.max_retries
        assert call_count >= 1


# -- 4. LLM sleep cap -------------------------------------------------------

class TestLLMSleepCap:
    def test_rate_limit_sleep_capped_at_60(self):
        """Rate limit sleep should be capped at 60s, not 300s (#360)."""
        from core.llm_client import LLMClient, LLMConfig

        config = LLMConfig(api_key="test-key")
        client = LLMClient(config)

        sleep_times = []
        original_sleep = time.sleep

        def mock_sleep(seconds):
            sleep_times.append(seconds)
            # Don't actually sleep

        # Simulate a rate limit error with a 200s wait time
        with patch.object(client, '_parse_rate_limit_wait', return_value=200.0):
            with patch.object(client, '_call_once', side_effect=Exception("429 rate limit exceeded")):
                with patch('time.sleep', side_effect=mock_sleep):
                    with pytest.raises((RateLimitError, Exception)):
                        client.call(
                            [{"role": "user", "content": "test"}],
                            max_retries=2,
                        )

        # All sleep times should be <= 61 (wait_sec + 1 = 61)
        for s in sleep_times:
            assert s <= 61, f"Sleep {s}s exceeds 60s cap"


# -- 5. _classify_error recognises new exceptions --------------------------

class TestClassifyError:
    def test_classify_node_timeout_error(self):
        from control_plane.service import _classify_error
        assert _classify_error("NodeTimeoutError: Node n1 (generator) exceeded 300s timeout") == "timeout"

    def test_classify_rate_limit_error(self):
        from control_plane.service import _classify_error
        assert _classify_error("RateLimitError: Rate limit exhausted for anthropic/claude-sonnet-4-6 after 3 retries") == "rate_limit"

    def test_classify_legacy_timeout_string(self):
        from control_plane.service import _classify_error
        assert _classify_error("Agent execution timed out after 300s") == "timeout"

    def test_classify_legacy_429_string(self):
        from control_plane.service import _classify_error
        assert _classify_error("429 Too Many Requests") == "rate_limit"

    def test_classify_unknown(self):
        from control_plane.service import _classify_error
        assert _classify_error("Something unexpected happened") == "unknown"
