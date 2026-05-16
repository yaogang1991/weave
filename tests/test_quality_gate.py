"""
Tests for #177 PR4: QualityGate extraction from DAGExecutionEngine.

Verifies that evaluation logic, feedback construction, and regression
handling work correctly as an independent service.
"""
import pytest
from unittest.mock import MagicMock
from datetime import datetime, timezone

from core.quality_gate import QualityGate, EvaluationOutcome
from core.models import (
    EvalStatus,
    EvaluationResult,
    NodeStatus,
    SuccessCriterion,
    CriterionType,
)


# ---------------------------------------------------------------------------
# Static helpers (originally on DAGExecutionEngine)
# ---------------------------------------------------------------------------

class TestEvalStatusMapping:
    def test_clean_pass_maps_to_success(self):
        assert QualityGate.eval_status_to_node_status(EvalStatus.CLEAN_PASS) == NodeStatus.SUCCESS

    def test_partial_pass_maps_correctly(self):
        assert QualityGate.eval_status_to_node_status(EvalStatus.PARTIAL_PASS) == NodeStatus.PARTIAL_PASS

    def test_warned_maps_correctly(self):
        assert QualityGate.eval_status_to_node_status(EvalStatus.WARNED) == NodeStatus.WARNED

    def test_failed_maps_correctly(self):
        assert QualityGate.eval_status_to_node_status(EvalStatus.FAILED) == NodeStatus.FAILED

    def test_unknown_defaults_to_success(self):
        result = QualityGate.eval_status_to_node_status("unknown_value")
        assert result == NodeStatus.SUCCESS


class TestIsTerminalSuccess:
    def test_success_is_terminal(self):
        assert QualityGate.is_terminal_success(NodeStatus.SUCCESS)

    def test_partial_pass_is_terminal(self):
        assert QualityGate.is_terminal_success(NodeStatus.PARTIAL_PASS)

    def test_warned_is_terminal(self):
        assert QualityGate.is_terminal_success(NodeStatus.WARNED)

    def test_failed_is_not_terminal(self):
        assert not QualityGate.is_terminal_success(NodeStatus.FAILED)

    def test_pending_is_not_terminal(self):
        assert not QualityGate.is_terminal_success(NodeStatus.PENDING)

    def test_skipped_is_not_terminal(self):
        assert not QualityGate.is_terminal_success(NodeStatus.SKIPPED)


class TestIsTestFileExistsCriterion:
    def test_structured_criterion(self):
        criterion = SuccessCriterion(
            type=CriterionType.TEST_FILE_EXISTS,
            description="test files exist",
        )
        assert QualityGate.is_test_file_exists_criterion(criterion)

    def test_other_criterion_type(self):
        criterion = SuccessCriterion(
            type=CriterionType.TESTS_PASS,
            description="tests pass",
        )
        assert not QualityGate.is_test_file_exists_criterion(criterion)

    def test_string_criterion(self):
        assert QualityGate.is_test_file_exists_criterion("test_file_exists")
        assert QualityGate.is_test_file_exists_criterion("Test file exists")

    def test_non_matching_string(self):
        assert not QualityGate.is_test_file_exists_criterion("tests_pass")

    def test_non_string_non_object(self):
        assert not QualityGate.is_test_file_exists_criterion(42)


# ---------------------------------------------------------------------------
# Test file requirement check
# ---------------------------------------------------------------------------

class TestCheckTestFileRequirement:
    def test_no_criteria_returns_none(self):
        gate = QualityGate()
        node = MagicMock(success_criteria=[])
        result = gate.check_test_file_requirement(node, "node_1")
        assert result is None

    def test_no_test_criterion_returns_none(self):
        gate = QualityGate()
        criterion = SuccessCriterion(
            type=CriterionType.TESTS_PASS,
            description="tests pass",
        )
        node = MagicMock(success_criteria=[criterion], output_artifacts=["main.py"])
        result = gate.check_test_file_requirement(node, "node_1")
        assert result is None

    def test_test_files_present_returns_none(self):
        gate = QualityGate()
        criterion = SuccessCriterion(
            type=CriterionType.TEST_FILE_EXISTS,
            description="test files exist",
        )
        node = MagicMock(
            success_criteria=[criterion],
            output_artifacts=["main.py", "tests/test_main.py"],
        )
        result = gate.check_test_file_requirement(node, "node_1")
        assert result is None

    def test_missing_test_files_returns_failure(self):
        gate = QualityGate()
        criterion = SuccessCriterion(
            type=CriterionType.TEST_FILE_EXISTS,
            description="test files exist",
        )
        node = MagicMock(
            success_criteria=[criterion],
            output_artifacts=["main.py"],
        )
        result = gate.check_test_file_requirement(node, "node_1")

        assert result is not None
        assert isinstance(result, EvaluationOutcome)
        assert not result.passed
        assert result.node_status == NodeStatus.FAILED
        assert "test files" in result.eval_feedback.lower() or "TEST_FILE" in result.eval_feedback
        assert result.event_type == "failed"
        assert result.event_details["reason"] == "no_test_files"

    def test_empty_output_artifacts_fails(self):
        gate = QualityGate()
        criterion = SuccessCriterion(
            type=CriterionType.TEST_FILE_EXISTS,
            description="test files exist",
        )
        node = MagicMock(
            success_criteria=[criterion],
            output_artifacts=[],
        )
        result = gate.check_test_file_requirement(node, "node_1")
        assert result is not None
        assert not result.passed

    def test_none_output_artifacts_fails(self):
        gate = QualityGate()
        criterion = SuccessCriterion(
            type=CriterionType.TEST_FILE_EXISTS,
            description="test files exist",
        )
        node = MagicMock(
            success_criteria=[criterion],
            output_artifacts=None,
        )
        result = gate.check_test_file_requirement(node, "node_1")
        assert result is not None
        assert not result.passed


# ---------------------------------------------------------------------------
# Evaluation with passing result
# ---------------------------------------------------------------------------

class TestEvaluatePassing:
    def test_passing_eval_returns_success(self):
        gate = QualityGate()
        eval_result = EvaluationResult(
            passed=True,
            score=100.0,
            feedback="All good",
            eval_status=EvalStatus.CLEAN_PASS,
        )
        node = MagicMock(output_artifacts=["main.py"])
        outcome = gate.evaluate(eval_result, node, "node_1", "/tmp/work")

        assert outcome.passed
        assert outcome.node_status == NodeStatus.SUCCESS
        assert outcome.auto_eval_result is not None
        assert outcome.auto_eval_result["passed"] is True

    def test_warned_eval_returns_warned_status(self):
        gate = QualityGate()
        eval_result = EvaluationResult(
            passed=True,
            score=80.0,
            feedback="Passed with warnings",
            eval_status=EvalStatus.WARNED,
        )
        node = MagicMock(output_artifacts=["main.py"])
        outcome = gate.evaluate(eval_result, node, "node_1", "/tmp/work")

        assert outcome.passed
        assert outcome.node_status == NodeStatus.WARNED


# ---------------------------------------------------------------------------
# Evaluation with failing result (no retry policy)
# ---------------------------------------------------------------------------

class TestEvaluateFailingNoPolicy:
    def test_failing_without_retry_policy(self):
        gate = QualityGate(retry_policy=None)
        eval_result = EvaluationResult(
            passed=False,
            score=40.0,
            feedback="Tests failed",
            eval_status=EvalStatus.FAILED,
        )
        node = MagicMock(output_artifacts=["main.py"])
        outcome = gate.evaluate(eval_result, node, "node_1", "/tmp/work")

        assert not outcome.passed
        assert outcome.node_status == NodeStatus.FAILED
        assert outcome.retry_count_increment == 1
        assert "Tests failed" in outcome.error


# ---------------------------------------------------------------------------
# Evaluation with retry policy
# ---------------------------------------------------------------------------

class TestEvaluateWithRetryPolicy:
    def test_failing_with_retry_policy_records_attempt(self):
        mock_policy = MagicMock()
        mock_policy.record_attempt.return_value = (False, None)
        mock_policy.get_best.return_value = None

        gate = QualityGate(retry_policy=mock_policy)
        eval_result = EvaluationResult(
            passed=False,
            score=50.0,
            feedback="Missing tests",
            eval_status=EvalStatus.FAILED,
            metadata={},
        )
        node = MagicMock(output_artifacts=["main.py"])
        outcome = gate.evaluate(eval_result, node, "node_1", "/tmp/work")

        assert not outcome.passed
        assert outcome.retry_count_increment == 1
        mock_policy.record_attempt.assert_called_once()

    def test_regression_triggers_restore(self):
        mock_policy = MagicMock()
        mock_policy.record_attempt.return_value = (
            True,
            {
                "score": 80.0,
                "feedback": "Good",
                "file_snapshot": {"main.py": "content"},
                "artifacts": ["main.py"],
                "artifact_set": {"main.py"},
                "lint_issues": set(),
            },
        )
        mock_policy.get_best.return_value = {
            "score": 80.0,
            "feedback": "Good",
            "file_snapshot": {"main.py": "content"},
            "artifacts": ["main.py"],
            "artifact_set": {"main.py"},
            "lint_issues": set(),
        }

        gate = QualityGate(retry_policy=mock_policy)
        eval_result = EvaluationResult(
            passed=False,
            score=30.0,
            feedback="Regression",
            eval_status=EvalStatus.FAILED,
            metadata={},
        )
        node = MagicMock(output_artifacts=["main.py"])
        outcome = gate.evaluate(eval_result, node, "node_1", "/tmp/work")

        assert outcome.should_restore_best
        assert outcome.restored_artifacts == ["main.py"]
        mock_policy.restore_file_snapshot.assert_called_once()

    def test_regression_hint_in_feedback(self):
        mock_policy = MagicMock()
        mock_policy.record_attempt.return_value = (
            True,
            {
                "score": 80.0,
                "feedback": "Good",
                "file_snapshot": {"main.py": "content"},
                "artifacts": ["main.py"],
                "lint_issues": set(),
            },
        )
        mock_policy.get_best.return_value = {
            "score": 80.0,
            "feedback": "Good",
            "file_snapshot": {"main.py": "content"},
            "artifacts": ["main.py"],
            "lint_issues": set(),
        }

        gate = QualityGate(retry_policy=mock_policy)
        eval_result = EvaluationResult(
            passed=False,
            score=30.0,
            feedback="Regression",
            eval_status=EvalStatus.FAILED,
            metadata={},
        )
        node = MagicMock(output_artifacts=["main.py"])
        outcome = gate.evaluate(eval_result, node, "node_1", "/tmp/work")

        assert "previous attempt scored higher" in outcome.eval_feedback

    def test_lint_guidance_in_feedback(self):
        mock_policy = MagicMock()
        mock_policy.record_attempt.return_value = (False, None)
        # Best score must be <= current score to avoid regression hint
        mock_policy.get_best.return_value = {
            "score": 40.0,
            "feedback": "Issues",
            "lint_issues": {"E501 line too long"},
        }

        gate = QualityGate(retry_policy=mock_policy)
        eval_result = EvaluationResult(
            passed=False,
            score=40.0,
            feedback="Lint issues",
            eval_status=EvalStatus.FAILED,
            metadata={
                "lint_new_issues": ["E501 line too long", "F401 unused import"],
                "lint_all_issues": ["E501 line too long", "F401 unused import"],
            },
        )
        node = MagicMock(output_artifacts=["main.py"])
        outcome = gate.evaluate(eval_result, node, "node_1", "/tmp/work")

        # F401 is new (not in prev_issues), so lint guidance should appear
        assert "LINT_FIX_GUIDANCE" in outcome.eval_feedback
        assert "F401 unused import" in outcome.eval_feedback

    def test_no_lint_guidance_when_regression_hint_present(self):
        mock_policy = MagicMock()
        mock_policy.record_attempt.return_value = (
            True,
            {
                "score": 80.0,
                "feedback": "Good",
                "file_snapshot": {"main.py": "content"},
                "artifacts": ["main.py"],
                "lint_issues": set(),
            },
        )
        mock_policy.get_best.return_value = {
            "score": 80.0,
            "feedback": "Good",
            "file_snapshot": {"main.py": "content"},
            "artifacts": ["main.py"],
            "lint_issues": set(),
        }

        gate = QualityGate(retry_policy=mock_policy)
        eval_result = EvaluationResult(
            passed=False,
            score=30.0,
            feedback="Regression",
            eval_status=EvalStatus.FAILED,
            metadata={
                "lint_new_issues": ["F401 unused import"],
            },
        )
        node = MagicMock(output_artifacts=["main.py"])
        outcome = gate.evaluate(eval_result, node, "node_1", "/tmp/work")

        # Regression hint takes priority, no lint guidance
        assert "LINT_FIX_GUIDANCE" not in outcome.eval_feedback
        assert "previous attempt scored higher" in outcome.eval_feedback


# ---------------------------------------------------------------------------
# EvaluationOutcome dataclass
# ---------------------------------------------------------------------------

class TestEvaluationOutcome:
    def test_default_values(self):
        outcome = EvaluationOutcome(
            passed=True,
            node_status=NodeStatus.SUCCESS,
        )
        assert outcome.eval_feedback == ""
        assert outcome.error == ""
        assert outcome.auto_eval_result is None
        assert outcome.retry_count_increment == 0
        assert outcome.should_restore_best is False
        assert outcome.restored_artifacts is None
        assert outcome.event_type == "completed"
        assert outcome.event_details == {}

    def test_full_outcome(self):
        outcome = EvaluationOutcome(
            passed=False,
            node_status=NodeStatus.FAILED,
            eval_feedback="Fix errors",
            error="Score too low",
            auto_eval_result={"passed": False, "score": 30},
            retry_count_increment=1,
            should_restore_best=True,
            restored_artifacts=["main.py"],
            event_type="retrying",
            event_details={"score": 30},
        )
        assert not outcome.passed
        assert outcome.retry_count_increment == 1
        assert outcome.should_restore_best
