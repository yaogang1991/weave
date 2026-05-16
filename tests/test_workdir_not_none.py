"""
Tests for #211: evaluator work_dir must never fall back to harness cwd.

Ensures:
- cmd_execute always sets project_work_dir (never None after _resolve_project_path)
- dag_engine fast-fails when work_dir is not set and evaluation is needed
"""
import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock
from pathlib import Path


class TestProjectWorkDirAlwaysSet:
    """After _resolve_project_path, args.project is always a string."""

    def test_resolve_project_path_returns_string(self):
        from main import _resolve_project_path
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            result = _resolve_project_path(tmp)
            assert result is not None
            assert isinstance(result, str)
            assert Path(result).is_absolute()

    def test_resolve_project_path_outside_harness(self):
        """Cwd outside harness tree returns cwd as string."""
        from main import _resolve_project_path
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            result = _resolve_project_path(tmp)
            assert Path(result).exists()


class TestDagEngineWorkDirFastFail:
    """DAG engine fast-fails when work_dir is None and evaluation is needed."""

    @pytest.mark.asyncio
    async def test_fast_fails_when_work_dir_unset(self, tmp_path):
        """When work_dir is None, node requiring evaluation fast-fails."""
        from core.dag_engine import DAGExecutionEngine
        from core.models import DAG, DAGNode, NodeStatus

        dag = DAG(reasoning="test")
        dag.add_node(DAGNode(
            id="gen",
            agent_type="generator",
            task_description="test",
            success_criteria=["tests pass"],
        ))

        async def executor(node, artifacts, **kwargs):
            return {"status": "completed", "summary": "done", "artifacts": ["impl.py"]}

        mock_evaluator = MagicMock()
        mock_evaluator.evaluate_stage.return_value = MagicMock(passed=True, score=10.0)

        engine = DAGExecutionEngine(
            agent_executor=executor,
            failure_handler=AsyncMock(return_value=MagicMock(action="abort")),
            work_dir=None,
            evaluator=mock_evaluator,
        )

        result = await engine.execute(dag)
        assert result.nodes["gen"].status == NodeStatus.FAILED
        assert "work_dir" in result.nodes["gen"].error.lower()

    @pytest.mark.asyncio
    async def test_succeeds_when_work_dir_set(self, tmp_path):
        """When work_dir is set, evaluation proceeds normally."""
        from core.dag_engine import DAGExecutionEngine
        from core.models import DAG, DAGNode, NodeStatus, EvaluationResult

        dag = DAG(reasoning="test")
        dag.add_node(DAGNode(
            id="gen",
            agent_type="generator",
            task_description="test",
            success_criteria=["tests pass"],
        ))

        async def executor(node, artifacts, **kwargs):
            return {"status": "completed", "summary": "done", "artifacts": ["impl.py"]}

        mock_evaluator = MagicMock()
        mock_evaluator.evaluate_stage.return_value = EvaluationResult(
            passed=True, score=10.0, feedback="OK",
        )

        engine = DAGExecutionEngine(
            agent_executor=executor,
            failure_handler=AsyncMock(return_value=MagicMock(action="abort")),
            work_dir=str(tmp_path),
            evaluator=mock_evaluator,
        )

        result = await engine.execute(dag)
        assert result.nodes["gen"].status == NodeStatus.SUCCESS
