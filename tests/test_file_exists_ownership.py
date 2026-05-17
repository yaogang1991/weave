"""
Tests for #332: file_pattern criterion should only match node-owned files.

When a file_pattern criterion like "tests/test_*.py" is used, it should
cross-reference against output_artifacts to avoid false PASS from
pre-existing harness test files.
"""
import pytest
from pathlib import Path

from core.models import SuccessCriterion, CriterionType
from evaluator.checkers.file_exists import FileExistsChecker
from evaluator.models import EvaluationContext


@pytest.fixture
def checker():
    return FileExistsChecker()


def _context(work_dir: Path, artifacts: list[str] | None = None) -> EvaluationContext:
    return EvaluationContext(
        work_dir=work_dir,
        artifacts=artifacts,
        session_store=None,
    )


class TestFilePatternOwnership:
    def test_matches_owned_files(self, checker, tmp_path):
        """Pattern matches files that ARE in output_artifacts."""
        (tmp_path / "test_parser.py").write_text("x = 1")
        (tmp_path / "test_types.py").write_text("y = 2")

        crit = SuccessCriterion(
            type=CriterionType.FILE_PATTERN,
            pattern="test_*.py",
        )
        ctx = _context(tmp_path, artifacts=["test_parser.py", "test_types.py"])
        result = checker.check(crit, ctx)
        assert result.passed

    def test_rejects_preexisting_only(self, checker, tmp_path):
        """Pattern matches ONLY pre-existing files → FAIL (#332)."""
        # Pre-existing Weave test files (not created by this node)
        (tmp_path / "test_main.py").write_text("x = 1")
        (tmp_path / "test_utils.py").write_text("y = 2")

        crit = SuccessCriterion(
            type=CriterionType.FILE_PATTERN,
            pattern="test_*.py",
        )
        # Node didn't create any test files — output_artifacts are source files
        ctx = _context(tmp_path, artifacts=["src/parser.py", "src/types.py"])
        result = checker.check(crit, ctx)
        assert not result.passed
        assert "pre-existing" in result.message

    def test_mixed_owned_and_preexisting(self, checker, tmp_path):
        """Pattern matches both owned and pre-existing → PASS, reports owned only."""
        # Pre-existing
        (tmp_path / "test_main.py").write_text("x = 1")
        # Owned by this node
        (tmp_path / "test_parser.py").write_text("x = 1")

        crit = SuccessCriterion(
            type=CriterionType.FILE_PATTERN,
            pattern="test_*.py",
        )
        ctx = _context(tmp_path, artifacts=["test_parser.py"])
        result = checker.check(crit, ctx)
        assert result.passed
        # Should report only 1 file (the owned one), not 2
        assert "1 file(s)" in result.message

    def test_no_artifacts_skips_ownership_check(self, checker, tmp_path):
        """Without output_artifacts, any match is accepted (backward compat)."""
        (tmp_path / "test_main.py").write_text("x = 1")

        crit = SuccessCriterion(
            type=CriterionType.FILE_PATTERN,
            pattern="test_*.py",
        )
        ctx = _context(tmp_path, artifacts=None)
        result = checker.check(crit, ctx)
        assert result.passed

    def test_no_matching_files_fails(self, checker, tmp_path):
        """No files match the pattern → FAIL."""
        crit = SuccessCriterion(
            type=CriterionType.FILE_PATTERN,
            pattern="test_*.py",
        )
        ctx = _context(tmp_path, artifacts=["src/parser.py"])
        result = checker.check(crit, ctx)
        assert not result.passed

    def test_nested_pattern_with_ownership(self, checker, tmp_path):
        """Pattern like tests/test_*.py with nested structure."""
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        # Pre-existing Weave test
        (tests_dir / "test_harness.py").write_text("x = 1")
        # Node's own test
        (tests_dir / "test_argkit.py").write_text("y = 2")

        crit = SuccessCriterion(
            type=CriterionType.FILE_PATTERN,
            pattern="tests/test_*.py",
        )
        ctx = _context(
            tmp_path,
            artifacts=["tests/test_argkit.py", "argkit/parser.py"],
        )
        result = checker.check(crit, ctx)
        assert result.passed
        assert "test_argkit.py" in result.message
        assert "test_harness.py" not in result.message

    def test_52_preexisting_weave_tests_scenario(self, checker, tmp_path):
        """Exact scenario from #332: 52 pre-existing harness tests."""
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        # Simulate 52 pre-existing harness test files
        for i in range(52):
            (tests_dir / f"test_harness_{i:03d}.py").write_text(f"x = {i}")

        crit = SuccessCriterion(
            type=CriterionType.FILE_PATTERN,
            pattern="tests/test_*.py",
        )
        # Node only created source files, no test files
        ctx = _context(
            tmp_path,
            artifacts=["argkit/parser.py", "argkit/types.py"],
        )
        result = checker.check(crit, ctx)
        assert not result.passed
        assert "pre-existing" in result.message


class TestFilterOwned:
    def test_matches_relative_paths(self, tmp_path):
        """_filter_owned matches relative artifact paths."""
        from evaluator.checkers.file_exists import FileExistsChecker

        files = [tmp_path / "src" / "a.py", tmp_path / "src" / "b.py"]
        result = FileExistsChecker._filter_owned(
            files, ["src/a.py"], tmp_path,
        )
        assert len(result) == 1
        assert result[0].name == "a.py"

    def test_matches_absolute_artifacts(self, tmp_path):
        """_filter_owned normalizes absolute artifact paths."""
        from evaluator.checkers.file_exists import FileExistsChecker

        files = [tmp_path / "src" / "a.py"]
        result = FileExistsChecker._filter_owned(
            files, [str(tmp_path / "src" / "a.py")], tmp_path,
        )
        assert len(result) == 1
