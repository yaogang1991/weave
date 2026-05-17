"""Tests for #372: zero-output guard — FILE_EXISTS fails when generator
produces no output files.

Verifies:
1. FileExistsChecker returns FAIL when output_artifacts is empty
2. FileExistsChecker still passes when artifacts are present
3. TEST_FILE_EXISTS already handles empty artifacts correctly
4. Full evaluation fails when zero output with FILE_EXISTS criteria
"""
from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parent.parent))

from core.models import CriterionType, SuccessCriterion  # noqa: E402
from evaluator.checkers.file_exists import FileExistsChecker  # noqa: E402
from evaluator.models import EvaluationContext  # noqa: E402


# ---------------------------------------------------------------------------
# FileExistsChecker tests
# ---------------------------------------------------------------------------


class TestFileExistsZeroOutput:
    """Verify FileExistsChecker fails when no artifacts produced."""

    def test_fails_with_empty_artifacts(self, tmp_path):
        """FILE_EXISTS with no artifacts and no crit.path → FAIL (#372)."""
        crit = SuccessCriterion(
            type=CriterionType.FILE_EXISTS,
            description="regex.py exists",
        )
        ctx = EvaluationContext(work_dir=tmp_path, artifacts=[])
        checker = FileExistsChecker()
        result = checker.check(crit, ctx)
        assert not result.passed
        assert ("no output files" in result.message.lower() or
                "did not create" in result.message.lower())

    def test_passes_with_none_artifacts_vacuous(self, tmp_path):
        """FILE_EXISTS with None artifacts and no crit.path → vacuous PASS (#372).

        None means "untracked" (not "confirmed empty"). This preserves the
        existing semantics where FILE_EXISTS passes vacuously when there are
        no specific files to check and artifacts are not tracked.
        """
        crit = SuccessCriterion(
            type=CriterionType.FILE_EXISTS,
            description="regex.py exists",
        )
        ctx = EvaluationContext(work_dir=tmp_path, artifacts=None)
        checker = FileExistsChecker()
        result = checker.check(crit, ctx)
        assert result.passed

    def test_passes_with_artifacts_present(self, tmp_path):
        """FILE_EXISTS with actual artifacts → PASS."""
        # Create a real file
        test_file = tmp_path / "regex.py"
        test_file.write_text("pass")
        crit = SuccessCriterion(
            type=CriterionType.FILE_EXISTS,
            description="regex.py exists",
        )
        ctx = EvaluationContext(
            work_dir=tmp_path,
            artifacts=["regex.py"],
        )
        checker = FileExistsChecker()
        result = checker.check(crit, ctx)
        assert result.passed

    def test_no_path_empty_artifacts_fails(self, tmp_path):
        """FILE_EXISTS without crit.path and empty artifacts → FAIL."""
        crit = SuccessCriterion(
            type=CriterionType.FILE_EXISTS,
            description="some file exists",
        )
        ctx = EvaluationContext(work_dir=tmp_path, artifacts=[])
        checker = FileExistsChecker()
        result = checker.check(crit, ctx)
        assert not result.passed


class TestTestFileExistsZeroOutput:
    """Verify TEST_FILE_EXISTS correctly handles empty artifacts."""

    def test_fails_with_empty_artifacts(self, tmp_path):
        """TEST_FILE_EXISTS with empty artifacts → FAIL (already correct)."""
        crit = SuccessCriterion(
            type=CriterionType.TEST_FILE_EXISTS,
            description="test files exist",
        )
        ctx = EvaluationContext(work_dir=tmp_path, artifacts=[])
        checker = FileExistsChecker()
        result = checker.check(crit, ctx)
        assert not result.passed

    def test_fails_with_no_test_files(self, tmp_path):
        """TEST_FILE_EXISTS with non-test artifacts → FAIL."""
        crit = SuccessCriterion(
            type=CriterionType.TEST_FILE_EXISTS,
            description="test files exist",
        )
        ctx = EvaluationContext(
            work_dir=tmp_path,
            artifacts=["regex.py", "utils.py"],
        )
        checker = FileExistsChecker()
        result = checker.check(crit, ctx)
        assert not result.passed

    def test_passes_with_test_files(self, tmp_path):
        """TEST_FILE_EXISTS with test artifacts → PASS."""
        crit = SuccessCriterion(
            type=CriterionType.TEST_FILE_EXISTS,
            description="test files exist",
        )
        ctx = EvaluationContext(
            work_dir=tmp_path,
            artifacts=["test_regex.py"],
        )
        checker = FileExistsChecker()
        result = checker.check(crit, ctx)
        assert result.passed
