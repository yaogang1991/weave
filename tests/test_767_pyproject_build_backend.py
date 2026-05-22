"""Tests for #767: auto-fix invalid pyproject.toml build-backend.

Verifies that:
1. Invalid build-backend is auto-corrected
2. Valid build-backend is left unchanged
3. Non-generator nodes are skipped
4. No crash when work_dir not set or file not found
"""
import os
import tempfile

from core.evaluation_pipeline import EvaluationPipeline
from core.models import DAG, DAGNode, NodeStatus
from core.quality_gate import QualityGate
from core.retry_policy import RetryPolicyEngine
from unittest.mock import MagicMock


def _make_pipeline(work_dir=None):
    """Create EvaluationPipeline with optional work_dir."""
    return EvaluationPipeline(
        evaluator=None,
        quality_gate=QualityGate(),
        work_dir=work_dir,
    )


def test_fixes_invalid_build_backend():
    """Invalid build-backend is auto-corrected to setuptools.build_meta (#767)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create pyproject.toml with invalid build-backend
        content = (
            '[build-system]\n'
            'requires = ["setuptools"]\n'
            'build-backend = "setuptools.backends._legacy:_Backend"\n'
        )
        pyproject_path = os.path.join(tmpdir, "pyproject.toml")
        with open(pyproject_path, "w") as f:
            f.write(content)

        pipeline = _make_pipeline(work_dir=tmpdir)

        dag = DAG(reasoning="test")
        dag.add_node(DAGNode(
            id="impl_1",
            agent_type="generator",
            task_description="Create project",
        ))
        dag.update_node(
            "impl_1",
            status=NodeStatus.SUCCESS,
            output_artifacts=["pyproject.toml"],
        )

        pipeline._fix_pyproject_build_backend(dag, "impl_1")

        with open(pyproject_path) as f:
            fixed = f.read()

        assert "setuptools.build_meta" in fixed
        assert "setuptools.backends._legacy" not in fixed


def test_valid_build_backend_unchanged():
    """Valid build-backend is not modified (#767)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        content = (
            '[build-system]\n'
            'requires = ["setuptools"]\n'
            'build-backend = "setuptools.build_meta"\n'
        )
        pyproject_path = os.path.join(tmpdir, "pyproject.toml")
        with open(pyproject_path, "w") as f:
            f.write(content)

        pipeline = _make_pipeline(work_dir=tmpdir)

        dag = DAG(reasoning="test")
        dag.add_node(DAGNode(
            id="impl_1",
            agent_type="generator",
            task_description="Create project",
        ))
        dag.update_node(
            "impl_1",
            status=NodeStatus.SUCCESS,
            output_artifacts=["pyproject.toml"],
        )

        pipeline._fix_pyproject_build_backend(dag, "impl_1")

        with open(pyproject_path) as f:
            result = f.read()

        assert result == content


def test_non_generator_node_skipped():
    """Non-generator nodes are not checked (#767)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        content = (
            '[build-system]\n'
            'build-backend = "setuptools.backends._legacy:_Backend"\n'
        )
        pyproject_path = os.path.join(tmpdir, "pyproject.toml")
        with open(pyproject_path, "w") as f:
            f.write(content)

        pipeline = _make_pipeline(work_dir=tmpdir)

        dag = DAG(reasoning="test")
        dag.add_node(DAGNode(
            id="eval_1",
            agent_type="evaluator",
            task_description="Evaluate",
        ))
        dag.update_node(
            "eval_1",
            status=NodeStatus.SUCCESS,
            output_artifacts=["pyproject.toml"],
        )

        pipeline._fix_pyproject_build_backend(dag, "eval_1")

        # File should be unchanged
        with open(pyproject_path) as f:
            assert "setuptools.backends._legacy" in f.read()


def test_no_crash_without_work_dir():
    """No crash when work_dir is not configured (#767)."""
    pipeline = _make_pipeline(work_dir=None)

    dag = DAG(reasoning="test")
    dag.add_node(DAGNode(
        id="impl_1",
        agent_type="generator",
        task_description="Create project",
    ))
    dag.update_node(
        "impl_1",
        status=NodeStatus.SUCCESS,
        output_artifacts=["pyproject.toml"],
    )

    # Should not raise
    pipeline._fix_pyproject_build_backend(dag, "impl_1")


def test_no_crash_missing_file():
    """No crash when referenced file does not exist (#767)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        pipeline = _make_pipeline(work_dir=tmpdir)

        dag = DAG(reasoning="test")
        dag.add_node(DAGNode(
            id="impl_1",
            agent_type="generator",
            task_description="Create project",
        ))
        dag.update_node(
            "impl_1",
            status=NodeStatus.SUCCESS,
            output_artifacts=["pyproject.toml"],
        )

        # File doesn't exist — should not raise
        pipeline._fix_pyproject_build_backend(dag, "impl_1")
