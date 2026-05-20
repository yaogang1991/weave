"""Tests for #377: FILE_CHANGED-only nodes should not trigger zero-output fast-fail.

When a generator's only file criteria is FILE_CHANGED (modify existing files),
zero output artifacts is expected — the node edits files that already exist,
it doesn't create new ones.
"""
from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent))

from core.models import CriterionType, DAGNode, SuccessCriterion  # noqa: E402
from core.dag_engine import DAGExecutionEngine  # noqa: E402


class TestRequiresOutputArtifacts:
    """Verify _requires_output_artifacts excludes FILE_CHANGED-only nodes."""

    def test_file_exists_requires_output(self):
        """FILE_EXISTS criteria → requires output artifacts."""
        node = DAGNode(
            id="n1",
            agent_type="generator",
            task_description="create parser.py",
            success_criteria=[
                SuccessCriterion(
                    type=CriterionType.FILE_EXISTS,
                    path="parser.py",
                    description="parser exists",
                ),
            ],
        )
        assert DAGExecutionEngine._requires_output_artifacts(node) is True

    def test_file_changed_only_does_not_require_output(self):
        """FILE_CHANGED-only criteria → does NOT require output (#377)."""
        node = DAGNode(
            id="n1",
            agent_type="generator",
            task_description="extend core.py",
            success_criteria=[
                SuccessCriterion(
                    type=CriterionType.FILE_CHANGED,
                    path="core.py",
                    description="core.py was modified",
                ),
            ],
        )
        assert DAGExecutionEngine._requires_output_artifacts(node) is False

    def test_file_changed_plus_file_exists_requires_output(self):
        """Mixed FILE_CHANGED + FILE_EXISTS → requires output."""
        node = DAGNode(
            id="n1",
            agent_type="generator",
            task_description="extend core.py and create utils.py",
            success_criteria=[
                SuccessCriterion(
                    type=CriterionType.FILE_CHANGED,
                    path="core.py",
                    description="core modified",
                ),
                SuccessCriterion(
                    type=CriterionType.FILE_EXISTS,
                    path="utils.py",
                    description="utils created",
                ),
            ],
        )
        assert DAGExecutionEngine._requires_output_artifacts(node) is True

    def test_no_criteria_does_not_require_output(self):
        """No file criteria → does not require output."""
        node = DAGNode(
            id="n1",
            agent_type="generator",
            task_description="analyze code",
            success_criteria=[
                SuccessCriterion(
                    type=CriterionType.CUSTOM,
                    description="analysis complete",
                ),
            ],
        )
        assert DAGExecutionEngine._requires_output_artifacts(node) is False

    def test_tests_pass_requires_output(self):
        """TESTS_PASS criteria → requires output."""
        node = DAGNode(
            id="n1",
            agent_type="generator",
            task_description="create and test module",
            success_criteria=[
                SuccessCriterion(
                    type=CriterionType.TESTS_PASS,
                    description="tests pass",
                ),
            ],
        )
        assert DAGExecutionEngine._requires_output_artifacts(node) is True

    def test_planner_with_file_exists_requires_output(self):
        """Planner with FILE_EXISTS criteria → still requires output."""
        node = DAGNode(
            id="plan1",
            agent_type="planner",
            task_description="plan DAG",
            success_criteria=[
                SuccessCriterion(
                    type=CriterionType.FILE_EXISTS,
                    path="plan.json",
                    description="plan file",
                ),
            ],
        )
        # Original behavior: FILE_EXISTS always requires output
        assert DAGExecutionEngine._requires_output_artifacts(node) is True

    def test_planner_with_no_criteria_does_not_require(self):
        """Planner with no file criteria → does not require output."""
        node = DAGNode(
            id="plan1",
            agent_type="planner",
            task_description="plan DAG",
            success_criteria=[],
        )
        assert DAGExecutionEngine._requires_output_artifacts(node) is False


# -- #626: Test node deep degeneration recovery --


class TestIsTestNode:
    """Verify _is_test_node heuristic (#626)."""

    def _make_node(self, task):
        return DAGNode(
            id="n1", agent_type="generator", task_description=task,
        )

    def test_explicit_test_task(self):
        from core.node_executor import NodeExecutor
        node = self._make_node("Write unit tests for the API module")
        assert NodeExecutor._is_test_node(node) is True

    def test_tests_keyword(self):
        from core.node_executor import NodeExecutor
        node = self._make_node("Create tests for authentication")
        assert NodeExecutor._is_test_node(node) is True

    def test_spec_keyword(self):
        from core.node_executor import NodeExecutor
        node = self._make_node("Write spec verification for user model")
        assert NodeExecutor._is_test_node(node) is True

    def test_implementation_task(self):
        from core.node_executor import NodeExecutor
        node = self._make_node("Implement REST API endpoints for users")
        assert NodeExecutor._is_test_node(node) is False

    def test_case_insensitive(self):
        from core.node_executor import NodeExecutor
        node = self._make_node("Add TEST coverage for the parser")
        assert NodeExecutor._is_test_node(node) is True


class TestCollectUpstreamArtifacts:
    """Verify _collect_upstream_artifacts (#626)."""

    def test_collects_from_successful_deps(self):
        from core.models import DAG, DAGNode, NodeStatus
        from core.dag_models import DAGEdge
        from core.node_executor import NodeExecutor

        impl_node = DAGNode(
            id="impl", agent_type="generator",
            task_description="Implement API",
            status=NodeStatus.SUCCESS,
            output_artifacts=["src/api.py", "src/models.py"],
        )
        test_node = DAGNode(
            id="test", agent_type="generator",
            task_description="Write tests for API",
            status=NodeStatus.RUNNING,
        )
        dag = DAG(
            nodes={"impl": impl_node, "test": test_node},
            edges=[DAGEdge(from_node="impl", to_node="test")],
        )
        result = NodeExecutor._collect_upstream_artifacts(dag, "test")
        assert result == ["src/api.py", "src/models.py"]

    def test_skips_failed_deps(self):
        from core.models import DAG, DAGNode, NodeStatus
        from core.dag_models import DAGEdge
        from core.node_executor import NodeExecutor

        impl_node = DAGNode(
            id="impl", agent_type="generator",
            task_description="Implement API",
            status=NodeStatus.FAILED,
            output_artifacts=["src/api.py"],
        )
        test_node = DAGNode(
            id="test", agent_type="generator",
            task_description="Write tests for API",
            status=NodeStatus.RUNNING,
        )
        dag = DAG(
            nodes={"impl": impl_node, "test": test_node},
            edges=[DAGEdge(from_node="impl", to_node="test")],
        )
        result = NodeExecutor._collect_upstream_artifacts(dag, "test")
        assert result == []

    def test_no_deps_returns_empty(self):
        from core.models import DAG, DAGNode
        from core.node_executor import NodeExecutor

        node = DAGNode(
            id="plan", agent_type="planner",
            task_description="Plan the project",
        )
        dag = DAG(nodes={"plan": node}, edges=[])
        result = NodeExecutor._collect_upstream_artifacts(dag, "plan")
        assert result == []
