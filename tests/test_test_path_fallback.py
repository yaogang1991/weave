"""
Tests for #218: FILE_EXISTS fallback for test file path convention mismatch.

When the planner expects test files inside module dirs (e.g.
fileutils/test_hasher.py) but the agent places them in tests/
(e.g. tests/test_fileutils_hasher.py), the evaluator should find
them via fallback search.
"""
import pytest
from pathlib import Path

from evaluator.checkers.file_exists import FileExistsChecker
from evaluator.engine import EvaluatorEngine
from evaluator.models import EvaluationContext
from core.models import SuccessCriterion, CriterionType
from unittest.mock import MagicMock


@pytest.fixture
def tmp_store(tmp_path):
    from session.store import SessionStore
    return SessionStore(base_path=str(tmp_path / "events"))


@pytest.fixture
def evaluator(tmp_store):
    return EvaluatorEngine(tmp_store)


class TestFindTestFileAlternative:
    """Unit tests for _find_test_file_alternative."""

    def test_module_test_in_tests_dir(self, tmp_path):
        """module/test_x.py → finds tests/test_module_x.py."""
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_fileutils_hasher.py").write_text("ok", encoding="utf-8")

        result = FileExistsChecker._find_test_file_alternative(
            "fileutils/test_hasher.py", tmp_path,
        )
        assert result is not None
        assert "test_fileutils_hasher.py" in result

    def test_module_test_in_tests_dir_simple(self, tmp_path):
        """module/test_x.py → finds tests/test_x.py."""
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_hasher.py").write_text("ok", encoding="utf-8")

        result = FileExistsChecker._find_test_file_alternative(
            "fileutils/test_hasher.py", tmp_path,
        )
        assert result is not None
        assert "test_hasher.py" in result

    def test_bare_test_in_tests_dir(self, tmp_path):
        """test_x.py (no parent dir) → finds tests/test_x.py."""
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_main.py").write_text("ok", encoding="utf-8")

        result = FileExistsChecker._find_test_file_alternative(
            "test_main.py", tmp_path,
        )
        assert result is not None
        assert "test_main.py" in result

    def test_non_test_file_returns_none(self, tmp_path):
        """Non-test files should not trigger fallback."""
        result = FileExistsChecker._find_test_file_alternative(
            "src/main.py", tmp_path,
        )
        assert result is None

    def test_empty_file_is_matched(self, tmp_path):
        """Empty alternative files should be matched (e.g. __init__.py)."""
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_hasher.py").write_text("", encoding="utf-8")

        result = FileExistsChecker._find_test_file_alternative(
            "fileutils/test_hasher.py", tmp_path,
        )
        assert result is not None

    def test_no_alternative_found(self, tmp_path):
        """When no alternative exists, returns None."""
        result = FileExistsChecker._find_test_file_alternative(
            "fileutils/test_hasher.py", tmp_path,
        )
        assert result is None


class TestFileExistsWithFallback:
    """Integration tests: FILE_EXISTS criterion uses fallback for test files."""

    def test_missing_test_file_found_via_fallback(self, evaluator, tmp_path):
        """FILE_EXISTS finds test file in tests/ via fallback."""
        # Create the actual file in tests/ convention
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "test_fileutils_hasher.py").write_text(
            "def test_hash(): pass\n", encoding="utf-8",
        )

        # Criterion expects module-internal convention
        crit = SuccessCriterion(
            type=CriterionType.FILE_EXISTS,
            path="fileutils/test_hasher.py",
            description="hasher tests exist",
        )

        passed, msg, auto = evaluator._check_criterion(
            crit, str(tmp_path),
        )
        assert passed
        assert "fallback" in msg.lower() or "alternative" in msg.lower() or "verified" in msg.lower()

    def test_source_file_still_requires_exact_match(self, evaluator, tmp_path):
        """Non-test source files must still match exactly (no fallback)."""
        # Create a source file with different name
        (tmp_path / "utils.py").write_text("x = 1\n", encoding="utf-8")

        crit = SuccessCriterion(
            type=CriterionType.FILE_EXISTS,
            path="src/helpers.py",
            description="helper module",
        )

        passed, msg, auto = evaluator._check_criterion(
            crit, str(tmp_path),
        )
        assert not passed
        assert "missing" in msg.lower()
