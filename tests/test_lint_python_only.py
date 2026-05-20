"""
Tests for #208: _run_lint must only pass .py files to flake8.

Non-Python files (pyproject.toml, requirements.txt, etc.) must be
filtered out before calling flake8 to avoid E999 SyntaxError.
"""
import pytest
from unittest.mock import MagicMock, patch

from evaluator.engine import EvaluatorEngine


@pytest.fixture
def tmp_store(tmp_path):
    from session.store import SessionStore
    return SessionStore(base_path=str(tmp_path / "events"))


@pytest.fixture
def evaluator(tmp_store):
    return EvaluatorEngine(tmp_store)


class TestLintPythonOnlyFilter:
    """_run_lint must filter out non-Python files from targets."""

    def test_toml_file_excluded(self, evaluator, tmp_path):
        """pyproject.toml must NOT be passed to flake8."""
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
        (tmp_path / "code.py").write_text("x = 1\n", encoding="utf-8")
        with patch("evaluator.runner.run_with_progress") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            evaluator._run_lint(["pyproject.toml", "code.py"], tmp_path)
            # Check all calls — resolved file paths must be .py only
            for call_args in mock_run.call_args_list:
                cmd = call_args[0][0]
                resolved_files = [a for a in cmd if str(tmp_path) in a]
                for f in resolved_files:
                    assert f.endswith(".py"), f"Non-.py file in lint command: {f}"

    def test_txt_file_excluded(self, evaluator, tmp_path):
        """requirements.txt must NOT be passed to flake8."""
        (tmp_path / "requirements.txt").write_text("flask\n", encoding="utf-8")
        with patch("evaluator.runner.run_with_progress") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            passed, msg = evaluator._run_lint(["requirements.txt"], tmp_path)
            assert passed
            assert "no targets" in msg.lower()

    def test_md_file_excluded(self, evaluator, tmp_path):
        """README.md must NOT be passed to flake8."""
        (tmp_path / "README.md").write_text("# Hello\n", encoding="utf-8")
        with patch("evaluator.runner.run_with_progress") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            passed, msg = evaluator._run_lint(["README.md"], tmp_path)
            assert passed
            assert "no targets" in msg.lower()

    def test_only_py_files_linted(self, evaluator, tmp_path):
        """Mixed file list: only .py files should be resolved."""
        (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")
        (tmp_path / "config.yaml").write_text("key: value\n", encoding="utf-8")
        (tmp_path / "data.json").write_text("{}\n", encoding="utf-8")
        (tmp_path / "Makefile").write_text("all:\n\techo hi\n", encoding="utf-8")
        with patch("evaluator.runner.run_with_progress") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            evaluator._run_lint(
                ["main.py", "config.yaml", "data.json", "Makefile"],
                tmp_path,
            )
            for call_args in mock_run.call_args_list:
                cmd = call_args[0][0]
                resolved_files = [a for a in cmd if str(tmp_path) in a]
                for f in resolved_files:
                    assert f.endswith(".py"), f"Non-.py file passed to linter: {f}"
