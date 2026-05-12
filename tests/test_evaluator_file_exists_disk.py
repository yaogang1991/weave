"""Tests for FILE_EXISTS criterion: disk verification, not output_artifacts trust.

Regression tests for issue #158: evaluator must verify files on disk,
not blindly trust agent-reported output_artifacts.
"""
import tempfile

import pytest

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


class TestFileExistsDiskVerification:
    """Core regression: output_artifacts must NOT bypass disk check."""

    def test_artifact_present_disk_present_passes(self, evaluator, work_dir):
        """Agent reports artifact AND file exists on disk → PASS."""
        (work_dir / "report.py").write_text("print('hi')", encoding="utf-8")
        crit = SuccessCriterion(type=CriterionType.FILE_EXISTS, path="report.py")
        passed, msg, auto = evaluator._check_criterion(
            crit, str(work_dir), output_artifacts=["report.py"],
        )
        assert passed is True
        assert auto is True

    def test_artifact_present_disk_missing_fails(self, evaluator, work_dir):
        """Agent reports artifact but file NOT on disk → FAIL (core regression)."""
        crit = SuccessCriterion(type=CriterionType.FILE_EXISTS, path="missing.py")
        passed, msg, auto = evaluator._check_criterion(
            crit, str(work_dir), output_artifacts=["missing.py"],
        )
        assert passed is False
        assert "missing" in msg.lower() or "failed" in msg.lower()

    def test_artifact_present_empty_file_fails(self, evaluator, work_dir):
        """Agent reports artifact, file exists but is empty → FAIL."""
        (work_dir / "empty.py").write_text("", encoding="utf-8")
        crit = SuccessCriterion(type=CriterionType.FILE_EXISTS, path="empty.py")
        passed, msg, auto = evaluator._check_criterion(
            crit, str(work_dir), output_artifacts=["empty.py"],
        )
        assert passed is False
        assert "empty" in msg.lower()

    def test_no_artifacts_disk_exists_passes(self, evaluator, work_dir):
        """No output_artifacts, but planner path exists on disk → PASS."""
        (work_dir / "utils.py").write_text("def f(): pass", encoding="utf-8")
        crit = SuccessCriterion(type=CriterionType.FILE_EXISTS, path="utils.py")
        passed, msg, auto = evaluator._check_criterion(
            crit, str(work_dir), output_artifacts=None,
        )
        assert passed is True

    def test_no_artifacts_no_path_defaults_pass(self, evaluator, work_dir):
        """No artifacts and no path → PASS (nothing to verify)."""
        crit = SuccessCriterion(type=CriterionType.FILE_EXISTS)
        passed, msg, auto = evaluator._check_criterion(
            crit, str(work_dir), output_artifacts=None,
        )
        assert passed is True

    def test_absolute_path_resolved(self, evaluator, work_dir):
        """Absolute path is resolved correctly."""
        f = work_dir / "abs_test.py"
        f.write_text("x = 1", encoding="utf-8")
        crit = SuccessCriterion(type=CriterionType.FILE_EXISTS, path=str(f))
        passed, msg, auto = evaluator._check_criterion(
            crit, str(work_dir), output_artifacts=None,
        )
        assert passed is True

    def test_relative_path_resolved(self, evaluator, work_dir):
        """Relative path resolved against work_dir."""
        sub = work_dir / "src"
        sub.mkdir()
        (sub / "main.py").write_text("print('main')", encoding="utf-8")
        crit = SuccessCriterion(type=CriterionType.FILE_EXISTS, path="src/main.py")
        passed, msg, auto = evaluator._check_criterion(
            crit, str(work_dir), output_artifacts=None,
        )
        assert passed is True

    def test_loose_match_fallback(self, evaluator, work_dir):
        """Exact path missing but loose match finds file → PASS."""
        (work_dir / "test_thing.py").write_text("def test_x(): pass", encoding="utf-8")
        crit = SuccessCriterion(
            type=CriterionType.FILE_EXISTS, path="tests/test_thing.py",
        )
        passed, msg, auto = evaluator._check_criterion(
            crit, str(work_dir), output_artifacts=None,
        )
        assert passed is True
        assert "verified on disk" in msg.lower()


class TestCheckFilesExistStrict:
    """Unit tests for _check_files_exist_strict."""

    def test_all_present(self, evaluator, work_dir):
        (work_dir / "a.py").write_text("x=1", encoding="utf-8")
        (work_dir / "b.py").write_text("y=2", encoding="utf-8")
        passed, msg = evaluator._check_files_exist_strict(
            ["a.py", "b.py"], work_dir,
        )
        assert passed is True

    def test_one_missing(self, evaluator, work_dir):
        (work_dir / "a.py").write_text("x=1", encoding="utf-8")
        passed, msg = evaluator._check_files_exist_strict(
            ["a.py", "z.py"], work_dir,
        )
        assert passed is False
        assert "z.py" in msg

    def test_empty_file_treated_as_missing(self, evaluator, work_dir):
        (work_dir / "empty.py").write_text("", encoding="utf-8")
        passed, msg = evaluator._check_files_exist_strict(
            ["empty.py"], work_dir,
        )
        assert passed is False
        assert "empty" in msg.lower()

    def test_absolute_path(self, evaluator, work_dir):
        f = work_dir / "abs.py"
        f.write_text("x = 1", encoding="utf-8")
        passed, msg = evaluator._check_files_exist_strict(
            [str(f)], work_dir,
        )
        assert passed is True
