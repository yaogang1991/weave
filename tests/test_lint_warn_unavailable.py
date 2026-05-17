"""
Tests for #200: lint check warns (not fails) when no linter is available.

When flake8 and ruff are both missing, lint should be treated as an
uncheckable criterion (WARN) rather than a hard FAIL.
"""
import pytest
from unittest.mock import MagicMock, patch

from core.models import CriterionType, SuccessCriterion
from evaluator.engine import EvaluatorEngine


@pytest.fixture
def tmp_store(tmp_path):
    from session.store import SessionStore
    return SessionStore(base_path=str(tmp_path / "events"))


@pytest.fixture
def evaluator(tmp_store):
    return EvaluatorEngine(tmp_store, auto_format_before_eval=True)


class TestLintWarnWhenUnavailable:
    """When no linter is installed, lint becomes WARN instead of FAIL."""

    @patch("evaluator.runner.subprocess.run")
    def test_no_linter_returns_warn(self, mock_run, evaluator, tmp_path):
        """Both flake8 and ruff missing → WARN, not FAIL."""
        (tmp_path / "code.py").write_text("x = 1\n", encoding="utf-8")
        # autoflake → not found, autopep8 → not found, flake8 → not found, ruff → not found
        mock_run.side_effect = [
            FileNotFoundError("autoflake not found"),
            FileNotFoundError("autopep8 not found"),
            FileNotFoundError("flake8 not found"),
            FileNotFoundError("ruff not found"),
        ]
        passed, msg, auto = evaluator._check_criterion(
            SuccessCriterion(type=CriterionType.LINT, description="lint clean"),
            str(tmp_path),
            output_artifacts=["code.py"],
        )
        assert passed  # Should pass (WARN, not FAIL)
        assert not auto  # Should be uncheckable (WARN)
        assert "lint skipped" in msg.lower()
        assert "No linter available" in msg

    @patch("evaluator.runner.subprocess.run")
    def test_lint_with_real_issues_still_fails(self, mock_run, evaluator, tmp_path):
        """When linter runs and finds issues, it still fails."""
        (tmp_path / "code.py").write_text("x = 1\n", encoding="utf-8")
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=""),  # autoflake ok
            MagicMock(returncode=0, stdout=""),  # autopep8 ok
            MagicMock(returncode=1, stdout="code.py:1: E501 line too long"),  # flake8 issues
        ]
        passed, msg, auto = evaluator._check_criterion(
            SuccessCriterion(type=CriterionType.LINT, description="lint clean"),
            str(tmp_path),
            output_artifacts=["code.py"],
        )
        assert not passed
        assert auto

    @patch("evaluator.runner.subprocess.run")
    def test_lint_clean_still_passes(self, mock_run, evaluator, tmp_path):
        """When linter runs and finds nothing, it passes."""
        (tmp_path / "code.py").write_text("x = 1\n", encoding="utf-8")
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout=""),  # autoflake ok
            MagicMock(returncode=0, stdout=""),  # autopep8 ok
            MagicMock(returncode=0, stdout=""),  # flake8 clean
        ]
        passed, msg, auto = evaluator._check_criterion(
            SuccessCriterion(type=CriterionType.LINT, description="lint clean"),
            str(tmp_path),
            output_artifacts=["code.py"],
        )
        assert passed
        assert auto

    @patch("evaluator.runner.subprocess.run")
    def test_no_linter_in_evaluate_stage(self, mock_run, evaluator, tmp_path):
        """Full evaluate_stage: no linter → WARN, overall still passes."""
        (tmp_path / "hello.py").write_text("x = 1\n", encoding="utf-8")
        mock_run.side_effect = [
            FileNotFoundError("autoflake"),
            FileNotFoundError("autopep8"),
            FileNotFoundError("flake8"),
            FileNotFoundError("ruff"),
            MagicMock(returncode=0, stdout="", stderr=""),  # import smoke test
        ]
        result = evaluator.evaluate_stage(
            "s1", "impl",
            [
                SuccessCriterion(
                    type=CriterionType.FILE_EXISTS,
                    path="hello.py", description="file"
                ),
                SuccessCriterion(type=CriterionType.LINT, description="lint clean"),
            ],
            str(tmp_path),
            output_artifacts=["hello.py"],
        )
        # Overall should pass: FILE_EXISTS passes, LINT is WARN (uncheckable)
        assert result.passed
        assert "WARN" in result.feedback
        assert "lint skipped" in result.feedback.lower()

    @patch("evaluator.runner.subprocess.run")
    def test_no_files_to_lint_passes(self, mock_run, evaluator, tmp_path):
        """No output_artifacts → lint passes by default."""
        passed, msg, auto = evaluator._check_criterion(
            SuccessCriterion(type=CriterionType.LINT, description="lint clean"),
            str(tmp_path),
        )
        assert passed
        assert auto
        mock_run.assert_not_called()
