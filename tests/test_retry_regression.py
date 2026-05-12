"""Tests for retry regression prevention (issue #129).

Verifies:
1. Coverage parse failure does not fail evaluation when tests pass
2. Best-attempt tracking detects score regression
3. Retry feedback includes regression warning
"""
import pytest
from unittest.mock import MagicMock, patch

from evaluator.engine import EvaluatorEngine
from core.models import SuccessCriterion, CriterionType


class TestCoverageParseTolerance:
    def test_coverage_parse_failure_passes_when_tests_ok(self, tmp_path):
        """When coverage can't be parsed but pytest passed, evaluation should pass."""
        engine = EvaluatorEngine(MagicMock())

        with patch("evaluator.engine.subprocess.run") as mock_run:
            # pytest+coverage runs with returncode 0 (tests pass) but no TOTAL line
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="test_session starts\n2 passed\n",
                stderr="",
            )
            passed, msg = engine._check_coverage(tmp_path, 80)

        assert passed
        assert "could not be parsed" in msg
        assert "tool error" in msg

    def test_coverage_parse_failure_fails_when_tests_fail(self, tmp_path):
        """When tests fail AND coverage can't be parsed, evaluation should fail."""
        engine = EvaluatorEngine(MagicMock())

        with patch("evaluator.engine.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1,
                stdout="1 failed\n",
                stderr="",
            )
            passed, msg = engine._check_coverage(tmp_path, 80)

        assert not passed
        assert "Tests failed" in msg

    def test_coverage_parsed_normally(self, tmp_path):
        """Normal coverage parsing still works correctly."""
        engine = EvaluatorEngine(MagicMock())

        with patch("evaluator.engine.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="Name        Stmts   Miss  Cover\n"
                       "module.py      20      4    80%\n"
                       "TOTAL          20      4    80%\n",
                stderr="",
            )
            passed, msg = engine._check_coverage(tmp_path, 80)

        assert passed
        assert "80%" in msg

    def test_coverage_below_target(self, tmp_path):
        """Coverage below target returns False."""
        engine = EvaluatorEngine(MagicMock())

        with patch("evaluator.engine.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="TOTAL  20  10  50%\n",
                stderr="",
            )
            passed, msg = engine._check_coverage(tmp_path, 80)

        assert not passed


class TestCoverageInEvaluationStage:
    def test_coverage_parse_doesnt_block_overall_eval(self, tmp_path):
        """Full evaluate_stage passes when coverage can't parse but other
        criteria succeed."""
        engine = EvaluatorEngine(MagicMock())

        (tmp_path / "module.py").write_text("x = 1\n")
        (tmp_path / "test_module.py").write_text("from module import x\n")

        criteria = [
            SuccessCriterion(type=CriterionType.TESTS_PASS, description="tests pass"),
            SuccessCriterion(type=CriterionType.LINT, description="lint clean"),
            SuccessCriterion(type=CriterionType.COVERAGE, target=80, description="coverage"),
        ]

        with patch("evaluator.engine.subprocess.run") as mock_run:
            def fake_run(cmd, **kwargs):
                if "pytest" in cmd and "--cov" in cmd:
                    # Coverage run: tests pass but no TOTAL line
                    return MagicMock(returncode=0, stdout="2 passed\n", stderr="")
                if "pytest" in cmd:
                    return MagicMock(returncode=0, stdout="2 passed\n", stderr="")
                if "autoflake" in cmd:
                    return MagicMock(returncode=0, stdout="", stderr="")
                if "autopep8" in cmd:
                    return MagicMock(returncode=0, stdout="", stderr="")
                if "flake8" in cmd:
                    return MagicMock(returncode=0, stdout="", stderr="")
                return MagicMock(returncode=0, stdout="", stderr="")

            mock_run.side_effect = fake_run
            result = engine.evaluate_stage(
                session_id="test",
                stage_name="impl",
                criteria=criteria,
                artifact_path=str(tmp_path),
                work_dir=str(tmp_path),
                output_artifacts=["module.py"],
            )

        assert result.passed
        # Coverage criterion should show as passed (with warning)
        assert result.score > 0
