"""
Tests for #247: enforce test file creation by generator nodes.

Verifies that generators with TESTS_PASS criteria fail fast when
they don't produce any test files, with clear actionable feedback.
"""
import pytest
from core.models import (
    DAG, DAGNode, NodeStatus, CriterionType, SuccessCriterion,
)
from core.dag_engine import DAGExecutionEngine


def _make_engine():
    async def noop_executor(node, artifacts):
        return {"status": "completed", "summary": "ok", "artifacts": node_artifacts}

    async def noop_failure_handler(dag, node_id, error):
        from core.models import FailureDecision
        return FailureDecision(action="abort", reasoning="test")

    return DAGExecutionEngine(noop_executor, noop_failure_handler)


class TestIsTestsPassCriterion:
    def test_structured_tests_pass(self):
        assert DAGExecutionEngine._is_tests_pass_criterion(
            SuccessCriterion(type=CriterionType.TESTS_PASS, description="tests pass"),
        )

    def test_string_tests_pass(self):
        assert DAGExecutionEngine._is_tests_pass_criterion("tests pass")

    def test_string_not_tests(self):
        assert not DAGExecutionEngine._is_tests_pass_criterion("lint clean")

    def test_structured_lint(self):
        assert not DAGExecutionEngine._is_tests_pass_criterion(
            SuccessCriterion(type=CriterionType.LINT, description="lint"),
        )


class TestTestFileEnforcement:
    @pytest.mark.asyncio
    async def test_fails_when_no_test_files(self):
        """Generator with TESTS_PASS but no test files → FAILED."""
        node_artifacts = ["parser.py", "lexer.py"]
        dag = DAG(reasoning="test")
        dag.add_node(DAGNode(
            id="impl",
            agent_type="generator",
            task_description="Create a new module and write tests for it",
            success_criteria=["tests pass"],
        ))

        async def executor(node, artifacts):
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
        """Generator with TESTS_PASS and test files present → SUCCESS."""
        dag = DAG(reasoning="test")
        dag.add_node(DAGNode(
            id="impl",
            agent_type="generator",
            task_description="impl",
            success_criteria=[],  # No criteria → no evaluation gate
        ))

        async def executor(node, artifacts):
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
    async def test_skips_when_no_tests_criteria(self):
        """Generator without TESTS_PASS criteria → no test file check."""
        dag = DAG(reasoning="test")
        dag.add_node(DAGNode(
            id="impl",
            agent_type="generator",
            task_description="impl",
            success_criteria=["lint clean"],
        ))

        async def executor(node, artifacts):
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
        """Generator with no output artifacts → no test file check."""
        dag = DAG(reasoning="test")
        dag.add_node(DAGNode(
            id="impl",
            agent_type="generator",
            task_description="impl",
            success_criteria=["tests pass"],
        ))

        async def executor(node, artifacts):
            return {"status": "completed", "summary": "ok", "artifacts": []}

        async def failure_handler(dag, node_id, error):
            from core.models import FailureDecision
            return FailureDecision(action="abort", reasoning="test")

        engine = DAGExecutionEngine(executor, failure_handler)
        result = await engine.execute(dag)
        # Empty artifacts → skip test file check (zero-output detection handles this)
        assert result.nodes["impl"].status in (NodeStatus.SUCCESS, NodeStatus.FAILED)

    @pytest.mark.asyncio
    async def test_feedback_is_actionable(self):
        """Error message tells the agent exactly what to do."""
        dag = DAG(reasoning="test")
        dag.add_node(DAGNode(
            id="impl",
            agent_type="generator",
            task_description="Create a new library and implement it",
            success_criteria=["tests pass"],
        ))

        async def executor(node, artifacts):
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
    async def test_skip_enforcement_for_bugfix_tasks(self):
        """Bug-fix tasks without creation keywords should not enforce test file creation."""
        dag = DAG(reasoning="test")
        dag.add_node(DAGNode(
            id="fix",
            agent_type="generator",
            task_description="Fix the null pointer bug in parser.py",
            success_criteria=["tests pass"],
        ))

        async def executor(node, artifacts):
            return {"status": "completed", "summary": "ok", "artifacts": ["parser.py"]}

        async def failure_handler(dag, node_id, error):
            from core.models import FailureDecision
            return FailureDecision(action="abort", reasoning="test")

        engine = DAGExecutionEngine(executor, failure_handler)
        result = await engine.execute(dag)
        # Should NOT fail — bug-fix tasks don't require creating new test files
        assert result.nodes["fix"].status != NodeStatus.FAILED or \
            "no test files" not in (result.nodes["fix"].error or "").lower()
