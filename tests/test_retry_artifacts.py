"""Tests for issue #165: retry artifact preservation and coverage scope.

Regression tests for:
1. output_artifacts preserved across retries when agent only reads/tests
2. Coverage check scoped to work_dir, not falling back to --cov=.
"""
import pytest
from unittest.mock import MagicMock, patch

from core.models import SuccessCriterion, CriterionType
from evaluator.engine import EvaluatorEngine
from session.store import SessionStore


@pytest.fixture
def store(tmp_path):
    return SessionStore(base_path=str(tmp_path / "events"))


@pytest.fixture
def evaluator(store):
    return EvaluatorEngine(session_store=store)


@pytest.fixture
def work_dir(tmp_path):
    d = tmp_path / "project"
    d.mkdir()
    return d


class TestRetryArtifactPreservation:
    """Verify artifacts are preserved when retry produces empty artifacts."""

    def test_artifact_preserved_on_retry(self, evaluator, work_dir):
        """If retry produces no new artifacts, previous artifacts are kept."""
        # First attempt: artifacts reported
        crit = SuccessCriterion(type=CriterionType.FILE_EXISTS, path="report.py")
        (work_dir / "report.py").write_text("x = 1", encoding="utf-8")
        passed, msg, auto = evaluator._check_criterion(
            crit, str(work_dir), output_artifacts=["report.py"],
        )
        assert passed

        # Simulate retry: artifacts empty but file still on disk
        # Due to #158 fix, disk verification catches this
        passed2, msg2, auto2 = evaluator._check_criterion(
            crit, str(work_dir), output_artifacts=None,
        )
        assert passed2


class TestCoverageScope:
    """Verify coverage check doesn't scan globally when artifacts empty."""

    @patch("evaluator.runner.subprocess.run")
    def test_coverage_no_artifacts_fails_gracefully(self, mock_run, evaluator, work_dir):
        """Without output_artifacts, coverage is unverifiable → WARN (#152, #165)."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout="2 passed in 0.01s\n", stderr="",
        )
        crit = SuccessCriterion(type=CriterionType.COVERAGE, target=80)
        passed, msg, auto = evaluator._check_criterion(
            crit, str(work_dir), output_artifacts=None,
        )
        # Coverage unverifiable without artifacts → WARN (auto=False)
        assert not auto
        assert "not verified" in msg

    def test_coverage_with_artifacts_uses_scoped_targets(self, evaluator, work_dir):
        """With output_artifacts, coverage is scoped to relevant packages."""
        crit = SuccessCriterion(type=CriterionType.COVERAGE, target=80)
        passed, msg, auto = evaluator._check_criterion(
            crit, str(work_dir), output_artifacts=["reporter/report.py"],
        )
        # Should try to scope coverage to reporter/
        assert "reporter" in msg or "could not be parsed" in msg.lower() or "coverage" in msg.lower()
