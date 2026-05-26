"""Integration tests for #751/#752/#770: retry-exhaustion remap via execute().

Unlike test_751_752_audit_event_remap.py (which copies the remap logic),
these tests call DAGExecutionEngine.execute() end-to-end and verify that
the emitted audit events contain the remapped action, not the raw "retry".
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from core.models import DAG, DAGNode, NodeStatus, ExecutionEvent
from core.dag_models import FailureDecision

pytestmark = pytest.mark.asyncio(loop_scope="function")

_AGENT_RESULT = {
    "summary": "done",
    "output": "ok",
    "token_usage": {"input_tokens": 10, "output_tokens": 5},
}


def _make_engine(
    failure_handler,
    replan_handler=None,
    max_replans=3,
):
    """Create a DAGExecutionEngine with minimal mocked dependencies."""
    from core.dag_engine import DAGExecutionEngine, DAGEngineConfig

    async def mock_agent_executor(node, artifacts):
        return _AGENT_RESULT

    engine = DAGExecutionEngine(
        agent_executor=mock_agent_executor,
        failure_handler=failure_handler,
        replan_handler=replan_handler,
        config=DAGEngineConfig(
            max_replans=max_replans,
            max_parallel=1,
        ),
    )
    return engine


async def test_initial_path_remaps_retry_to_replan_when_exhausted():
    """Initial decision path: failure_handler returns 'retry' but retries
    exhausted -> engine remaps to 'replan', emitted event is not 'retry' (#752/#770)."""
    emitted_events: list[ExecutionEvent] = []

    async def capture_emit(event):
        emitted_events.append(event)

    async def always_retry(dag, failed_id, error):
        return FailureDecision(action="retry", reasoning="LLM says retry")

    # replan_handler returns (dag, levels, level_idx, replan_count, initiated)
    async def mock_replan(dag, failed_id, levels, level_idx, replan_count):
        new_dag = DAG(reasoning="replanned")
        return (new_dag, [["gen_1"]], 0, replan_count + 1, True)

    engine = _make_engine(
        failure_handler=always_retry,
        replan_handler=mock_replan,
    )
    engine._emit = capture_emit

    # Node already exhausted retries
    dag = DAG(reasoning="test")
    dag.add_node(DAGNode(
        id="gen_1",
        agent_type="generator",
        task_description="implement feature",
        max_retries=3,
        retry_count=3,
    ))
    dag.update_node("gen_1", status=NodeStatus.FAILED, error="timeout")

    await engine.execute(dag)

    failure_events = [
        e for e in emitted_events
        if e.event_type == "failure_decision"
    ]
    assert len(failure_events) >= 1
    for ev in failure_events:
        assert ev.details["action"] != "retry", (
            f"Expected remapped action but got 'retry': {ev.details}"
        )


async def test_initial_path_remaps_retry_to_skip_when_no_replan():
    """Initial decision path: 'retry' + exhausted + no replan handler
    -> engine emits 'skip', not 'retry' (#752/#770)."""
    emitted_events: list[ExecutionEvent] = []

    async def capture_emit(event):
        emitted_events.append(event)

    async def always_retry(dag, failed_id, error):
        return FailureDecision(action="retry", reasoning="LLM says retry")

    engine = _make_engine(
        failure_handler=always_retry,
        replan_handler=None,
    )
    engine._emit = capture_emit

    dag = DAG(reasoning="test")
    dag.add_node(DAGNode(
        id="gen_1",
        agent_type="generator",
        task_description="implement feature",
        max_retries=3,
        retry_count=3,
    ))
    dag.update_node("gen_1", status=NodeStatus.FAILED, error="timeout")

    await engine.execute(dag)

    failure_events = [
        e for e in emitted_events
        if e.event_type == "failure_decision"
    ]
    assert len(failure_events) >= 1
    for ev in failure_events:
        assert ev.details["action"] == "skip", (
            f"Expected 'skip' but got '{ev.details['action']}'"
        )


async def test_no_remap_when_retries_remain():
    """When retries remain, failure_handler returning 'retry' should NOT be
    remapped -- the engine actually retries the node (#770)."""
    emitted_events: list[ExecutionEvent] = []

    async def capture_emit(event):
        emitted_events.append(event)

    async def retry_once_then_skip(dag, failed_id, error):
        node = dag.nodes[failed_id]
        if node.retry_count < node.max_retries:
            return FailureDecision(action="retry", reasoning="legitimate retry")
        return FailureDecision(action="skip", reasoning="giving up")

    engine = _make_engine(failure_handler=retry_once_then_skip)
    engine._emit = capture_emit

    dag = DAG(reasoning="test")
    dag.add_node(DAGNode(
        id="gen_1",
        agent_type="generator",
        task_description="implement feature",
        max_retries=3,
        retry_count=1,
    ))
    dag.update_node("gen_1", status=NodeStatus.FAILED, error="first failure")

    await engine.execute(dag)

    failure_events = [
        e for e in emitted_events
        if e.event_type == "failure_decision"
    ]
    assert len(failure_events) >= 1
    first_action = failure_events[0].details["action"]
    assert first_action == "retry", (
        f"First decision should be 'retry' (retries remain), got '{first_action}'"
    )
