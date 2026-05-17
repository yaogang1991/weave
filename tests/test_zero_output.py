"""
Tests for #229: fast-fail generator nodes with zero output.

Key scenarios:
1. Generator with zero artifacts -> FAILED, even without evaluator
2. Generator with artifacts -> SUCCESS (unchanged)
3. Planner/evaluator nodes never fast-failed for zero output
4. Generator with non-file criteria (TESTS_PASS only) -> not fast-failed
5. Generator with FILE_EXISTS criteria, zero output -> fast-failed
"""
import asyncio  # noqa: F401
import pytest
from unittest.mock import MagicMock

from core.models import (
    DAG, DAGNode, NodeStatus, FailureDecision,
    EvaluationResult, SuccessCriterion, CriterionType,
)
from core.dag_engine import DAGExecutionEngine


async def _noop_failure_handler(dag, node_id, error):
    return FailureDecision(action="abort", reasoning="test")


class TestZeroOutputFastFail:

    @pytest.mark.asyncio
    async def test_generator_zero_output_fails_without_evaluator(self):
        dag = DAG()
        dag.add_node(DAGNode(
            id="gen1", agent_type="generator",
            task_description="implement parser.py",
            success_criteria=["tests pass"],
        ))

        async def no_output_executor(node, artifacts, **kwargs):
            return {"status": "completed", "summary": "done", "artifacts": []}

        engine = DAGExecutionEngine(
            no_output_executor, _noop_failure_handler,
            work_dir="/tmp/test_workdir"
        )
        result = await engine.execute(dag)
        assert result.nodes["gen1"].status == NodeStatus.FAILED
        assert "zero output" in result.nodes["gen1"].error.lower()

    @pytest.mark.asyncio
    async def test_generator_zero_output_fails_with_evaluator(self):
        dag = DAG()
        dag.add_node(DAGNode(
            id="gen1", agent_type="generator",
            task_description="implement parser.py",
            success_criteria=["tests pass"],
        ))

        async def no_output_executor(node, artifacts, **kwargs):
            return {"status": "completed", "summary": "done", "artifacts": []}

        mock_eval = MagicMock()
        mock_eval.evaluate_stage = MagicMock(return_value=EvaluationResult(
            passed=True, score=10.0, feedback="OK",
        ))

        engine = DAGExecutionEngine(
            no_output_executor, _noop_failure_handler, evaluator=mock_eval,
            work_dir="/tmp/test_workdir",
        )
        result = await engine.execute(dag)
        assert result.nodes["gen1"].status == NodeStatus.FAILED
        assert "zero output" in result.nodes["gen1"].error.lower()
        mock_eval.evaluate_stage.assert_not_called()

    @pytest.mark.asyncio
    async def test_generator_with_artifacts_succeeds(self):
        dag = DAG()
        dag.add_node(DAGNode(
            id="gen1", agent_type="generator",
            task_description="implement parser.py",
            success_criteria=["tests pass"],
        ))

        async def executor_with_output(node, artifacts, **kwargs):
            return {"status": "completed", "summary": "done", "artifacts": ["parser.py"]}

        engine = DAGExecutionEngine(executor_with_output, _noop_failure_handler)
        result = await engine.execute(dag)
        assert result.nodes["gen1"].status == NodeStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_planner_zero_output_still_succeeds(self):
        dag = DAG()
        dag.add_node(DAGNode(
            id="plan1", agent_type="planner",
            task_description="plan the architecture",
            success_criteria=["tests pass"],
        ))

        async def no_output_executor(node, artifacts, **kwargs):
            return {"status": "completed", "summary": "done", "artifacts": []}

        engine = DAGExecutionEngine(no_output_executor, _noop_failure_handler)
        result = await engine.execute(dag)
        assert result.nodes["plan1"].status == NodeStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_evaluator_zero_output_still_succeeds(self):
        dag = DAG()
        dag.add_node(DAGNode(
            id="eval1", agent_type="evaluator",
            task_description="evaluate results",
            success_criteria=["tests pass"],
        ))

        async def no_output_executor(node, artifacts, **kwargs):
            return {"status": "completed", "summary": "done", "artifacts": []}

        engine = DAGExecutionEngine(no_output_executor, _noop_failure_handler)
        result = await engine.execute(dag)
        assert result.nodes["eval1"].status == NodeStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_generator_no_criteria_zero_output_succeeds(self):
        dag = DAG()
        dag.add_node(DAGNode(
            id="gen1", agent_type="generator",
            task_description="analyze codebase",
            success_criteria=[],
        ))

        async def no_output_executor(node, artifacts, **kwargs):
            return {"status": "completed", "summary": "done", "artifacts": []}

        engine = DAGExecutionEngine(no_output_executor, _noop_failure_handler)
        result = await engine.execute(dag)
        assert result.nodes["gen1"].status == NodeStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_file_exists_criteria_zero_output_fails(self):
        dag = DAG()
        dag.add_node(DAGNode(
            id="worker1", agent_type="worker",
            task_description="create config file",
            success_criteria=[
                SuccessCriterion(
                    type=CriterionType.FILE_EXISTS,
                    path="config.yaml",
                    description="config file must exist",
                ),
            ],
        ))

        async def no_output_executor(node, artifacts, **kwargs):
            return {"status": "completed", "summary": "done", "artifacts": []}

        engine = DAGExecutionEngine(
            no_output_executor, _noop_failure_handler,
            work_dir="/tmp/test_workdir"
        )
        result = await engine.execute(dag)
        assert result.nodes["worker1"].status == NodeStatus.FAILED
        assert "zero output" in result.nodes["worker1"].error.lower()

    @pytest.mark.asyncio
    async def test_zero_output_emits_failed_event(self):
        dag = DAG()
        dag.add_node(DAGNode(
            id="gen1", agent_type="generator",
            task_description="implement feature",
            success_criteria=["tests pass"],
        ))

        async def no_output_executor(node, artifacts, **kwargs):
            return {"status": "completed", "summary": "done", "artifacts": []}

        events = []

        async def event_handler(event):
            events.append(event)

        engine = DAGExecutionEngine(
            no_output_executor, _noop_failure_handler,
            work_dir="/tmp/test_workdir"
        )
        engine.on_event(event_handler)
        await engine.execute(dag)

        event_types = [e.event_type for e in events]
        assert "started" in event_types
        assert "failed" in event_types
        failed_events = [e for e in events if e.event_type == "failed"]
        assert failed_events[0].details["reason"] == "zero_output_artifacts"
