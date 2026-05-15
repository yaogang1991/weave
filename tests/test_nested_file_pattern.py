"""
Tests for #253: file_pattern should match nested subdirectories.

Verifies that glob patterns like "validlib/*.py" automatically fall back
to recursive matching when the initial pattern doesn't find files in
nested subdirectory structures.
"""
import pytest
from pathlib import Path
from unittest.mock import MagicMock

from evaluator.checkers.file_exists import FileExistsChecker
from evaluator.models import EvaluationContext
from core.models import SuccessCriterion, CriterionType


@pytest.fixture
def checker():
    return FileExistsChecker()


def _make_context(work_dir, artifacts=None):
    return EvaluationContext(
        work_dir=work_dir,
        artifacts=artifacts,
        session_store=MagicMock(),
    )


class TestNestedFilePattern:
    """file_pattern matches files in nested subdirectories."""

    def test_flat_glob_matches_nested_files(self, tmp_path, checker):
        """Pattern 'lib/*.py' finds files in lib/core/ and lib/primitives/."""
        (tmp_path / "validlib" / "core").mkdir(parents=True)
        (tmp_path / "validlib" / "primitives").mkdir(parents=True)
        (tmp_path / "validlib" / "core" / "validator.py").write_text("x = 1")
        (tmp_path / "validlib" / "primitives" / "string.py").write_text("y = 2")

        crit = SuccessCriterion(
            type=CriterionType.FILE_PATTERN,
            pattern="validlib/*.py",
            description="validlib modules exist",
        )
        ctx = _make_context(tmp_path, artifacts=[
            str(tmp_path / "validlib" / "core" / "validator.py"),
            str(tmp_path / "validlib" / "primitives" / "string.py"),
        ])
        result = checker.check(crit, ctx)
        assert result.passed is True
        assert "validator.py" in result.message or "string.py" in result.message

    def test_deeply_nested_structure(self, tmp_path, checker):
        """Pattern matches files 3+ levels deep."""
        (tmp_path / "lib" / "a" / "b" / "c").mkdir(parents=True)
        (tmp_path / "lib" / "a" / "b" / "c" / "deep.py").write_text("z = 3")

        crit = SuccessCriterion(
            type=CriterionType.FILE_PATTERN,
            pattern="lib/*.py",
            description="lib modules",
        )
        ctx = _make_context(tmp_path, artifacts=[
            str(tmp_path / "lib" / "a" / "b" / "c" / "deep.py"),
        ])
        result = checker.check(crit, ctx)
        assert result.passed is True

    def test_flat_match_preferred_over_recursive(self, tmp_path, checker):
        """When flat pattern matches, recursive fallback is not needed."""
        (tmp_path / "lib").mkdir()
        (tmp_path / "lib" / "main.py").write_text("x = 1")

        crit = SuccessCriterion(
            type=CriterionType.FILE_PATTERN,
            pattern="lib/*.py",
            description="lib modules",
        )
        ctx = _make_context(tmp_path, artifacts=[
            str(tmp_path / "lib" / "main.py"),
        ])
        result = checker.check(crit, ctx)
        assert result.passed is True
        assert "main.py" in result.message

    def test_recursive_fallback_only_for_glob_patterns(self, tmp_path, checker):
        """Non-glob patterns (no *) don't trigger recursive fallback."""
        (tmp_path / "lib").mkdir()
        (tmp_path / "lib" / "module.py").write_text("x = 1")

        crit = SuccessCriterion(
            type=CriterionType.FILE_PATTERN,
            pattern="lib/module.py",
            description="exact file",
        )
        ctx = _make_context(tmp_path, artifacts=[
            str(tmp_path / "lib" / "module.py"),
        ])
        result = checker.check(crit, ctx)
        assert result.passed is True

    def test_mixed_flat_and_nested(self, tmp_path, checker):
        """Pattern matches both flat and nested files."""
        (tmp_path / "lib").mkdir()
        (tmp_path / "lib" / "core").mkdir()
        (tmp_path / "lib" / "main.py").write_text("x = 1")
        (tmp_path / "lib" / "core" / "handler.py").write_text("y = 2")

        crit = SuccessCriterion(
            type=CriterionType.FILE_PATTERN,
            pattern="lib/*.py",
            description="lib modules",
        )
        ctx = _make_context(tmp_path, artifacts=[
            str(tmp_path / "lib" / "main.py"),
            str(tmp_path / "lib" / "core" / "handler.py"),
        ])
        result = checker.check(crit, ctx)
        assert result.passed is True
        assert "main.py" in result.message
        assert "handler.py" in result.message
