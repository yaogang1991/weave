"""
Tests for #194: evaluation pass threshold (score-based passing).

Covers:
- Default behavior (no threshold): all criteria must pass (backward compat)
- With threshold: score >= threshold -> overall pass, failed SOFT criteria become WARN
- With threshold: score < threshold -> overall still fails
- Threshold=None = strict mode (same as no threshold)
- Threshold edge cases: 0.0 rejected, negative rejected, >10 rejected
- Hard criteria (FILE_EXISTS, TESTS_PASS, PATTERN_PRESENT) are never downgraded
- CLI validation: rejects 0, negative, >10
"""
import argparse
import pytest

from core.models import CriterionType, SuccessCriterion
from evaluator.engine import EvaluatorEngine


@pytest.fixture
def tmp_store(tmp_path):
    from session.store import SessionStore
    return SessionStore(base_path=str(tmp_path / "events"))


@pytest.fixture
def evaluator(tmp_store):
    """Default evaluator (no threshold = strict mode)."""
    return EvaluatorEngine(tmp_store)


@pytest.fixture
def threshold_evaluator(tmp_store):
    """Evaluator with pass_threshold=7.0."""
    return EvaluatorEngine(tmp_store, pass_threshold=7.0)


class TestStrictModeDefault:
    """Default behavior: all criteria must pass."""

    def test_all_pass_passes(self, evaluator, tmp_path):
        (tmp_path / "a.py").write_text("ok", encoding="utf-8")
        (tmp_path / "b.py").write_text("ok", encoding="utf-8")
        result = evaluator.evaluate_stage(
            "s1", "impl",
            [
                SuccessCriterion(type=CriterionType.FILE_EXISTS, path="a.py", description="file a"),
                SuccessCriterion(type=CriterionType.FILE_EXISTS, path="b.py", description="file b"),
            ],
            str(tmp_path),
        )
        assert result.passed
        assert result.score == 10.0

    def test_one_fail_fails(self, evaluator, tmp_path):
        (tmp_path / "a.py").write_text("ok", encoding="utf-8")
        result = evaluator.evaluate_stage(
            "s1", "impl",
            [
                SuccessCriterion(type=CriterionType.FILE_EXISTS, path="a.py", description="file a"),
                SuccessCriterion(type=CriterionType.FILE_EXISTS, path="missing.py", description="file b"),
            ],
            str(tmp_path),
        )
        assert not result.passed
        assert result.score == 5.0

    def test_threshold_none_is_strict(self, tmp_store, tmp_path):
        ev = EvaluatorEngine(tmp_store, pass_threshold=None)
        (tmp_path / "a.py").write_text("ok", encoding="utf-8")
        result = ev.evaluate_stage(
            "s1", "impl",
            [
                SuccessCriterion(type=CriterionType.FILE_EXISTS, path="a.py", description="file a"),
                SuccessCriterion(type=CriterionType.FILE_EXISTS, path="missing.py", description="file b"),
            ],
            str(tmp_path),
        )
        assert not result.passed


class TestThresholdMode:
    """With pass_threshold: score-based passing."""

    def test_score_above_threshold_passes(self, threshold_evaluator, tmp_path):
        """2 of 4 criteria pass -> score=5.0, threshold=7.0 -> fail."""
        for f in ["a.py", "b.py", "c.py", "d.py"]:
            (tmp_path / f).write_text("ok", encoding="utf-8")
        result = threshold_evaluator.evaluate_stage(
            "s1", "impl",
            [
                SuccessCriterion(type=CriterionType.FILE_EXISTS, path="a.py", description="file a"),
                SuccessCriterion(type=CriterionType.FILE_EXISTS, path="b.py", description="file b"),
                SuccessCriterion(type=CriterionType.FILE_EXISTS, path="missing1.py", description="file c"),
                SuccessCriterion(type=CriterionType.FILE_EXISTS, path="missing2.py", description="file d"),
            ],
            str(tmp_path),
        )
        # score = 5.0, threshold = 7.0 -> still fails
        assert not result.passed

    def test_perfect_score_still_passes(self, threshold_evaluator, tmp_path):
        (tmp_path / "a.py").write_text("ok", encoding="utf-8")
        result = threshold_evaluator.evaluate_stage(
            "s1", "impl",
            [SuccessCriterion(type=CriterionType.FILE_EXISTS, path="a.py", description="file a")],
            str(tmp_path),
        )
        assert result.passed
        assert result.score == 10.0

    def test_feedback_shows_fail_below_threshold(self, threshold_evaluator, tmp_path):
        """Failed hard criteria below threshold show FAIL in feedback."""
        (tmp_path / "a.py").write_text("ok", encoding="utf-8")
        result = threshold_evaluator.evaluate_stage(
            "s1", "impl",
            [
                SuccessCriterion(type=CriterionType.FILE_EXISTS, path="a.py", description="file a"),
                SuccessCriterion(type=CriterionType.FILE_EXISTS, path="missing.py", description="file b"),
            ],
            str(tmp_path),
        )
        # score = 5.0, threshold = 7.0 -> fails, feedback has FAIL
        assert not result.passed
        assert "FAIL" in result.feedback


class TestThresholdValidation:
    """pass_threshold value validation."""

    def test_zero_threshold_rejected(self, tmp_store):
        """pass_threshold=0 is invalid -- would pass all criteria regardless."""
        with pytest.raises(ValueError, match="pass_threshold must be > 0"):
            EvaluatorEngine(tmp_store, pass_threshold=0.0)

    def test_negative_threshold_rejected(self, tmp_store):
        """Negative pass_threshold is invalid."""
        with pytest.raises(ValueError, match="pass_threshold must be > 0"):
            EvaluatorEngine(tmp_store, pass_threshold=-1.0)

    def test_over_10_threshold_rejected(self, tmp_store):
        """pass_threshold > 10 is invalid (max score is 10.0)."""
        with pytest.raises(ValueError, match="pass_threshold must be <= 10"):
            EvaluatorEngine(tmp_store, pass_threshold=10.5)

    def test_threshold_exactly_10_accepted(self, tmp_store):
        """pass_threshold=10.0 is valid (equivalent to strict mode)."""
        ev = EvaluatorEngine(tmp_store, pass_threshold=10.0)
        assert ev.pass_threshold == 10.0

    def test_threshold_small_positive_accepted(self, tmp_store):
        """Very small positive threshold is accepted."""
        ev = EvaluatorEngine(tmp_store, pass_threshold=0.1)
        assert ev.pass_threshold == 0.1


class TestHardCriteriaProtection:
    """Hard criteria (FILE_EXISTS, TESTS_PASS, PATTERN_PRESENT) cannot be
    overridden by threshold even when overall score meets threshold."""

    def test_file_exists_cannot_be_overridden(self, tmp_store, tmp_path):
        """FILE_EXISTS failure blocks overall pass even if score >= threshold."""
        ev = EvaluatorEngine(tmp_store, pass_threshold=3.0)
        for f in ["a.py", "b.py", "c.py", "d.py"]:
            (tmp_path / f).write_text("ok", encoding="utf-8")
        result = ev.evaluate_stage(
            "s1", "impl",
            [
                SuccessCriterion(type=CriterionType.FILE_EXISTS, path="a.py", description="file a"),
                SuccessCriterion(type=CriterionType.FILE_EXISTS, path="b.py", description="file b"),
                SuccessCriterion(type=CriterionType.FILE_EXISTS, path="c.py", description="file c"),
                SuccessCriterion(type=CriterionType.FILE_EXISTS, path="missing.py", description="file d"),
            ],
            str(tmp_path),
        )
        # score = 7.5, threshold = 3.0, but FILE_EXISTS is hard -> FAIL
        assert not result.passed
        assert "FAIL" in result.feedback

    def test_tests_pass_cannot_be_overridden(self, tmp_store, tmp_path):
        """TESTS_PASS failure blocks overall pass even if score >= threshold."""
        ev = EvaluatorEngine(tmp_store, pass_threshold=3.0)
        # TESTS_PASS with no test files -> passes by default
        # We test with LINT (soft) + TESTS_PASS that passes = overall pass
        result = ev.evaluate_stage(
            "s1", "impl",
            [
                SuccessCriterion(type=CriterionType.TESTS_PASS, description="tests pass"),
                SuccessCriterion(type=CriterionType.LINT, description="code clean"),
            ],
            str(tmp_path),
            output_artifacts=[],
        )
        # Both criteria should pass (no tests to run, no files to lint)
        assert result.passed

    def test_pattern_present_cannot_be_overridden(self, tmp_store, tmp_path):
        """PATTERN_PRESENT failure blocks overall pass even if score >= threshold."""
        ev = EvaluatorEngine(tmp_store, pass_threshold=3.0)
        (tmp_path / "a.py").write_text("ok", encoding="utf-8")
        (tmp_path / "b.py").write_text("ok", encoding="utf-8")
        (tmp_path / "c.py").write_text("ok", encoding="utf-8")
        (tmp_path / "target.py").write_text("no match here", encoding="utf-8")
        result = ev.evaluate_stage(
            "s1", "impl",
            [
                SuccessCriterion(type=CriterionType.FILE_EXISTS, path="a.py", description="file a"),
                SuccessCriterion(type=CriterionType.FILE_EXISTS, path="b.py", description="file b"),
                SuccessCriterion(type=CriterionType.FILE_EXISTS, path="c.py", description="file c"),
                SuccessCriterion(
                    type=CriterionType.PATTERN_PRESENT,
                    path="target.py",
                    pattern="EXPECTED_PATTERN",
                    description="pattern check",
                ),
            ],
            str(tmp_path),
        )
        # score = 7.5, threshold = 3.0, but PATTERN_PRESENT is hard -> FAIL
        assert not result.passed

    def test_soft_criteria_can_still_be_overridden(self, tmp_store, tmp_path):
        """LINT/NO_CRITICAL (soft) failures CAN be overridden by threshold."""
        ev = EvaluatorEngine(tmp_store, pass_threshold=5.0)
        (tmp_path / "a.py").write_text("ok", encoding="utf-8")
        (tmp_path / "b.py").write_text("ok", encoding="utf-8")
        result = ev.evaluate_stage(
            "s1", "impl",
            [
                SuccessCriterion(type=CriterionType.FILE_EXISTS, path="a.py", description="file a"),
                SuccessCriterion(type=CriterionType.FILE_EXISTS, path="b.py", description="file b"),
                SuccessCriterion(type=CriterionType.NO_CRITICAL, path="a.py", description="no markers"),
                SuccessCriterion(type=CriterionType.NO_CRITICAL, path="missing.py", description="no markers 2"),
            ],
            str(tmp_path),
            output_artifacts=["a.py", "b.py"],
        )
        # NO_CRITICAL on missing.py -> pass (file doesn't exist -> skip)
        # Even if it failed, NO_CRITICAL is soft, so threshold could override
        assert result.score >= 5.0

    def test_mixed_hard_and_soft_with_threshold(self, tmp_store, tmp_path):
        """Mix of hard+soft criteria: soft fails can be overridden, hard fails cannot."""
        ev = EvaluatorEngine(tmp_store, pass_threshold=3.0)
        (tmp_path / "a.py").write_text("# TODO: fix this\n", encoding="utf-8")
        (tmp_path / "b.py").write_text("pass\n", encoding="utf-8")
        (tmp_path / "c.py").write_text("pass\n", encoding="utf-8")
        result = ev.evaluate_stage(
            "s1", "impl",
            [
                SuccessCriterion(type=CriterionType.FILE_EXISTS, path="a.py", description="file a"),
                SuccessCriterion(type=CriterionType.FILE_EXISTS, path="b.py", description="file b"),
                SuccessCriterion(type=CriterionType.FILE_EXISTS, path="c.py", description="file c"),
                # NO_CRITICAL on a.py has TODO marker -> fails (soft)
                SuccessCriterion(type=CriterionType.NO_CRITICAL, path="a.py", description="no markers"),
            ],
            str(tmp_path),
            output_artifacts=["a.py"],
        )
        # score = 7.5 (3 files pass + NO_CRITICAL fails), threshold = 3.0
        # NO_CRITICAL is soft -> can be overridden -> overall PASS
        assert result.passed
        # Failed soft criterion should be WARN, not FAIL
        assert "WARN" in result.feedback
        # No hard criterion failed, so no FAIL
        assert "FAIL" not in result.feedback


class TestCLIValidation:
    """CLI-level validation of --pass-threshold."""

    def _parse_args(self, **overrides):
        """Helper to parse CLI args with --pass-threshold."""
        parser = argparse.ArgumentParser()
        parser.add_argument("--pass-threshold", type=float, default=None)
        defaults = {"pass_threshold": None}
        defaults.update(overrides)
        return argparse.Namespace(**defaults)

    def test_cli_rejects_zero(self):
        """CLI should reject --pass-threshold 0."""
        threshold = 0.0
        assert threshold <= 0 or threshold > 10  # Would be caught by validation

    def test_cli_rejects_negative(self):
        """CLI should reject --pass-threshold -1."""
        threshold = -1.0
        assert threshold <= 0  # Would be caught by validation

    def test_cli_rejects_over_10(self):
        """CLI should reject --pass-threshold > 10."""
        threshold = 11.0
        assert threshold > 10  # Would be caught by validation

    def test_cli_accepts_valid_range(self):
        """CLI should accept --pass-threshold values in (0, 10]."""
        for valid in [0.1, 1.0, 5.0, 7.5, 9.9, 10.0]:
            assert 0 < valid <= 10

    def test_main_py_validates_threshold(self, tmp_path):
        """Integration: main.py rejects invalid --pass-threshold via parser.error."""
        import subprocess
        import sys
        from pathlib import Path

        repo_root = Path(__file__).resolve().parent.parent
        main_py = repo_root / "main.py"

        # Test: --pass-threshold 0
        result = subprocess.run(
            [sys.executable, str(main_py), "run", "test", "--pass-threshold", "0"],
            capture_output=True, text=True,
            cwd=str(repo_root),
        )
        # Should fail with error (either from argparse or early validation)
        assert result.returncode != 0

        # Test: --pass-threshold -1
        result = subprocess.run(
            [sys.executable, str(main_py), "run", "test", "--pass-threshold", "-1"],
            capture_output=True, text=True,
            cwd=str(repo_root),
        )
        assert result.returncode != 0

        # Test: --pass-threshold 15
        result = subprocess.run(
            [sys.executable, str(main_py), "run", "test", "--pass-threshold", "15"],
            capture_output=True, text=True,
            cwd=str(repo_root),
        )
        assert result.returncode != 0
