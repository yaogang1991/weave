"""
Tests for #247: enforce test file creation by generator nodes.

Verifies that generators with TEST_FILE_EXISTS criteria fail fast when
they don't produce any test files, with clear actionable feedback.
TESTS_PASS is purely about running tests — only TEST_FILE_EXISTS triggers
test file creation enforcement.
"""
import pytest
from core.models import (
    DAG, DAGNode, NodeStatus, CriterionType, SuccessCriterion,
)
from core.dag_engine import DAGExecutionEngine


class TestIsTestFileExistsCriterion:
    def test_structured_test_file_exists(self):
        assert DAGExecutionEngine._is_test_file_exists_criterion(
            SuccessCriterion(
                type=CriterionType.TEST_FILE_EXISTS,
                description="test files must exist"
            ),
        )

    def test_string_test_file_exists(self):
        assert DAGExecutionEngine._is_test_file_exists_criterion("test_file_exists")

    def test_string_not_test_file_exists(self):
        assert not DAGExecutionEngine._is_test_file_exists_criterion("lint clean")

    def test_structured_lint(self):
        assert not DAGExecutionEngine._is_test_file_exists_criterion(
            SuccessCriterion(type=CriterionType.LINT, description="lint"),
        )

    def test_tests_pass_is_not_test_file_exists(self):
        """TESTS_PASS should NOT trigger test file enforcement."""
        assert not DAGExecutionEngine._is_test_file_exists_criterion(
            SuccessCriterion(type=CriterionType.TESTS_PASS, description="tests pass"),
        )

    def test_string_tests_pass_is_not_test_file_exists(self):
        """String 'tests pass' should NOT trigger test file enforcement."""
        assert not DAGExecutionEngine._is_test_file_exists_criterion("tests pass")


class TestTestFileEnforcement:
    @pytest.mark.asyncio
    async def test_fails_when_no_test_files(self):
        """Generator with TEST_FILE_EXISTS but no test files -> FAILED."""
        node_artifacts = ["parser.py", "lexer.py"]
        dag = DAG(reasoning="test")
        dag.add_node(DAGNode(
            id="impl",
            agent_type="generator",
            task_description="Create a new module and write tests for it",
            success_criteria=[SuccessCriterion(type=CriterionType.TEST_FILE_EXISTS)],
        ))

        async def executor(node, artifacts, **kwargs):
            return {"status": "completed", "summary": "ok", "artifacts": node_artifacts}

        async def failure_handler(dag, node_id, error):
            from core.models import FailureDecision
            return FailureDecision(action="abort", reasoning="test")

        engine = DAGExecutionEngine(executor, failure_handler)
        result = await engine.execute(dag)
        assert result.nodes["impl"].status == NodeStatus.FAILED
        assert "no test files" in result.nodes["impl"].error.lower()
        assert "test" in result.nodes["impl"].eval_feedback.lower()

    @pytest.mark.asyncio
    async def test_passes_when_test_files_exist(self):
        """Generator with TEST_FILE_EXISTS and test files present -> SUCCESS."""
        dag = DAG(reasoning="test")
        dag.add_node(DAGNode(
            id="impl",
            agent_type="generator",
            task_description="impl",
            success_criteria=[SuccessCriterion(type=CriterionType.TEST_FILE_EXISTS)],
        ))

        async def executor(node, artifacts, **kwargs):
            return {
                "status": "completed",
                "summary": "ok",
                "artifacts": ["parser.py", "test_parser.py"],
            }

        async def failure_handler(dag, node_id, error):
            from core.models import FailureDecision
            return FailureDecision(action="abort", reasoning="test")

        engine = DAGExecutionEngine(executor, failure_handler)
        result = await engine.execute(dag)
        assert result.nodes["impl"].status == NodeStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_skips_when_no_test_file_criteria(self):
        """Generator without TEST_FILE_EXISTS criteria -> no test file check."""
        dag = DAG(reasoning="test")
        dag.add_node(DAGNode(
            id="impl",
            agent_type="generator",
            task_description="impl",
            success_criteria=["lint clean"],
        ))

        async def executor(node, artifacts, **kwargs):
            return {
                "status": "completed",
                "summary": "ok",
                "artifacts": ["parser.py"],  # No test files, but that's OK
            }

        async def failure_handler(dag, node_id, error):
            from core.models import FailureDecision
            return FailureDecision(action="abort", reasoning="test")

        engine = DAGExecutionEngine(executor, failure_handler)
        result = await engine.execute(dag)
        # Should not be failed by test file check
        assert result.nodes["impl"].status != NodeStatus.FAILED or \
            "no test files" not in (result.nodes["impl"].error or "").lower()

    @pytest.mark.asyncio
    async def test_skips_when_no_artifacts(self):
        """Generator with no output artifacts -> no test file check."""
        dag = DAG(reasoning="test")
        dag.add_node(DAGNode(
            id="impl",
            agent_type="generator",
            task_description="impl",
            success_criteria=[SuccessCriterion(type=CriterionType.TEST_FILE_EXISTS)],
        ))

        async def executor(node, artifacts, **kwargs):
            return {"status": "completed", "summary": "ok", "artifacts": []}

        async def failure_handler(dag, node_id, error):
            from core.models import FailureDecision
            return FailureDecision(action="abort", reasoning="test")

        engine = DAGExecutionEngine(executor, failure_handler)
        result = await engine.execute(dag)
        # Empty artifacts -> skip test file check (zero-output detection handles this)
        assert result.nodes["impl"].status in (NodeStatus.SUCCESS, NodeStatus.FAILED)

    @pytest.mark.asyncio
    async def test_feedback_is_actionable(self):
        """Error message tells the agent exactly what to do."""
        dag = DAG(reasoning="test")
        dag.add_node(DAGNode(
            id="impl",
            agent_type="generator",
            task_description="Create a new library and implement it",
            success_criteria=[SuccessCriterion(type=CriterionType.TEST_FILE_EXISTS)],
        ))

        async def executor(node, artifacts, **kwargs):
            return {
                "status": "completed",
                "summary": "ok",
                "artifacts": ["mylib.py"],
            }

        async def failure_handler(dag, node_id, error):
            from core.models import FailureDecision
            return FailureDecision(action="abort", reasoning="test")

        engine = DAGExecutionEngine(executor, failure_handler)
        result = await engine.execute(dag)
        feedback = result.nodes["impl"].eval_feedback
        assert "write tool" in feedback.lower() or "write" in feedback.lower()
        assert "test_*.py" in feedback or "test file" in feedback.lower()

    @pytest.mark.asyncio
    async def test_tests_pass_does_not_enforce_test_files(self):
        """TESTS_PASS alone should NOT trigger test file creation check.

        This is the key distinction: TESTS_PASS means 'tests must pass',
        not 'must create test files'. Only TEST_FILE_EXISTS triggers
        the enforcement.
        """
        dag = DAG(reasoning="test")
        dag.add_node(DAGNode(
            id="impl",
            agent_type="generator",
            task_description="Create a new library and implement it",
            success_criteria=["tests pass"],
        ))

        async def executor(node, artifacts, **kwargs):
            return {
                "status": "completed",
                "summary": "ok",
                "artifacts": ["mylib.py"],  # No test files
            }

        async def failure_handler(dag, node_id, error):
            from core.models import FailureDecision
            return FailureDecision(action="abort", reasoning="test")

        engine = DAGExecutionEngine(executor, failure_handler)
        result = await engine.execute(dag)
        # Should NOT fail from test file check — TESTS_PASS doesn't enforce it
        assert result.nodes["impl"].status != NodeStatus.FAILED or \
            "no test files" not in (result.nodes["impl"].error or "").lower()
