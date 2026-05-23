"""Tests for #802: import smoke test must reset score on failure.

Verifies that when an import smoke test fails, the evaluation result has
both passed=False AND score=0.0 — consistent with the phantom-artifact
and zero-output post-checks.
"""
import pytest
from unittest.mock import patch

from core.models import SuccessCriterion, CriterionType
from evaluator.engine import EvaluatorEngine


@pytest.fixture
def tmp_store(tmp_path):
    from session.store import SessionStore
    return SessionStore(base_path=str(tmp_path / "events"))


@pytest.fixture
def evaluator(tmp_store):
    return EvaluatorEngine(tmp_store)


class TestImportSmokeScoreReset:
    """Import smoke test failure resets score to 0.0 (#802)."""

    @patch("evaluator.engine.import_smoke_test")
    def test_import_error_resets_score(self, mock_smoke, evaluator, tmp_path):
        """When import_smoke_test finds errors, score should be 0.0 not 10.0."""
        src = tmp_path / "src.py"
        src.write_text("x = 1\n")

        mock_smoke.return_value = [("src.py", "SyntaxError: invalid syntax")]

        result = evaluator.evaluate_stage(
            session_id="test",
            stage_name="node_a",
            criteria=[
                SuccessCriterion(
                    type=CriterionType.FILE_EXISTS,
                    path="src.py",
                    description="Source file exists",
                ),
            ],
            artifact_path=str(tmp_path),
            work_dir=str(tmp_path),
            output_artifacts=["src.py"],
        )

        assert result.passed is False
        assert result.score == 0.0, (
            f"Expected score=0.0 when import fails, got {result.score}"
        )

    @patch("evaluator.engine.import_smoke_test")
    def test_import_pass_preserves_score(self, mock_smoke, evaluator, tmp_path):
        """When import_smoke_test passes, score should remain 10.0."""
        src = tmp_path / "src.py"
        src.write_text("x = 1\n")

        mock_smoke.return_value = []

        result = evaluator.evaluate_stage(
            session_id="test",
            stage_name="node_a",
            criteria=[
                SuccessCriterion(
                    type=CriterionType.FILE_EXISTS,
                    path="src.py",
                    description="Source file exists",
                ),
            ],
            artifact_path=str(tmp_path),
            work_dir=str(tmp_path),
            output_artifacts=["src.py"],
        )

        assert result.passed is True
        assert result.score == 10.0
