"""Tests for #377: FILE_CHANGED-only nodes should not trigger zero-output fast-fail.

When a generator's only file criteria is FILE_CHANGED (modify existing files),
zero output artifacts is expected — the node edits files that already exist,
it doesn't create new ones.
"""
from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent))

from core.models import CriterionType, DAGNode, SuccessCriterion
from core.dag_engine import DAGExecutionEngine


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
