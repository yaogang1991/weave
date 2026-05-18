"""
Tests for PR 1/2/3/4 of fault tolerance refactoring (#360).

Covers:
1. NodeTimeoutError raised (not returned as dict) on agent timeout
2. RateLimitError propagation chain
3. RateLimitError does not consume node retry budget in dag_engine
4. LLM rate limit sleep cap reduced from 300 to 60
5. classify_error recognises new exception names
6. PR2: Node timeout managed by dag_engine + cooperative cancel
7. PR3: progress_callback replaces _heartbeat_loop
8. PR4: Timeout inequality validation
"""

import asyncio
import time
from unittest.mock import patch

import pytest

from core.exceptions import NodeTimeoutError, RateLimitError
from core.models import (  # noqa: F401
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

        async def mock_executor(n, artifacts, **kwargs):
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

        assert dag.nodes["test_node"].status == NodeStatus.FAILED
        assert dag.nodes["test_node"].retry_count == 0  # NOT incremented
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_normal_error_consumes_retry_budget(self):
        """Non-rate-limit errors SHOULD still consume retry budget."""
        from core.dag_engine import DAGExecutionEngine

        node = _make_node("test_node", max_retries=2)
        dag = _make_dag({"test_node": node})

        call_count = 0

        async def mock_executor(n, artifacts, **kwargs):
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

        # Read from dag.nodes (not stale local `node`) after immutable updates (#486)
        final_node = dag.nodes["test_node"]
        assert final_node.status == NodeStatus.FAILED
        # dag_engine retries internally up to max_retries, then abort fires.
        # retry_count == max_retries means all internal retries were consumed.
        assert final_node.retry_count == final_node.max_retries
        assert call_count >= 1


# -- 4. LLM sleep cap -------------------------------------------------------

class TestLLMSleepCap:
    def test_rate_limit_sleep_capped_at_60(self):
        """Rate limit sleep should be capped at 60s, not 300s (#360)."""
        from core.llm_client import LLMClient, LLMConfig

        config = LLMConfig(api_key="test-key")
        client = LLMClient(config)

        sleep_times = []
        _original_sleep = time.sleep  # noqa: F841

        def mock_sleep(seconds):
            sleep_times.append(seconds)
            # Don't actually sleep

        # Simulate a rate limit error with a 200s wait time
        with patch.object(client, '_parse_rate_limit_wait', return_value=200.0):
            with patch.object(
                client, '_call_once',
                side_effect=Exception("429 rate limit exceeded")
            ):
                with patch('time.sleep', side_effect=mock_sleep):
                    with pytest.raises((RateLimitError, Exception)):
                        client.call(
                            [{"role": "user", "content": "test"}],
                            max_retries=2,
                        )

        # All sleep times should be <= 61 (wait_sec + 1 = 61)
        for s in sleep_times:
            assert s <= 61, f"Sleep {s}s exceeds 60s cap"


# -- 5. classify_error recognises new exceptions --------------------------

class TestClassifyError:
    def test_classify_node_timeout_error(self):
        from control_plane.errors import classify_error
        assert classify_error(
            "NodeTimeoutError: Node n1 (generator) exceeded 300s timeout"
        ) == "timeout"

    def test_classify_rate_limit_error(self):
        from control_plane.errors import classify_error
        assert classify_error(
            "RateLimitError: Rate limit exhausted for "
            "anthropic/claude-sonnet-4-6 after 3 retries"
        ) == "rate_limit"

    def test_classify_legacy_timeout_string(self):
        from control_plane.errors import classify_error
        assert classify_error("Agent execution timed out after 300s") == "timeout"

    def test_classify_legacy_429_string(self):
        from control_plane.errors import classify_error
        assert classify_error("429 Too Many Requests") == "rate_limit"

    def test_classify_unknown(self):
        from control_plane.errors import classify_error
        assert classify_error("Something unexpected happened") == "unknown"


# -- 6. PR2: Node timeout managed by dag_engine + cooperative cancel ---------

class TestNodeTimeoutInDagEngine:
    @pytest.mark.asyncio
    async def test_node_timeout_from_dag_engine(self):
        """Timeout is enforced by dag_engine, not agent_pool (#360 PR2)."""
        from core.dag_engine import DAGExecutionEngine

        node = _make_node("slow_node", max_retries=1)
        dag = _make_dag({"slow_node": node})

        async def slow_executor(n, artifacts, **kwargs):
            import asyncio
            await asyncio.sleep(10)  # Will exceed short timeout
            return {"status": "completed", "artifacts": []}

        async def mock_failure_handler(d, nid, err):
            from core.models import FailureDecision
            return FailureDecision(action="abort", reasoning="timeout")

        engine = DAGExecutionEngine(
            agent_executor=slow_executor,
            failure_handler=mock_failure_handler,
            enable_watchdog=False,
        )
        # Override timeout to 1s for fast test
        engine._get_node_timeout = lambda agent_type: 1
        await engine.execute(dag)

        assert dag.nodes["slow_node"].status == NodeStatus.FAILED


class TestCooperativeCancellation:
    def test_cancel_event_stops_worker(self):
        """threading.Event causes worker loop to exit at iteration boundary."""
        import threading

        from core.config import LLMConfig
        from core.llm_client import LLMClient  # noqa: F401
        from session.store import SessionStore
        from agent.worker import AgentWorker

        cancel = threading.Event()
        call_count = 0

        config = LLMConfig(api_key="test-key")
        store = SessionStore("./data/events")
        worker = AgentWorker(config, store)

        # Mock LLM to return a tool call every time
        def mock_call(messages, tools):
            nonlocal call_count
            call_count += 1
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": f"tc_{call_count}",
                    "name": "bash",
                    "arguments": {"command": "echo hi"},
                }],
            }

        # Mock tool executor
        from core.models import ToolResult

        class FakeExecutor:
            def execute(self, name, args):
                # Set cancel after 2 tool executions
                if call_count >= 2:
                    cancel.set()
                return ToolResult(tool_call_id="", success=True, output="ok")

        worker.llm.call = mock_call

        list(worker.run(  # noqa: F841
            session_id="test",
            system_prompt="test",
            user_message="test",
            tools=[],
            tool_executor=FakeExecutor(),
            max_iterations=50,
            cancel_event=cancel,
        ))

        # Should have stopped before 50 iterations
        assert call_count <= 5, f"Expected early exit but ran {call_count} iterations"


class TestNodeTimeoutConfig:
    def test_default_timeout(self):
        from core.config import NodeTimeoutConfig
        cfg = NodeTimeoutConfig()
        assert cfg.default_timeout == 300
        assert cfg.timeout_for("planner") == 300

    def test_generator_override(self):
        from core.config import NodeTimeoutConfig
        cfg = NodeTimeoutConfig()
        assert cfg.timeout_for("generator") == 600

    def test_evaluator_override(self):
        """Evaluator gets longer timeout than default for test+lint runs (#568)."""
        from core.config import NodeTimeoutConfig
        cfg = NodeTimeoutConfig()
        assert cfg.timeout_for("evaluator") == 480
        assert cfg.timeout_for("evaluator") > cfg.default_timeout

    def test_env_var_override(self):
        """Verify default_timeout can be explicitly set (env var is read at
        import time, not instance creation time, so we pass it explicitly)."""
        from core.config import NodeTimeoutConfig
        import os
        os.environ["WEAVE_NODE_TIMEOUT"] = "500"
        try:
            cfg = NodeTimeoutConfig(
                default_timeout=int(os.environ["WEAVE_NODE_TIMEOUT"]),
            )
            assert cfg.default_timeout == 500
        finally:
            del os.environ["WEAVE_NODE_TIMEOUT"]

    def test_min_max(self):
        from core.config import NodeTimeoutConfig
        cfg = NodeTimeoutConfig(default_timeout=300, overrides={"generator": 600, "evaluator": 120})
        assert cfg.min_timeout == 120
        assert cfg.max_timeout == 600


# -- 7. PR3: progress_callback replaces _heartbeat_loop -------------------------

class TestProgressCallback:
    @pytest.mark.asyncio
    async def test_progress_callback_called_after_llm_response(self):
        """progress_callback is invoked after each LLM call (#360 PR3)."""
        from core.dag_engine import DAGExecutionEngine

        progress_calls = 0

        async def mock_executor(n, artifacts, **kwargs):
            nonlocal progress_calls
            callback = kwargs.get("progress_callback")
            if callback:
                callback()  # Simulating agent reporting after LLM response
                progress_calls += 1
            return {"status": "completed", "artifacts": []}

        node = _make_node("test_node")
        dag = _make_dag({"test_node": node})

        engine = DAGExecutionEngine(
            agent_executor=mock_executor,
            failure_handler=None,
            enable_watchdog=False,
        )
        engine._get_node_timeout = lambda agent_type: 300
        await engine.execute(dag)

        assert dag.nodes["test_node"].status == NodeStatus.SUCCESS
        assert progress_calls >= 1

    @pytest.mark.asyncio
    async def test_progress_callback_receives_cancel_event(self):
        """Agent executor receives cancel_event for cooperative cancellation."""
        from core.dag_engine import DAGExecutionEngine
        import threading

        received_cancel = None
        received_progress = None

        async def mock_executor(n, artifacts, **kwargs):
            nonlocal received_cancel, received_progress
            received_cancel = kwargs.get("cancel_event")
            received_progress = kwargs.get("progress_callback")
            return {"status": "completed", "artifacts": []}

        node = _make_node("test_node")
        dag = _make_dag({"test_node": node})

        engine = DAGExecutionEngine(
            agent_executor=mock_executor,
            failure_handler=None,
            enable_watchdog=False,
        )
        engine._get_node_timeout = lambda agent_type: 300
        await engine.execute(dag)

        assert isinstance(received_cancel, threading.Event)
        assert callable(received_progress)

    @pytest.mark.asyncio
    async def test_no_heartbeat_loop_coroutine(self):
        """_heartbeat_loop should not exist as a method (#360 PR3)."""
        from core.dag_engine import DAGExecutionEngine
        engine = DAGExecutionEngine(
            agent_executor=lambda n, a, **kw: asyncio.coroutine(lambda: {"status": "ok"})(),
            failure_handler=None,
            enable_watchdog=False,
        )
        assert not hasattr(engine, '_heartbeat_loop'), \
            "_heartbeat_loop should have been removed in PR3"


# -- 8. PR4: Timeout inequality validation -----------------------------------

class TestTimeoutValidation:
    def test_valid_config_no_issues(self):
        from core.config import WeaveConfig, LLMConfig, NodeTimeoutConfig
        config = WeaveConfig(
            llm=LLMConfig(api_key="test", timeout=120),
            run_timeout_sec=1800,
            node_timeout=NodeTimeoutConfig(default_timeout=300, overrides={"generator": 600}),
        )
        issues = config.validate_timeout_inequality()
        assert issues == []

    def test_llm_timeout_exceeds_node_timeout(self):
        from core.config import WeaveConfig, LLMConfig, NodeTimeoutConfig
        config = WeaveConfig(
            llm=LLMConfig(api_key="test", timeout=400),
            run_timeout_sec=1800,
            node_timeout=NodeTimeoutConfig(default_timeout=300),
        )
        issues = config.validate_timeout_inequality()
        assert len(issues) == 1
        assert "HTTP timeout" in issues[0]

    def test_node_timeout_exceeds_run_timeout(self):
        from core.config import WeaveConfig, LLMConfig, NodeTimeoutConfig
        config = WeaveConfig(
            llm=LLMConfig(api_key="test", timeout=60),
            run_timeout_sec=300,
            node_timeout=NodeTimeoutConfig(default_timeout=300, overrides={"generator": 600}),
        )
        issues = config.validate_timeout_inequality()
        assert len(issues) == 1
        assert "Max node timeout" in issues[0]

    def test_both_violations(self):
        from core.config import WeaveConfig, LLMConfig, NodeTimeoutConfig
        config = WeaveConfig(
            llm=LLMConfig(api_key="test", timeout=200),
            run_timeout_sec=100,
            node_timeout=NodeTimeoutConfig(default_timeout=150, overrides={"generator": 500}),
        )
        issues = config.validate_timeout_inequality()
        assert len(issues) == 2
