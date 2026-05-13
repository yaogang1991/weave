"""
Tests for #194: evaluation pass threshold (score-based passing).

Covers:
- Default behavior (no threshold): all criteria must pass (backward compat)
- With threshold: score >= threshold → overall pass, failed criteria become WARN
- With threshold: score < threshold → overall still fails
- Threshold=None = strict mode (same as no threshold)
- Threshold edge cases: 0.0 (always pass), 10.0 (strict)
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
        """2 of 4 criteria pass → score=5.0, threshold=7.0 → fail."""
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
        # score = 5.0, threshold = 7.0 → still fails
        assert not result.passed

    def test_score_meets_threshold_passes(self, threshold_evaluator, tmp_path):
        """3 of 4 criteria pass → score=7.5, threshold=7.0 → pass."""
        for f in ["a.py", "b.py", "c.py"]:
            (tmp_path / f).write_text("ok", encoding="utf-8")
        result = threshold_evaluator.evaluate_stage(
            "s1", "impl",
            [
                SuccessCriterion(type=CriterionType.FILE_EXISTS, path="a.py", description="file a"),
                SuccessCriterion(type=CriterionType.FILE_EXISTS, path="b.py", description="file b"),
                SuccessCriterion(type=CriterionType.FILE_EXISTS, path="c.py", description="file c"),
                SuccessCriterion(type=CriterionType.FILE_EXISTS, path="missing.py", description="file d"),
            ],
            str(tmp_path),
        )
        # score = 7.5, threshold = 7.0 → passes
        assert result.passed
        # Failed criterion should be WARN, not FAIL
        assert "WARN" in result.feedback
        assert "FAIL" not in result.feedback

    def test_perfect_score_still_passes(self, threshold_evaluator, tmp_path):
        (tmp_path / "a.py").write_text("ok", encoding="utf-8")
        result = threshold_evaluator.evaluate_stage(
            "s1", "impl",
            [SuccessCriterion(type=CriterionType.FILE_EXISTS, path="a.py", description="file a")],
            str(tmp_path),
        )
        assert result.passed
        assert result.score == 10.0

    def test_zero_threshold_rejected(self, tmp_store, tmp_path):
        """pass_threshold=0 is invalid — would pass all criteria regardless."""
        with pytest.raises(ValueError, match="pass_threshold must be > 0"):
            EvaluatorEngine(tmp_store, pass_threshold=0.0)

    def test_negative_threshold_rejected(self, tmp_store, tmp_path):
        """Negative pass_threshold is invalid."""
        with pytest.raises(ValueError, match="pass_threshold must be > 0"):
            EvaluatorEngine(tmp_store, pass_threshold=-1.0)

    def test_feedback_shows_warn_for_failed_criteria(self, threshold_evaluator, tmp_path):
        """Failed criteria are downgraded from FAIL to WARN in feedback."""
        (tmp_path / "a.py").write_text("ok", encoding="utf-8")
        result = threshold_evaluator.evaluate_stage(
            "s1", "impl",
            [
                SuccessCriterion(type=CriterionType.FILE_EXISTS, path="a.py", description="file a"),
                SuccessCriterion(type=CriterionType.FILE_EXISTS, path="missing.py", description="file b"),
            ],
            str(tmp_path),
        )
        # score = 5.0, threshold = 7.0 → fails, feedback has FAIL
        assert not result.passed
        assert "FAIL" in result.feedback

    def test_feedback_no_fail_when_above_threshold(self, threshold_evaluator, tmp_path):
        """When score >= threshold, feedback should not contain FAIL."""
        (tmp_path / "a.py").write_text("ok", encoding="utf-8")
        (tmp_path / "b.py").write_text("ok", encoding="utf-8")
        (tmp_path / "c.py").write_text("ok", encoding="utf-8")
        result = threshold_evaluator.evaluate_stage(
            "s1", "impl",
            [
                SuccessCriterion(type=CriterionType.FILE_EXISTS, path="a.py", description="file a"),
                SuccessCriterion(type=CriterionType.FILE_EXISTS, path="b.py", description="file b"),
                SuccessCriterion(type=CriterionType.FILE_EXISTS, path="c.py", description="file c"),
                SuccessCriterion(type=CriterionType.FILE_EXISTS, path="missing.py", description="file d"),
            ],
            str(tmp_path),
        )
        assert result.passed
        # Should have WARN for failed criterion, not FAIL
        for line in result.feedback.split("\n"):
            if "file d" in line:
                assert line.startswith("WARN"), f"Expected WARN, got: {line}"
