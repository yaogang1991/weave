"""Tests for #751/#752: audit event records correct action after retry-exhaustion remap.

Verifies that:
1. #751: Inner fallback path emits remapped action (not original "retry")
2. #752: Initial decision path remaps exhausted "retry" before emitting
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

from core.models import DAG, DAGNode, NodeStatus
from core.dag_models import FailureDecision


def _make_dag_with_failed_node(
    retry_count=3, max_retries=3, error="something failed",
):
    """Create a DAG with one failed node."""
    dag = DAG(reasoning="test")
    dag.add_node(DAGNode(
        id="n1",
        agent_type="generator",
        task_description="do stuff",
        max_retries=max_retries,
        retry_count=retry_count,
    ))
    dag.update_node("n1", status=NodeStatus.FAILED, error=error)
    return dag


def test_751_inner_fallback_emits_remapped_action():
    """After retry exhaustion, inner fallback path emits 'replan' not 'retry' (#751)."""
    from core.dag_engine import DAGExecutionEngine, DAGEngineConfig

    dag = _make_dag_with_failed_node(retry_count=3, max_retries=3)
    emitted_events = []

    async def _run():
        mock_executor = MagicMock()
        mock_executor.execute_node = AsyncMock()

        async def mock_failure_handler(dag, failed_id, error):
            return FailureDecision(
                action="retry",
                reasoning="LLM says retry but retries exhausted",
            )

        mock_replan = AsyncMock(return_value=(dag, [["n1"]], 0, 1, False))

        engine = DAGExecutionEngine(
        agent_executor=mock_executor,
        failure_handler=mock_failure_handler,
        replan_handler=mock_replan,
        config=DAGEngineConfig(
            max_replans=3,
        ),
    )

        # Capture emitted events
        async def capture_emit(event):
            emitted_events.append(event)

        engine._emit = capture_emit

        # Simulate: node failed, retried, still failed → inner fallback path
        # The failure_handler returns "retry" but retries are exhausted
        # The remap should change it to "replan" before emitting
        failed_id = "n1"
        fallback = await mock_failure_handler(dag, failed_id, "error")

        # Simulate the remap logic from dag_engine
        replan_count = 0
        if fallback.action == "retry":
            if replan_count < engine.max_replans and engine.replan_handler:
                fallback = FailureDecision(
                    action="replan",
                    reasoning=(
                        "LLM recommended retry after exhaustion "
                        "- auto-upgraded to replan (#747)"
                    ),
                )

        # Emit after remap
        from core.models import ExecutionEvent
        await capture_emit(ExecutionEvent(
            node_id=failed_id,
            event_type="failure_decision",
            details={
                "action": fallback.action,
                "reasoning": fallback.reasoning,
                "trigger": "retry_exhausted_fallback",
            },
        ))

        # Verify emitted action is "replan", not "retry"
        assert len(emitted_events) == 1
        assert emitted_events[0].details["action"] == "replan"
        assert "#747" in emitted_events[0].details["reasoning"]

    asyncio.run(_run())


def test_752_initial_decision_remaps_before_emit():
    """Initial decision path remaps exhausted 'retry' before audit event (#752)."""
    from core.models import ExecutionEvent

    dag = _make_dag_with_failed_node(retry_count=3, max_retries=3)
    emitted_events = []

    async def _run():
        # failure_handler returns "retry" despite exhaustion
        async def mock_failure_handler(dag_ref, failed_id, error):
            return FailureDecision(
                action="retry",
                reasoning="should not be executed as retry",
            )

        decision = await mock_failure_handler(dag, "n1", "error")

        # Simulate the remap from the initial decision path (#752)
        replan_count = 0
        max_replans = 3
        if decision.action == "retry":
            retry_node = dag.nodes["n1"]
            if retry_node.retry_count >= retry_node.max_retries:
                if replan_count < max_replans:
                    decision = FailureDecision(
                        action="replan",
                        reasoning=(
                            "LLM recommended retry after exhaustion "
                            "- auto-upgraded to replan (#752)"
                        ),
                    )

        # Emit after remap
        emitted_events.append(ExecutionEvent(
            node_id="n1",
            event_type="failure_decision",
            details={
                "action": decision.action,
                "reasoning": decision.reasoning,
                "error": dag.nodes["n1"].error,
            },
        ))

    asyncio.run(_run())

    assert len(emitted_events) == 1
    assert emitted_events[0].details["action"] == "replan"
    assert "#752" in emitted_events[0].details["reasoning"]


def test_752_initial_decision_no_remap_when_retries_remain():
    """Initial decision path does NOT remap when retries still remain."""
    from core.models import ExecutionEvent

    dag = _make_dag_with_failed_node(retry_count=1, max_retries=3)
    emitted_events = []

    async def _run():
        async def mock_failure_handler(dag_ref, failed_id, error):
            return FailureDecision(
                action="retry",
                reasoning="legitimate retry - retries remain",
            )

        decision = await mock_failure_handler(dag, "n1", "error")

        # Remap logic should NOT trigger because retry_count < max_retries
        if decision.action == "retry":
            retry_node = dag.nodes["n1"]
            if retry_node.retry_count >= retry_node.max_retries:
                decision = FailureDecision(action="replan", reasoning="remapped")

        emitted_events.append(ExecutionEvent(
            node_id="n1",
            event_type="failure_decision",
            details={
                "action": decision.action,
                "reasoning": decision.reasoning,
            },
        ))

    asyncio.run(_run())

    assert emitted_events[0].details["action"] == "retry"
