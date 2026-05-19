"""Tests for NotImplementedError guard in file_exists checker (#591)."""
from core.models import CriterionType, SuccessCriterion
from evaluator.checkers.file_exists import FileExistsChecker
from evaluator.models import EvaluationContext


def _make_context(tmp_path, artifacts=None):
    return EvaluationContext(
        work_dir=tmp_path,
        artifacts=artifacts or [],
        criteria=[],
        session_id="test",
        node_id="test_node",
    )


class TestGlobNotImplementedErrorGuard:
    """Verify glob() calls don't crash with non-relative patterns (#591)."""

    def test_try_stdlib_rename_handles_not_implemented_error(self, tmp_path):
        """_try_stdlib_rename catches NotImplementedError from glob (#591)."""
        checker = FileExistsChecker()
        # Path with ".." can trigger NotImplementedError in Python 3.12+
        result = checker._try_stdlib_rename("../etc/passwd", tmp_path)
        # Should return None, not crash
        assert result is None

    def test_file_pattern_with_absolute_path_no_crash(self, tmp_path):
        """file_pattern criterion with absolute path doesn't crash (#591)."""
        checker = FileExistsChecker()
        crit = SuccessCriterion(
            type=CriterionType.FILE_PATTERN,
            pattern="/absolute/path/*.py",
        )
        ctx = _make_context(tmp_path)
        result = checker.check(crit, ctx)
        # Should return a result, not raise
        assert result is not None
        assert hasattr(result, "passed")

    def test_file_pattern_with_dotdot_no_crash(self, tmp_path):
        """file_pattern with '..' in pattern doesn't crash (#591)."""
        checker = FileExistsChecker()
        crit = SuccessCriterion(
            type=CriterionType.FILE_PATTERN,
            pattern="../sibling/*.py",
        )
        ctx = _make_context(tmp_path)
        result = checker.check(crit, ctx)
        assert result is not None

    def test_file_exists_with_normal_pattern_still_works(self, tmp_path):
        """Normal relative patterns still work correctly."""
        checker = FileExistsChecker()
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("x = 1")
        crit = SuccessCriterion(
            type=CriterionType.FILE_EXISTS,
            path="src/main.py",
        )
        ctx = _make_context(tmp_path)
        result = checker.check(crit, ctx)
        assert result.passed

    def test_glob_stem_fallback_handles_error(self, tmp_path):
        """Loose stem glob fallback handles NotImplementedError (#591)."""
        checker = FileExistsChecker()
        # Create a file so the checker has something to verify
        (tmp_path / "target_file.py").write_text("x = 1")
        crit = SuccessCriterion(
            type=CriterionType.FILE_EXISTS,
            path="/nonexistent/deep/path/target_file.py",
        )
        ctx = _make_context(tmp_path)
        # Should not crash even with absolute path triggering glob issues
        result = checker.check(crit, ctx)
        assert result is not None
