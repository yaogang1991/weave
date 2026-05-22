"""Tests for M5.1 trace event emission during DAG execution."""
import pytest

from core.models import DAG, DAGNode, FailureDecision
from core.dag_engine import DAGExecutionEngine


def _make_linear_dag():
    dag = DAG(reasoning="test trace")
    dag.add_node(DAGNode(id="a", agent_type="generator", task_description="impl"))
    return dag


async def _noop_executor(node, artifacts, **kwargs):
    return {"status": "completed", "summary": "done", "artifacts": [], "output": "ok"}


async def _noop_failure_handler(dag, node_id, error):
    return FailureDecision(action="abort", reasoning="test")


class TestTraceEventEmission:
    @pytest.mark.asyncio
    async def test_run_start_and_end_emitted(self):
        dag = _make_linear_dag()
        collected = []

        async def collector(event):
            collected.append(event)

        engine = DAGExecutionEngine(_noop_executor, _noop_failure_handler)
        engine.on_event(collector)
        await engine.execute(dag)

        trace_events = [e for e in collected if e.event_type == "trace"]
        trace_types = [e.details.get("trace_type") for e in trace_events]
        assert "run_start" in trace_types
        assert "run_end" in trace_types

    @pytest.mark.asyncio
    async def test_node_start_and_end_emitted(self):
        dag = _make_linear_dag()
        collected = []

        async def collector(event):
            collected.append(event)

        engine = DAGExecutionEngine(_noop_executor, _noop_failure_handler)
        engine.on_event(collector)
        await engine.execute(dag)

        trace_events = [e for e in collected if e.event_type == "trace"]
        trace_types = [e.details.get("trace_type") for e in trace_events]
        assert "node_start" in trace_types
        assert "node_end" in trace_types

    @pytest.mark.asyncio
    async def test_run_end_has_token_totals(self):
        dag = _make_linear_dag()
        collected = []

        async def collector(event):
            collected.append(event)

        engine = DAGExecutionEngine(_noop_executor, _noop_failure_handler)
        engine.on_event(collector)
        await engine.execute(dag)

        run_end = next(
            e for e in collected
            if e.event_type == "trace" and e.details.get("trace_type") == "run_end"
        )
        assert "total_input_tokens" in run_end.details
        assert "total_output_tokens" in run_end.details
        assert "duration_ms" in run_end.details

    @pytest.mark.asyncio
    async def test_node_end_has_status(self):
        dag = _make_linear_dag()
        collected = []

        async def collector(event):
            collected.append(event)

        engine = DAGExecutionEngine(_noop_executor, _noop_failure_handler)
        engine.on_event(collector)
        await engine.execute(dag)

        node_end = next(
            e for e in collected
            if e.event_type == "trace" and e.details.get("trace_type") == "node_end"
        )
        assert node_end.details.get("status") == "success"
        assert node_end.node_id == "a"

    @pytest.mark.asyncio
    async def test_concurrent_nodes_produce_separate_traces(self):
        dag = DAG(reasoning="parallel")
        dag.add_node(DAGNode(id="a", agent_type="planner", task_description="plan"))
        dag.add_node(DAGNode(id="b1", agent_type="generator", task_description="impl1"))
        dag.add_node(DAGNode(id="b2", agent_type="generator", task_description="impl2"))
        dag.add_edge("a", "b1")
        dag.add_edge("a", "b2")

        collected = []

        async def collector(event):
            collected.append(event)

        engine = DAGExecutionEngine(_noop_executor, _noop_failure_handler)
        engine.on_event(collector)
        await engine.execute(dag)

        node_ends = [
            e for e in collected
            if e.event_type == "trace" and e.details.get("trace_type") == "node_end"
        ]
        node_ids = {e.node_id for e in node_ends}
        assert "b1" in node_ids
        assert "b2" in node_ids
