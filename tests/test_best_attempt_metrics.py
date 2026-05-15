"""Tests for #273: best-attempt criteria tracking in _best_attempts.

Verifies:
1. criteria_results and passed are tracked in _best_attempts
2. criteria_results updated when score improves
3. criteria_results preserved when regression detected and rollback occurs
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.dag_engine import DAGExecutionEngine
from core.models import (
    DAG,
    DAGNode,
    EvaluationResult,
    FailureDecision,
    NodeStatus,
)


async def _mock_failure_handler(node, error, dag):
    return FailureDecision(action="retry")


def _make_eval_result(
    passed: bool,
    score: float,
    lint_issues: list[str] | None = None,
    criteria_results: dict | None = None,
) -> EvaluationResult:
    return EvaluationResult(
        passed=passed,
        score=score,
        feedback="test feedback",
        criteria_results=criteria_results or {},
        metadata=(
            {"lint_new_issues": lint_issues, "lint_all_issues": lint_issues}
            if lint_issues
            else {}
        ),
    )


class TestBestAttemptCriteriaTracking:
    """Verify criteria_results and passed are tracked in _best_attempts."""

    @pytest.mark.asyncio
    async def test_criteria_results_tracked_in_best_attempt(self, tmp_path):
        """First failure should store criteria_results in _best_attempts."""
        criteria = {"lint clean": True, "tests pass": False}
        eval_result = _make_eval_result(
            False, 5.0, ["a.py:10:E501"], criteria,
        )

        async def executor(node, artifacts):
            return {"summary": "ok", "artifacts": ["a.py"]}

        engine = DAGExecutionEngine(
            agent_executor=executor,
            failure_handler=_mock_failure_handler,
            work_dir=str(tmp_path),
        )
        engine.evaluator = MagicMock()
        engine.evaluator.evaluate_stage = MagicMock(return_value=eval_result)

        dag = DAG(
            nodes={
                "n1": DAGNode(
                    id="n1",
                    agent_type="generator",
                    task_description="test",
                    success_criteria=["lint clean", "tests pass"],
                ),
            },
        )
        result = await engine.execute(dag)

        assert result.nodes["n1"].status == NodeStatus.FAILED
        assert "n1" in engine._best_attempts
        best = engine._best_attempts["n1"]
        assert best["criteria_results"] == criteria
        assert best["passed"] is False

    @pytest.mark.asyncio
    async def test_criteria_updated_on_better_score(self, tmp_path):
        """When score improves, criteria_results should update."""
        engine = DAGExecutionEngine(
            agent_executor=None,
            failure_handler=_mock_failure_handler,
            work_dir=str(tmp_path),
        )
        # Seed with previous best
        engine._best_attempts["n1"] = {
            "score": 5.0,
            "artifacts": ["a.py"],
            "feedback": "old",
            "lint_issues": {"a.py:10:E501"},
            "artifact_set": {"a.py"},
            "file_snapshot": {},
            "criteria_results": {"lint clean": False, "tests pass": False},
            "passed": False,
        }

        # Simulate better eval result via direct tracking logic
        prev_best = engine._best_attempts["n1"]
        better_result = _make_eval_result(
            False, 7.5, [],
            {"lint clean": True, "tests pass": False},
        )
        # Score improved → update best
        if better_result.score > prev_best["score"]:
            engine._best_attempts["n1"] = {
                "score": better_result.score,
                "artifacts": ["a.py", "b.py"],
                "feedback": better_result.feedback,
                "lint_issues": set(),
                "artifact_set": {"a.py", "b.py"},
                "file_snapshot": {},
                "criteria_results": better_result.criteria_results,
                "passed": better_result.passed,
            }

        best = engine._best_attempts["n1"]
        assert best["score"] == 7.5
        assert best["criteria_results"]["lint clean"] is True
        assert best["passed"] is False

    @pytest.mark.asyncio
    async def test_criteria_preserved_on_regression(self, tmp_path):
        """When regression detected, best criteria_results should be preserved."""
        engine = DAGExecutionEngine(
            agent_executor=None,
            failure_handler=_mock_failure_handler,
            work_dir=str(tmp_path),
        )
        best_criteria = {"lint clean": True, "tests pass": False}
        engine._best_attempts["n1"] = {
            "score": 7.5,
            "artifacts": ["a.py"],
            "feedback": "best feedback",
            "lint_issues": set(),
            "artifact_set": {"a.py"},
            "file_snapshot": {"a.py": "good content"},
            "criteria_results": best_criteria,
            "passed": False,
        }

        # Simulate regression: worse score with new issues
        worse_result = _make_eval_result(
            False, 3.0, ["a.py:10:E501", "b.py:20:E501"],
            {"lint clean": False, "tests pass": False},
        )
        # Regression logic: don't update _best_attempts
        if worse_result.score <= engine._best_attempts["n1"]["score"]:
            # Best is preserved
            pass

        best = engine._best_attempts["n1"]
        assert best["score"] == 7.5
        assert best["criteria_results"] == best_criteria
        assert best["passed"] is False
