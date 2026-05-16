"""
Tests for core/dag_engine.py — DAG execution, evaluator integration, failure handling.
"""
import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock

from core.models import (
    DAG, DAGNode, NodeStatus, ExecutionEvent,
    FailureDecision, HandoffArtifact, EvaluationResult,
)
from core.dag_engine import DAGExecutionEngine


def _make_linear_dag(criteria=None):
    dag = DAG(reasoning="test")
    dag.add_node(DAGNode(
        id="a", agent_type="generator", task_description="impl",
        success_criteria=criteria or [],
    ))
    return dag


def _make_three_node_dag():
    dag = DAG(reasoning="test")
    dag.add_node(DAGNode(id="a", agent_type="planner", task_description="plan"))
    dag.add_node(DAGNode(id="b", agent_type="generator", task_description="impl"))
    dag.add_node(DAGNode(id="c", agent_type="evaluator", task_description="eval"))
    dag.add_edge("a", "b")
    dag.add_edge("b", "c")
    return dag


async def _noop_executor(node, artifacts, **kwargs):
    return {"status": "completed", "summary": "done", "artifacts": [], "output": "ok"}


async def _noop_failure_handler(dag, node_id, error):
    return FailureDecision(action="abort", reasoning="test")


class TestTopologicalExecution:
    @pytest.mark.asyncio
    async def test_single_node(self):
        dag = _make_linear_dag()
        engine = DAGExecutionEngine(_noop_executor, _noop_failure_handler)
        result = await engine.execute(dag)
        assert result.nodes["a"].status == NodeStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_three_nodes_sequential(self):
        dag = _make_three_node_dag()
        execution_order = []

        async def tracking_executor(node, artifacts, **kwargs):
            execution_order.append(node.id)
            return {"status": "completed", "summary": "done", "artifacts": []}

        engine = DAGExecutionEngine(tracking_executor, _noop_failure_handler)
        await engine.execute(dag)
        assert execution_order == ["a", "b", "c"]

    @pytest.mark.asyncio
    async def test_parallel_nodes(self):
        dag = DAG()
        dag.add_node(DAGNode(id="a", agent_type="planner", task_description="plan"))
        dag.add_node(DAGNode(id="b1", agent_type="generator", task_description="impl1"))
        dag.add_node(DAGNode(id="b2", agent_type="generator", task_description="impl2"))
        dag.add_node(DAGNode(id="c", agent_type="evaluator", task_description="eval"))
        dag.add_edge("a", "b1")
        dag.add_edge("a", "b2")
        dag.add_edge("b1", "c")
        dag.add_edge("b2", "c")

        execution_order = []

        async def tracking_executor(node, artifacts, **kwargs):
            execution_order.append(node.id)
            return {"status": "completed", "summary": "done", "artifacts": []}

        engine = DAGExecutionEngine(tracking_executor, _noop_failure_handler)
        await engine.execute(dag)
        assert execution_order[0] == "a"
        assert set(execution_order[1:3]) == {"b1", "b2"}
        assert execution_order[3] == "c"


class TestFailureHandling:
    @pytest.mark.asyncio
    async def test_failure_aborts_remaining(self):
        dag = _make_three_node_dag()

        async def fail_on_b(node, artifacts, **kwargs):
            if node.id == "b":
                raise RuntimeError("boom")
            return {"status": "completed", "summary": "ok", "artifacts": []}

        engine = DAGExecutionEngine(fail_on_b, _noop_failure_handler)
        result = await engine.execute(dag)
        assert result.nodes["a"].status == NodeStatus.SUCCESS
        assert result.nodes["b"].status == NodeStatus.FAILED
        assert result.nodes["c"].status == NodeStatus.SKIPPED

    @pytest.mark.asyncio
    async def test_failure_retry_then_success(self):
        dag = _make_linear_dag()
        dag.nodes["a"].max_retries = 2
        call_count = 0

        async def fail_once(node, artifacts, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("transient")
            return {"status": "completed", "summary": "ok", "artifacts": []}

        async def retry_handler(dag, node_id, error):
            return FailureDecision(action="retry", reasoning="try again")

        engine = DAGExecutionEngine(fail_once, retry_handler)
        result = await engine.execute(dag)
        assert result.nodes["a"].status == NodeStatus.SUCCESS
        assert call_count == 2


class TestEvaluatorIntegration:
    @pytest.mark.asyncio
    async def test_eval_passes(self):
        dag = _make_linear_dag(criteria=["tests pass"])

        async def exec_with_artifacts(node, artifacts, **kwargs):
            return {"status": "completed", "summary": "done", "artifacts": ["impl.py"]}

        mock_eval = MagicMock()
        mock_eval.evaluate_stage = MagicMock(return_value=EvaluationResult(
            passed=True, score=10.0, feedback="OK",
        ))
        engine = DAGExecutionEngine(exec_with_artifacts, _noop_failure_handler, evaluator=mock_eval, work_dir="/tmp/test_workdir")
        result = await engine.execute(dag)
        assert result.nodes["a"].status == NodeStatus.SUCCESS
        mock_eval.evaluate_stage.assert_called_once()

    @pytest.mark.asyncio
    async def test_eval_fails_marks_retrying(self):
        dag = _make_linear_dag(criteria=["tests pass"])
        dag.nodes["a"].max_retries = 2
        call_count = 0

        async def exec_fn(node, artifacts, **kwargs):
            return {"status": "completed", "summary": "ok", "artifacts": ["impl.py"]}

        mock_eval = MagicMock()
        mock_eval.evaluate_stage = MagicMock(return_value=EvaluationResult(
            passed=False, score=3.0, feedback="Tests failed",
        ))

        async def retry_handler(dag, node_id, error):
            return FailureDecision(action="retry", reasoning="retry")

        engine = DAGExecutionEngine(exec_fn, retry_handler, evaluator=mock_eval, work_dir="/tmp/test_workdir")
        result = await engine.execute(dag)
        # First attempt fails eval -> RETRYING, then retry also fails eval -> FAILED
        assert result.nodes["a"].status == NodeStatus.FAILED
        assert "evaluation failed" in result.nodes["a"].error.lower()
        assert result.nodes["a"].eval_feedback.startswith("Tests failed")

    @pytest.mark.asyncio
    async def test_no_evaluator_skips_evaluation(self):
        dag = _make_linear_dag(criteria=["tests pass"])

        async def exec_with_artifacts(node, artifacts, **kwargs):
            return {"status": "completed", "summary": "done", "artifacts": ["impl.py"]}

        engine = DAGExecutionEngine(exec_with_artifacts, _noop_failure_handler, evaluator=None)
        result = await engine.execute(dag)
        assert result.nodes["a"].status == NodeStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_no_criteria_skips_evaluation(self):
        mock_eval = MagicMock()
        dag = _make_linear_dag(criteria=[])
        engine = DAGExecutionEngine(_noop_executor, _noop_failure_handler, evaluator=mock_eval)
        result = await engine.execute(dag)
        assert result.nodes["a"].status == NodeStatus.SUCCESS
        mock_eval.evaluate_stage.assert_not_called()


class TestHandoffArtifacts:
    @pytest.mark.asyncio
    async def test_artifacts_passed_between_nodes(self):
        dag = DAG()
        dag.add_node(DAGNode(id="a", agent_type="generator", task_description="gen"))
        dag.add_node(DAGNode(id="b", agent_type="evaluator", task_description="eval"))
        dag.add_edge("a", "b")

        received_artifacts = []

        async def capturing_executor(node, artifacts, **kwargs):
            if node.id == "b":
                received_artifacts.extend(artifacts)
            return {"status": "completed", "summary": f"{node.id} done", "artifacts": [f"{node.id}_file.py"]}

        engine = DAGExecutionEngine(capturing_executor, _noop_failure_handler)
        result = await engine.execute(dag)
        assert len(received_artifacts) == 1
        assert received_artifacts[0].from_agent == "generator"
        assert received_artifacts[0].file_paths == ["a_file.py"]


class TestEventEmission:
    @pytest.mark.asyncio
    async def test_events_emitted_in_order(self):
        dag = _make_linear_dag()
        events = []

        async def event_handler(event):
            events.append(event.event_type)

        engine = DAGExecutionEngine(_noop_executor, _noop_failure_handler)
        engine.on_event(event_handler)
        await engine.execute(dag)
        assert events == ["started", "completed"]
