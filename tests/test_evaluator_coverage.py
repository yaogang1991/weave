"""Tests for _check_coverage() — covers all parsing and failure paths.

Regression guard for #152: coverage parse failure must return FAIL.
"""

from unittest.mock import MagicMock, patch

import pytest

from core.subprocess_runner import SubprocessResult
from evaluator.engine import EvaluatorEngine


@pytest.fixture
def evaluator(tmp_path):
    store = MagicMock()
    return EvaluatorEngine(session_store=store)


class TestCoverageParsing:
    """Verify TOTAL line parsing across output formats."""

    @patch("evaluator.runner.run_with_progress")
    def test_pass_when_above_target(self, mock_run, evaluator, tmp_path):
        mock_run.return_value = SubprocessResult(
            returncode=0,
            stdout=(
                "Name    Stmts   Miss  Cover\n"
                "------  ------  -----  -----\n"
                "mod.py      10      2    80%\n"
                "TOTAL       10      2    80%"
            ),
            stderr="",
        )
        passed, msg, _ = evaluator._check_coverage(tmp_path, 80)
        assert passed
        assert "80%" in msg

    @patch("evaluator.runner.run_with_progress")
    def test_fail_when_below_target(self, mock_run, evaluator, tmp_path):
        mock_run.return_value = SubprocessResult(
            returncode=0,
            stdout="TOTAL   50  30  60%",
            stderr="",
        )
        passed, msg, _ = evaluator._check_coverage(tmp_path, 80)
        assert not passed
        assert "60.0%" in msg

    @patch("evaluator.runner.run_with_progress")
    def test_wide_format_with_branch_column(self, mock_run, evaluator, tmp_path):
        mock_run.return_value = SubprocessResult(
            returncode=0,
            stdout="TOTAL   120  10  5  91.7%",
            stderr="",
        )
        passed, msg, _ = evaluator._check_coverage(tmp_path, 90)
        assert passed
        assert "91.7%" in msg

    @patch("evaluator.runner.run_with_progress")
    def test_decimal_coverage(self, mock_run, evaluator, tmp_path):
        mock_run.return_value = SubprocessResult(
            returncode=0,
            stdout="TOTAL   100  15  85.5%",
            stderr="",
        )
        passed, msg, _ = evaluator._check_coverage(tmp_path, 85)
        assert passed
        assert "85.5%" in msg


class TestCoverageParseFailure:
    """Regression tests for #152: parse failure must not return PASS."""

    @patch("evaluator.runner.run_with_progress")
    def test_no_total_line_returns_warn(self, mock_run, evaluator, tmp_path):
        """Tests pass but no TOTAL → WARN (unverifiable), not PASS."""
        (tmp_path / "test_module.py").write_text("def test_x(): pass\n")
        mock_run.return_value = SubprocessResult(
            returncode=0,
            stdout="2 passed in 0.01s\n",
            stderr="",
        )
        passed, msg, auto = evaluator._check_coverage(
            tmp_path, 80, output_artifacts=["test_module.py"],
        )
        assert passed  # passed=True but auto=False → WARN in evaluate_stage
        assert not auto  # not auto-verified → will be WARN
        assert "could not be parsed" in msg
        assert "not verified" in msg

    @patch("evaluator.runner.run_with_progress")
    def test_stderr_included_in_feedback(self, mock_run, evaluator, tmp_path):
        """stderr tail should appear in feedback for debugging."""
        (tmp_path / "test_module.py").write_text("def test_x(): pass\n")
        mock_run.return_value = SubprocessResult(
            returncode=0,
            stdout="2 passed\n",
            stderr="WARNING:pytest-cov:Failed to generate report",
        )
        passed, msg, auto = evaluator._check_coverage(
            tmp_path, 80, output_artifacts=["test_module.py"],
        )
        assert passed  # passed but unverifiable → WARN
        assert "pytest-cov" in msg

    @patch("evaluator.runner.run_with_progress")
    def test_tests_fail_and_no_total(self, mock_run, evaluator, tmp_path):
        """Tests fail + no TOTAL → FAIL with combined message."""
        mock_run.return_value = SubprocessResult(
            returncode=1,
            stdout="1 failed, 1 passed\n",
            stderr="",
        )
        passed, msg, _ = evaluator._check_coverage(tmp_path, 80)
        assert not passed
        assert "Tests failed" in msg

    @patch("evaluator.runner.run_with_progress")
    def test_exception_returns_fail(self, mock_run, evaluator, tmp_path):
        """Unexpected exception → FAIL with error message."""
        mock_run.side_effect = RuntimeError("boom")
        passed, msg, _ = evaluator._check_coverage(tmp_path, 80)
        assert not passed
        assert "error" in msg.lower()

    @patch("evaluator.runner.run_with_progress")
    def test_total_line_without_percentage(self, mock_run, evaluator, tmp_path):
        """TOTAL line present but no % value → WARN (unverifiable)."""
        (tmp_path / "test_module.py").write_text("def test_x(): pass\n")
        mock_run.return_value = SubprocessResult(
            returncode=0,
            stdout="TOTAL   100   10",
            stderr="",
        )
        passed, msg, auto = evaluator._check_coverage(
            tmp_path, 80, output_artifacts=["test_module.py"],
        )
        assert passed  # passed but unverifiable → WARN
        assert not auto
        assert "could not be parsed" in msg
