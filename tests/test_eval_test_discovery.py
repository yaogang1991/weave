"""Tests for evaluator test discovery and pytest timeout (#598, #601)."""
from unittest.mock import patch, MagicMock

from evaluator.runner import find_test_files, run_tests


class TestFindTestFilesFallback:
    """find_test_files discovers tests even without artifact hints (#598)."""

    def test_fallback_discovers_tests_in_project(self, tmp_path):
        """When artifacts don't match any test by name/stem, fallback finds all test_*.py."""
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_main.py").write_text("def test_ok(): pass")
        (tmp_path / "tests" / "test_utils.py").write_text("def test_ok(): pass")

        # Use an artifact that doesn't match any test file by name or stem
        result = find_test_files(["src/parser.py"], tmp_path)
        assert len(result) >= 2

    def test_fallback_discovers_tests_in_workdir(self, tmp_path):
        """When artifacts exist but no tests/ dir, fallback finds test_*.py in work_dir."""
        (tmp_path / "test_app.py").write_text("def test_ok(): pass")

        result = find_test_files(["src/app.py"], tmp_path)
        assert len(result) >= 1

    def test_artifact_match_takes_priority(self, tmp_path):
        """Direct artifact matches are found before fallback."""
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_parser.py").write_text("def test_ok(): pass")
        (tmp_path / "tests" / "test_other.py").write_text("def test_ok(): pass")

        result = find_test_files(["tests/test_parser.py"], tmp_path)
        assert "tests/test_parser.py" in result

    def test_no_tests_returns_empty(self, tmp_path):
        """When no test files exist anywhere, returns empty list."""
        result = find_test_files([], tmp_path)
        assert result == []


class TestPytestTimeoutIncreased:
    """Verify pytest timeout was increased from 60s (#601)."""

    def test_run_tests_uses_180s_timeout(self, tmp_path):
        """run_tests calls subprocess.run with timeout=180 (#601)."""
        (tmp_path / "test_dummy.py").write_text("def test_ok(): pass")

        with patch("evaluator.runner.run_with_progress") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="1 passed", stderr="",
            )
            run_tests(tmp_path, ["test_dummy.py"])

        _, kwargs = mock_run.call_args
        assert kwargs["timeout"] == 180, (
            f"Expected timeout=180, got {kwargs['timeout']}"
        )
