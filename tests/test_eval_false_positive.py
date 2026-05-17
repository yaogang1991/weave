"""
Tests for #234: evaluator false positive prevention.

Verifies:
1. TESTS_PASS warns (not passes) when no test files exist
2. Artifact verification catches phantom output files
"""
from unittest.mock import MagicMock

from core.models import CriterionType, SuccessCriterion
from evaluator.engine import EvaluatorEngine


def _make_engine():
    mock_store = MagicMock()
    mock_store.emit_event = MagicMock()
    return EvaluatorEngine(session_store=mock_store)


class TestTestsPassNoTests:
    """TESTS_PASS should WARN when no test files exist (#234)."""

    def test_no_tests_emits_warning(self):
        """No test files → was_auto=False (WARN, not PASS)."""
        engine = _make_engine()
        passed, msg, was_auto = engine._check_criterion(
            SuccessCriterion(type=CriterionType.TESTS_PASS, description="tests pass"),
            "/nonexistent/work",
        )
        assert passed  # WARN still counts as "passed" for scoring
        assert not was_auto  # But it's a WARNING, not a PASS
        assert "not verified" in msg.lower()

    def test_no_tests_through_evaluate_stage(self, tmp_path):
        """evaluate_stage should mark no-test as uncheckable (WARN)."""
        engine = _make_engine()
        result = engine.evaluate_stage(
            session_id="s1",
            stage_name="impl",
            criteria=["tests pass"],
            artifact_path=str(tmp_path),
            work_dir=str(tmp_path),
        )
        # Should pass overall (WARN doesn't fail) but with suggestion
        assert "tests pass" in result.suggestions
        assert "WARN" in result.feedback or "not verified" in result.feedback

    def test_no_tests_with_existing_test_file(self, tmp_path):
        """If test files exist, was_auto=True (normal PASS/FAIL)."""
        (tmp_path / "test_app.py").write_text("def test_ok(): pass", encoding="utf-8")
        engine = _make_engine()
        passed, msg, was_auto = engine._check_criterion(
            SuccessCriterion(type=CriterionType.TESTS_PASS, description="tests pass"),
            str(tmp_path),
            output_artifacts=["test_app.py"],
        )
        assert was_auto  # Normal automated check


class TestArtifactVerification:
    """Mandatory artifact-on-disk check prevents false positives (#234)."""

    def test_phantom_artifact_fails_evaluation(self, tmp_path):
        """If output_artifacts contains files not on disk, evaluation fails."""
        engine = _make_engine()
        result = engine.evaluate_stage(
            session_id="s1",
            stage_name="impl",
            criteria=["lint clean"],
            artifact_path=str(tmp_path),
            work_dir=str(tmp_path),
            output_artifacts=["serializer.py"],  # Doesn't exist
        )
        assert not result.passed
        assert "phantom" in result.feedback.lower() or "not found on disk" in result.feedback.lower()

    def test_all_artifacts_present_passes(self, tmp_path):
        """If all output_artifacts exist on disk, evaluation passes."""
        (tmp_path / "app.py").write_text("print('hi')\n", encoding="utf-8")
        engine = _make_engine()
        result = engine.evaluate_stage(
            session_id="s1",
            stage_name="impl",
            criteria=["lint clean"],
            artifact_path=str(tmp_path),
            work_dir=str(tmp_path),
            output_artifacts=["app.py"],
        )
        assert result.passed

    def test_partial_phantom_artifacts_fails(self, tmp_path):
        """If some artifacts exist but others don't, evaluation fails."""
        (tmp_path / "lexer.py").write_text("x=1", encoding="utf-8")
        engine = _make_engine()
        result = engine.evaluate_stage(
            session_id="s1",
            stage_name="impl",
            criteria=["lint clean"],
            artifact_path=str(tmp_path),
            work_dir=str(tmp_path),
            output_artifacts=["lexer.py", "parser.py", "serializer.py"],
        )
        assert not result.passed
        assert "2" in result.feedback or "parser.py" in result.feedback

    def test_empty_artifacts_skips_check(self, tmp_path):
        """No output_artifacts → no phantom check (backward compatible)."""
        engine = _make_engine()
        result = engine.evaluate_stage(
            session_id="s1",
            stage_name="impl",
            criteria=["lint clean"],
            artifact_path=str(tmp_path),
            work_dir=str(tmp_path),
            output_artifacts=None,
        )
        # Should pass (no artifacts to verify)
        assert result.passed

    def test_zero_byte_file_treated_as_valid(self, tmp_path):
        """Empty (0-byte) artifact file is valid (e.g. __init__.py)."""
        (tmp_path / "empty.py").write_text("", encoding="utf-8")
        engine = _make_engine()
        result = engine.evaluate_stage(
            session_id="s1",
            stage_name="impl",
            criteria=["lint clean"],
            artifact_path=str(tmp_path),
            work_dir=str(tmp_path),
            output_artifacts=["empty.py"],
        )
        assert result.passed

    def test_phantom_overrides_criteria_pass(self, tmp_path):
        """Even if all criteria pass, phantom artifacts cause failure."""
        engine = _make_engine()
        # No criteria that would fail → all would pass
        result = engine.evaluate_stage(
            session_id="s1",
            stage_name="impl",
            criteria=[],  # No criteria → all pass trivially
            artifact_path=str(tmp_path),
            work_dir=str(tmp_path),
            output_artifacts=["nonexistent.py"],
        )
        assert not result.passed
        assert result.score == 0.0
