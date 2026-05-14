"""
Tests for #291: inject planned file path constraints into generator tasks.

When a generator node has file_exists/file_pattern success_criteria, the
task description is prepended with a CRITICAL constraint listing the exact
file paths the LLM must create.
"""
import pytest

from core.models import DAGNode, NodeStatus, SuccessCriterion, CriterionType
from agent.agent_pool import _inject_file_path_constraints


def _make_generator_node(criteria: list[SuccessCriterion], task: str = "Create files") -> DAGNode:
    return DAGNode(
        id="impl",
        agent_type="generator",
        task_description=task,
        success_criteria=criteria,
        status=NodeStatus.PENDING,
    )


class TestFilePathConstraints:
    def test_generator_with_file_exists_gets_constraint(self):
        """Generator with file_exists criteria gets path constraint prepended."""
        node = _make_generator_node([
            SuccessCriterion(type=CriterionType.FILE_EXISTS, path="src/parser.py"),
        ])
        result = _inject_file_path_constraints(node)
        assert "CRITICAL FILE PATH CONSTRAINT" in result
        assert "src/parser.py" in result
        assert result.startswith("CRITICAL")

    def test_generator_with_multiple_paths(self):
        """Multiple file_exists paths are all listed."""
        node = _make_generator_node([
            SuccessCriterion(type=CriterionType.FILE_EXISTS, path="src/a.py, src/b.py"),
        ])
        result = _inject_file_path_constraints(node)
        assert "src/a.py" in result
        assert "src/b.py" in result

    def test_generator_with_file_pattern_gets_constraint(self):
        """Generator with file_pattern criteria gets constraint."""
        node = _make_generator_node([
            SuccessCriterion(type=CriterionType.FILE_PATTERN, pattern="tests/test_*.py"),
        ])
        result = _inject_file_path_constraints(node)
        assert "CRITICAL FILE PATH CONSTRAINT" in result
        assert "tests/test_*.py" in result

    def test_generator_without_file_criteria_no_change(self):
        """Generator with no file-based criteria gets no constraint."""
        node = _make_generator_node([
            SuccessCriterion(type=CriterionType.LINT, description="lint clean"),
            SuccessCriterion(type=CriterionType.TESTS_PASS, description="tests pass"),
        ])
        result = _inject_file_path_constraints(node)
        assert result == node.task_description

    def test_non_generator_no_change(self):
        """Non-generator nodes (planner, evaluator) are not modified."""
        node = DAGNode(
            id="plan",
            agent_type="planner",
            task_description="Plan the implementation",
            success_criteria=[
                SuccessCriterion(type=CriterionType.FILE_EXISTS, path="plan.json"),
            ],
            status=NodeStatus.PENDING,
        )
        result = _inject_file_path_constraints(node)
        assert result == node.task_description

    def test_original_task_preserved(self):
        """Original task description is preserved after constraint."""
        node = _make_generator_node(
            [SuccessCriterion(type=CriterionType.FILE_EXISTS, path="lib.py")],
            task="Create a library for X",
        )
        result = _inject_file_path_constraints(node)
        assert "Create a library for X" in result

    def test_exact_issue_291_scenario(self):
        """#291 scenario: tests/test_integration.py must be created exactly."""
        node = _make_generator_node([
            SuccessCriterion(
                type=CriterionType.FILE_EXISTS,
                path="tests/test_integration.py",
                description="integration tests exist",
            ),
        ])
        result = _inject_file_path_constraints(node)
        assert "tests/test_integration.py" in result
        assert "Do NOT use alternative filenames" in result
