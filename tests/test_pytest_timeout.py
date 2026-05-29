"""Test pytest timeout handling for background thread leaks (#256).

Verifies that evaluator handles subprocess timeout gracefully
and returns actionable feedback instead of hanging indefinitely.

M4.5: Updated to mock run_with_progress returning SubprocessResult.
"""
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.subprocess_runner import SubprocessResult  # noqa: E402
from evaluator.engine import EvaluatorEngine  # noqa: E402
from core.models import CriterionType, SuccessCriterion  # noqa: E402
from session.store import SessionStore  # noqa: E402


@pytest.fixture
def engine(tmp_path):
    store = SessionStore(str(tmp_path / "events"))
    return EvaluatorEngine(store)


@pytest.fixture
def work_dir(tmp_path):
    return tmp_path


class TestPytestTimeout:
    """Evaluator should fail fast on hanging tests with actionable feedback."""

    def test_run_tests_timeout_returns_actionable_error(self, engine, work_dir):
        """_run_tests returns a clear error when subprocess times out."""
        with patch("evaluator.runner.run_with_progress") as mock_run:
            mock_run.return_value = SubprocessResult(
                returncode=-9, stdout="", stderr="", timed_out=True,
            )
            passed, msg = engine._run_tests(work_dir, "test_dummy.py")

        assert passed is False
        assert "timed out" in msg.lower()
        assert "daemon" in msg.lower() or "thread" in msg.lower()

    def test_run_tests_timeout_mentions_teardown(self, engine, work_dir):
        """Timeout message mentions proper teardown for fixing the issue."""
        with patch("evaluator.runner.run_with_progress") as mock_run:
            mock_run.return_value = SubprocessResult(
                returncode=-9, stdout="", stderr="", timed_out=True,
            )
            passed, msg = engine._run_tests(work_dir, "test_dummy.py")

        assert "teardown" in msg.lower()

    def test_coverage_check_timeout_returns_error(self, engine, work_dir):
        """_check_coverage returns a clear error when subprocess times out."""
        (work_dir / "mymod").mkdir(parents=True, exist_ok=True)
        (work_dir / "mymod" / "core.py").write_text("x = 1\n")
        (work_dir / "tests").mkdir(parents=True, exist_ok=True)
        (work_dir / "tests" / "test_core.py").write_text("def test_ok(): pass\n")

        with patch("evaluator.runner.run_with_progress") as mock_run:
            mock_run.return_value = SubprocessResult(
                returncode=-9, stdout="", stderr="", timed_out=True,
            )
            passed, msg, auto = engine._check_coverage(
                work_dir, 80, output_artifacts=["mymod/core.py"],
            )

        assert passed is False
        assert "timed out" in msg.lower()
        assert auto is True

    def test_normal_failure_not_affected(self, engine, work_dir):
        """Normal test failures are not affected by the timeout handling."""
        with patch("evaluator.runner.run_with_progress") as mock_run:
            mock_run.return_value = SubprocessResult(
                returncode=1,
                stdout="FAILED test_dummy.py::test_fail - AssertionError",
                stderr="",
            )
            passed, msg = engine._run_tests(work_dir, "test_dummy.py")

        assert passed is False
        assert "FAILED" in msg
        assert "timed out" not in msg.lower()

    def test_evaluate_stage_handles_timeout_gracefully(self, engine, tmp_path):
        """Full evaluate_stage handles timeout without crashing."""
        test_file = tmp_path / "test_hanging.py"
        test_file.write_text("def test_hang(): import time; time.sleep(999)\n")

        with patch("evaluator.runner.run_with_progress") as mock_run:
            mock_run.return_value = SubprocessResult(
                returncode=-9, stdout="", stderr="", timed_out=True,
            )
            result = engine.evaluate_stage(
                session_id="test-timeout",
                stage_name="impl_core",
                criteria=[SuccessCriterion(
                    type=CriterionType.TESTS_PASS,
                    test_path=str(test_file),
                    description="Tests pass",
                )],
                artifact_path=str(tmp_path),
            )

        assert result.passed is False
        assert "timed out" in result.feedback.lower()
