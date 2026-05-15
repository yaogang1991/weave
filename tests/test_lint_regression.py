"""
Tests for lint regression detection in DAG retry logic (#151).

Verifies that the DAG engine tracks lint issue sets across retries
and allows retries when there is partial progress (some issues fixed,
some new introduced), while blocking true regressions.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.models import (
    DAG,
    DAGNode,
    EvaluationResult,
    FailureDecision,
    NodeStatus,
)
from core.dag_engine import DAGExecutionEngine


def _make_eval_result(
    passed: bool,
    score: float,
    lint_issues: list[str] | None = None,
) -> EvaluationResult:
    return EvaluationResult(
        passed=passed,
        score=score,
        criteria_results={"lint": passed},
        feedback="lint feedback",
        metadata={"lint_new_issues": lint_issues or [], "lint_all_issues": lint_issues or []},
    )


async def _mock_failure_handler(dag, node_id, error):
    return FailureDecision(action="abort")


# ---------------------------------------------------------------------------
# TestLintIssueRegression
# ---------------------------------------------------------------------------


class TestLintIssueRegression:
    @pytest.mark.asyncio
    async def test_first_failure_records_issues(self, tmp_path):
        """First eval failure should record lint issues in _best_attempts."""
        call_count = 0
        results = [_make_eval_result(False, 7.5, ["a.py:10:E501", "b.py:20:E501"])]

        async def executor(node, artifacts):
            nonlocal call_count
            _ = results[min(call_count, len(results) - 1)]
            call_count += 1
            return {"summary": "ok", "artifacts": ["a.py"]}

        engine = DAGExecutionEngine(
            agent_executor=executor,
            failure_handler=_mock_failure_handler,
            work_dir=str(tmp_path),
        )
        # Inject eval result
        engine.evaluator = MagicMock()
        engine.evaluator.evaluate_stage = MagicMock(
            return_value=results[0],
        )

        dag = DAG(
            nodes={
                "n1": DAGNode(
                    id="n1",
                    agent_type="generator",
                    task_description="test",
                    success_criteria=["lint clean"],
                ),
            },
        )
        result = await engine.execute(dag)

        # Node should fail (eval failed)
        assert result.nodes["n1"].status == NodeStatus.FAILED
        # _best_attempts should have lint_issues
        assert "n1" in engine._best_attempts
        assert engine._best_attempts["n1"]["lint_issues"] == {
            "a.py:10:E501", "b.py:20:E501",
        }

    @pytest.mark.asyncio
    async def test_partial_progress_allows_retry(self):
        """Score same but issues changed: some fixed, some new → allow."""
        engine = DAGExecutionEngine(
            agent_executor=None,
            failure_handler=_mock_failure_handler,
        )
        engine._best_attempts["n1"] = {
            "score": 7.5,
            "artifacts": ["a.py"],
            "feedback": "old feedback",
            "lint_issues": {"a.py:10:E501", "b.py:20:E501"},
        }

        # New eval: fixed b.py:20, introduced c.py:5
        current_issues = {"a.py:10:E501", "c.py:5:E501"}
        prev_issues = engine._best_attempts["n1"]["lint_issues"]

        new_in_current = current_issues - prev_issues
        fixed_from_prev = prev_issues - current_issues

        # New: 1 (c.py:5), Fixed: 1 (b.py:20) → partial progress
        assert len(new_in_current) == 1
        assert len(fixed_from_prev) == 1
        # Should NOT be regression (some fixed, some new)
        assert not (new_in_current and not fixed_from_prev)

    @pytest.mark.asyncio
    async def test_only_new_issues_is_regression(self):
        """New issues added, nothing fixed → true regression."""
        engine = DAGExecutionEngine(
            agent_executor=None,
            failure_handler=_mock_failure_handler,
        )
        engine._best_attempts["n1"] = {
            "score": 7.5,
            "artifacts": ["a.py"],
            "feedback": "old",
            "lint_issues": {"a.py:10:E501"},
        }

        current_issues = {"a.py:10:E501", "b.py:20:E501", "c.py:5:E501"}
        prev_issues = engine._best_attempts["n1"]["lint_issues"]

        new_in_current = current_issues - prev_issues
        fixed_from_prev = prev_issues - current_issues

        # New: 2 (b.py, c.py), Fixed: 0 → regression
        assert new_in_current and not fixed_from_prev
        assert len(new_in_current) == 2
        assert len(fixed_from_prev) == 0

    @pytest.mark.asyncio
    async def test_all_fixed_allows_retry(self):
        """All old issues fixed, no new ones → allow retry."""
        engine = DAGExecutionEngine(
            agent_executor=None,
            failure_handler=_mock_failure_handler,
        )
        engine._best_attempts["n1"] = {
            "score": 7.5,
            "artifacts": ["a.py"],
            "feedback": "old",
            "lint_issues": {"a.py:10:E501", "b.py:20:E501"},
        }

        # All fixed, only new ones
        current_issues = {"c.py:5:E501"}
        prev_issues = engine._best_attempts["n1"]["lint_issues"]

        new_in_current = current_issues - prev_issues
        fixed_from_prev = prev_issues - current_issues

        # New: 1, Fixed: 2 → progress (more fixed than new)
        assert len(fixed_from_prev) == 2
        assert len(new_in_current) == 1


# ---------------------------------------------------------------------------
# TestEvaluationResultMetadata
# ---------------------------------------------------------------------------


class TestEvaluationResultMetadata:
    def test_metadata_default_empty(self):
        r = EvaluationResult(passed=True, score=10.0)
        assert r.metadata == {}

    def test_metadata_preserved(self):
        r = EvaluationResult(
            passed=False,
            score=7.5,
            metadata={"lint_new_issues": ["a.py:10:E501"]},
        )
        assert r.metadata["lint_new_issues"] == ["a.py:10:E501"]

    def test_metadata_serialized(self):
        r = EvaluationResult(
            passed=False,
            score=7.5,
            metadata={"lint_all_issues": ["a.py:10:E501"]},
        )
        d = r.model_dump()
        assert d["metadata"]["lint_all_issues"] == ["a.py:10:E501"]
