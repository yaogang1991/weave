"""Tests for #767: auto-fix invalid pyproject.toml build-backend.

Verifies that:
1. Invalid build-backend is auto-corrected (to legacy-compatible value)
2. Valid build-backend is left unchanged
3. Non-generator nodes are skipped
4. No crash when work_dir not set or file not found
5. Path traversal is blocked
6. Uses workspace_path when provided (node isolation)
7. Only build-backend in [build-system] is rewritten
"""
import os
import tempfile

from core.evaluation_pipeline import EvaluationPipeline
from core.models import DAG, DAGNode, NodeStatus
from core.quality_gate import QualityGate


def _make_pipeline(work_dir=None):
    """Create EvaluationPipeline with optional work_dir."""
    return EvaluationPipeline(
        evaluator=None,
        quality_gate=QualityGate(),
        work_dir=work_dir,
    )


def _make_dag_with_generator(artifacts):
    """Create a DAG with a generator node having given output artifacts."""
    dag = DAG(reasoning="test")
    dag.add_node(DAGNode(
        id="impl_1",
        agent_type="generator",
        task_description="Create project",
    ))
    dag.update_node(
        "impl_1",
        status=NodeStatus.SUCCESS,
        output_artifacts=artifacts,
    )
    return dag


def test_fixes_invalid_build_backend():
    """Invalid build-backend is auto-corrected to setuptools.build_meta:__legacy__ (#767)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        content = (
            '[build-system]\n'
            'requires = ["setuptools"]\n'
            'build-backend = "setuptools.backends._legacy:_Backend"\n'
        )
        pyproject_path = os.path.join(tmpdir, "pyproject.toml")
        with open(pyproject_path, "w") as f:
            f.write(content)

        pipeline = _make_pipeline(work_dir=tmpdir)
        dag = _make_dag_with_generator(["pyproject.toml"])
        pipeline._fix_pyproject_build_backend(dag, "impl_1")

        with open(pyproject_path) as f:
            fixed = f.read()

        assert "setuptools.backends._legacy" not in fixed
        assert "setuptools.build_meta:__legacy__" in fixed


def test_fixes_invalid_build_backend_without_entry_point():
    """setuptools.backends._legacy (no entry point) is also fixed (#767)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        content = (
            '[build-system]\n'
            'requires = ["setuptools"]\n'
            'build-backend = "setuptools.backends._legacy"\n'
        )
        pyproject_path = os.path.join(tmpdir, "pyproject.toml")
        with open(pyproject_path, "w") as f:
            f.write(content)

        pipeline = _make_pipeline(work_dir=tmpdir)
        dag = _make_dag_with_generator(["pyproject.toml"])
        pipeline._fix_pyproject_build_backend(dag, "impl_1")

        with open(pyproject_path) as f:
            fixed = f.read()

        assert "setuptools.backends._legacy" not in fixed
        assert "setuptools.build_meta:__legacy__" in fixed


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
        dag = _make_dag_with_generator(["pyproject.toml"])
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

        with open(pyproject_path) as f:
            assert "setuptools.backends._legacy" in f.read()


def test_no_crash_without_work_dir():
    """No crash when work_dir is not configured (#767)."""
    pipeline = _make_pipeline(work_dir=None)
    dag = _make_dag_with_generator(["pyproject.toml"])

    # Should not raise
    pipeline._fix_pyproject_build_backend(dag, "impl_1")


def test_no_crash_missing_file():
    """No crash when referenced file does not exist (#767)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        pipeline = _make_pipeline(work_dir=tmpdir)
        dag = _make_dag_with_generator(["pyproject.toml"])

        # File doesn't exist — should not raise
        pipeline._fix_pyproject_build_backend(dag, "impl_1")


def test_path_traversal_blocked():
    """Path traversal via ../../ is rejected (#767)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create an outside file that should NOT be modified
        outside_dir = os.path.join(tmpdir, "outside")
        os.makedirs(outside_dir)
        outside_content = (
            '[build-system]\n'
            'build-backend = "setuptools.backends._legacy:_Backend"\n'
        )
        outside_path = os.path.join(outside_dir, "pyproject.toml")
        with open(outside_path, "w") as f:
            f.write(outside_content)

        # Work dir is a subdirectory
        work_dir = os.path.join(tmpdir, "workspace")
        os.makedirs(work_dir)

        pipeline = _make_pipeline(work_dir=work_dir)
        # Artifact path tries to escape work_dir
        dag = _make_dag_with_generator(["../outside/pyproject.toml"])

        pipeline._fix_pyproject_build_backend(dag, "impl_1")

        # Outside file should be unchanged
        with open(outside_path) as f:
            assert "setuptools.backends._legacy" in f.read()


def test_uses_workspace_path():
    """Uses workspace_path instead of work_dir when provided (#767)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace_dir = os.path.join(tmpdir, "node_workspace")
        os.makedirs(workspace_dir)

        content = (
            '[build-system]\n'
            'requires = ["setuptools"]\n'
            'build-backend = "setuptools.backends._legacy:_Backend"\n'
        )
        pyproject_path = os.path.join(workspace_dir, "pyproject.toml")
        with open(pyproject_path, "w") as f:
            f.write(content)

        # work_dir points to tmpdir, but workspace_path is the node workspace
        pipeline = _make_pipeline(work_dir=tmpdir)
        dag = _make_dag_with_generator(["pyproject.toml"])

        pipeline._fix_pyproject_build_backend(
            dag, "impl_1", workspace_path=workspace_dir,
        )

        with open(pyproject_path) as f:
            fixed = f.read()

        assert "setuptools.backends._legacy" not in fixed


def test_only_build_backend_in_build_system_rewritten():
    """Invalid pattern in other sections is NOT rewritten (#767)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # The invalid string appears in a comment, not in build-backend value
        content = (
            '[build-system]\n'
            'requires = ["setuptools"]\n'
            'build-backend = "setuptools.build_meta"\n'
            '\n'
            '[tool.some]\n'
            'note = "setuptools.backends._legacy:_Backend is bad"\n'
        )
        pyproject_path = os.path.join(tmpdir, "pyproject.toml")
        with open(pyproject_path, "w") as f:
            f.write(content)

        pipeline = _make_pipeline(work_dir=tmpdir)
        dag = _make_dag_with_generator(["pyproject.toml"])
        pipeline._fix_pyproject_build_backend(dag, "impl_1")

        with open(pyproject_path) as f:
            result = f.read()

        # The comment in [tool.some] should be untouched
        assert "setuptools.backends._legacy:_Backend is bad" in result
        # The valid build-backend should remain unchanged
        assert 'build-backend = "setuptools.build_meta"' in result
