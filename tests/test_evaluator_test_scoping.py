"""
Tests for #249: scope pytest to current artifacts to avoid leftover test files.

Verifies that _run_tests and _check_coverage only collect test files related
to the current DAG node's output_artifacts, not all test files in the workspace.
"""
import pytest  # noqa: F401
from unittest.mock import MagicMock, patch

from evaluator.engine import EvaluatorEngine
from core.models import SuccessCriterion, CriterionType


class TestFindTestFiles:
    """Tests for _find_test_files helper."""

    def test_finds_test_files_from_artifacts(self, tmp_path):
        """Direct test files in output_artifacts are returned."""
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_parser.py").write_text("def test_x(): pass")
        (tmp_path / "parser.py").write_text("x = 1")

        engine = EvaluatorEngine(MagicMock())
        result = engine._find_test_files(
            ["parser.py", "tests/test_parser.py"],
            tmp_path,
        )
        assert "tests/test_parser.py" in result

    def test_infers_test_files_from_source_stem(self, tmp_path):
        """Infers test_parser.py from parser.py when test file not in artifacts."""
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_parser.py").write_text("def test_x(): pass")
        (tmp_path / "parser.py").write_text("x = 1")

        engine = EvaluatorEngine(MagicMock())
        result = engine._find_test_files(["parser.py"], tmp_path)
        assert any("test_parser.py" in r for r in result)

    def test_excludes_unrelated_test_files(self, tmp_path):
        """Leftover test files not matching artifacts are excluded."""
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_parser.py").write_text("def test_x(): pass")
        (tmp_path / "tests" / "test_oldlib.py").write_text("def test_y(): pass")
        (tmp_path / "parser.py").write_text("x = 1")

        engine = EvaluatorEngine(MagicMock())
        result = engine._find_test_files(["parser.py", "tests/test_parser.py"], tmp_path)
        assert any("test_parser.py" in r for r in result)
        assert not any("test_oldlib.py" in r for r in result)

    def test_returns_empty_for_no_artifacts(self, tmp_path):
        """Returns empty list when no artifacts match."""
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_other.py").write_text("def test_z(): pass")

        engine = EvaluatorEngine(MagicMock())
        result = engine._find_test_files([], tmp_path)
        assert result == []

    def test_finds_test_files_at_workdir_root(self, tmp_path):
        """Finds test files at work_dir root, not just tests/ subdirectory."""
        (tmp_path / "test_parser.py").write_text("def test_x(): pass")
        (tmp_path / "parser.py").write_text("x = 1")

        engine = EvaluatorEngine(MagicMock())
        result = engine._find_test_files(["parser.py"], tmp_path)
        assert any("test_parser.py" in r for r in result)


class TestTestScopingIntegration:
    """Integration tests that pytest is scoped to relevant files."""

    @patch("evaluator.runner.subprocess.run")
    def test_tests_pass_scoped_to_artifact_tests(self, mock_run, tmp_path):
        """_check_criterion for TESTS_PASS passes scoped test paths to _run_tests."""
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_parser.py").write_text("def test_x(): pass")
        (tmp_path / "parser.py").write_text("x = 1")

        mock_run.return_value = MagicMock(returncode=0, stdout="1 passed", stderr="")

        engine = EvaluatorEngine(MagicMock())
        crit = SuccessCriterion(type=CriterionType.TESTS_PASS, description="tests pass")
        passed, msg, auto = engine._check_criterion(
            crit, str(tmp_path), output_artifacts=["parser.py", "tests/test_parser.py"],
        )

        assert passed is True
        # Verify pytest was called with scoped test files, not the whole directory
        call_args = mock_run.call_args[0][0]
        assert "test_parser.py" in " ".join(call_args)
        # Should NOT have bare "pytest -v" without test targets
        assert call_args[-1].endswith("test_parser.py") or any(
            "test_parser" in a for a in call_args
        )
