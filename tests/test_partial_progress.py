"""
Tests for #154: partial progress awareness in retry regression logic.

When score stays the same but issues change (especially lint-only),
the retry system should allow continued retry instead of aborting.
"""
import pytest
from unittest.mock import MagicMock, AsyncMock

from core.dag_engine import DAGExecutionEngine
from core.models import DAG, DAGNode, NodeStatus


def _make_engine(tmp_path, eval_side_effect):
    """Create a DAGExecutionEngine with mocked evaluator."""
    async def mock_executor(node, artifacts, **kwargs):
        return {"artifacts": ["main.py"]}

    mock_evaluator = MagicMock()
    mock_evaluator.evaluate_stage.side_effect = eval_side_effect

    return DAGExecutionEngine(
        agent_executor=mock_executor,
        failure_handler=AsyncMock(),
        work_dir=str(tmp_path),
        evaluator=mock_evaluator,
    )


def _make_dag():
    """Create a DAG with a single generator node that has success criteria."""
    dag = DAG(reasoning="test")
    node = DAGNode(
        id="gen_1",
        agent_type="generator",
        task_description="test",
        status=NodeStatus.PENDING,
        success_criteria=["tests pass", "lint clean"],
    )
    dag.add_node(node)
    return dag


class TestPartialProgress:
    """Score stays same, issues change — should allow retry."""

    @pytest.mark.asyncio
    async def test_lint_only_issues_not_treated_as_regression(self, tmp_path):
        """Score same, all issues are lint-only → NOT regression, retry allowed."""
        (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")

        # First eval: score 7.5, lint issues
        eval1 = MagicMock(
            passed=False, score=7.5, feedback="lint issues",
            metadata={"lint_new_issues": ["E501 line 10"], "lint_all_issues": ["E501 line 10"]},
        )
        # Second eval: score 7.5, different lint issues (same score, issues shifted)
        eval2 = MagicMock(
            passed=False, score=7.5, feedback="lint issues at different line",
            metadata={"lint_new_issues": ["E501 line 20"], "lint_all_issues": ["E501 line 20"]},
        )

        engine = _make_engine(tmp_path, [eval1, eval2])
        dag = _make_dag()

        # First attempt
        await engine._execute_single_node(dag, "gen_1")
        assert dag.nodes["gen_1"].status == NodeStatus.FAILED
        assert engine._best_attempts["gen_1"]["score"] == 7.5

        # Second attempt (retry)
        dag.nodes["gen_1"].status = NodeStatus.RETRYING
        dag.nodes["gen_1"].error = ""
        dag.nodes["gen_1"].retry_count = 0

        await engine._execute_single_node(dag, "gen_1")
        # Should still be FAILED but NOT due to regression
        assert dag.nodes["gen_1"].status == NodeStatus.FAILED
        # Best should be updated (not regression)
        best = engine._best_attempts["gen_1"]
        assert "E501 line 20" in best["lint_issues"]

    @pytest.mark.asyncio
    async def test_score_same_issues_fixed_allows_retry(self, tmp_path):
        """Score same but some issues fixed → update best, not regression."""
        (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")

        eval1 = MagicMock(
            passed=False, score=7.5, feedback="issues",
            metadata={
                "lint_new_issues": ["E501 line 10", "F401 import os"],
                "lint_all_issues": ["E501 line 10", "F401 import os"],
            },
        )
        eval2 = MagicMock(
            passed=False, score=7.5, feedback="fewer issues",
            metadata={
                "lint_new_issues": ["E501 line 10"],
                "lint_all_issues": ["E501 line 10"],
            },
        )

        engine = _make_engine(tmp_path, [eval1, eval2])
        dag = _make_dag()

        await engine._execute_single_node(dag, "gen_1")

        dag.nodes["gen_1"].status = NodeStatus.RETRYING
        dag.nodes["gen_1"].error = ""
        dag.nodes["gen_1"].retry_count = 0

        await engine._execute_single_node(dag, "gen_1")
        # Not regression — best should be updated
        best = engine._best_attempts["gen_1"]
        assert len(best["lint_issues"]) == 1  # Fixed one issue

    @pytest.mark.asyncio
    async def test_functional_regression_still_detected(self, tmp_path):
        """Score drops + new functional issues → still regression."""
        (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")

        eval1 = MagicMock(
            passed=False, score=7.5, feedback="some issues",
            metadata={"lint_new_issues": ["E501 line 10"], "lint_all_issues": ["E501 line 10"]},
        )
        eval2 = MagicMock(
            passed=False, score=5.0, feedback="much worse",
            metadata={
                "lint_new_issues": ["import error", "syntax error"],
                "lint_all_issues": ["import error", "syntax error"],
            },
        )

        engine = _make_engine(tmp_path, [eval1, eval2])
        dag = _make_dag()

        await engine._execute_single_node(dag, "gen_1")

        dag.nodes["gen_1"].status = NodeStatus.RETRYING
        dag.nodes["gen_1"].error = ""
        dag.nodes["gen_1"].retry_count = 0

        await engine._execute_single_node(dag, "gen_1")
        # Should be FAILED — best NOT updated (regression)
        best = engine._best_attempts["gen_1"]
        assert best["score"] == 7.5  # Original best kept

    @pytest.mark.asyncio
    async def test_lint_whitespace_only_not_regression(self, tmp_path):
        """Whitespace/trailing lint issues → not treated as regression."""
        (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")

        eval1 = MagicMock(
            passed=False, score=7.5, feedback="lint",
            metadata={
                "lint_new_issues": ["W291 trailing whitespace"],
                "lint_all_issues": ["W291 trailing whitespace"],
            },
        )
        eval2 = MagicMock(
            passed=False, score=7.5, feedback="lint",
            metadata={
                "lint_new_issues": ["W291 whitespace line 5"],
                "lint_all_issues": ["W291 whitespace line 5"],
            },
        )

        engine = _make_engine(tmp_path, [eval1, eval2])
        dag = _make_dag()

        await engine._execute_single_node(dag, "gen_1")

        dag.nodes["gen_1"].status = NodeStatus.RETRYING
        dag.nodes["gen_1"].error = ""
        dag.nodes["gen_1"].retry_count = 0

        await engine._execute_single_node(dag, "gen_1")
        # Best should be updated (whitespace-only = lint-only)
        best = engine._best_attempts["gen_1"]
        assert "W291 whitespace line 5" in best["lint_issues"]
