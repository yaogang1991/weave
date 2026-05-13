"""
Tests for #206: auto-format detection (dry-run) before evaluation.

Covers:
- autopep8 --diff dry-run detects formatting issues without modifying files
- Silently skipped when autopep8 not installed
- Only targets common whitespace/blank-line rules
"""
import pytest
from unittest.mock import MagicMock, call, patch

from core.models import CriterionType, SuccessCriterion
from evaluator.engine import EvaluatorEngine


@pytest.fixture
def tmp_store(tmp_path):
    from session.store import SessionStore
    return SessionStore(base_path=str(tmp_path / "events"))


@pytest.fixture
def evaluator(tmp_store):
    return EvaluatorEngine(tmp_store)


class TestAutoFormat:
    @patch("evaluator.engine.subprocess.run")
    def test_autopep8_dry_run_before_flake8(self, mock_run, evaluator, tmp_path):
        """autopep8 --diff dry-run is called first, then autoflake --check, then flake8."""
        (tmp_path / "code.py").write_text("x = 1\n", encoding="utf-8")
        mock_run.return_value = MagicMock(returncode=0, stdout="")

        evaluator._run_lint(["code.py"], tmp_path)

        # First call = autopep8 --diff (dry-run, no file modification)
        calls = mock_run.call_args_list
        assert len(calls) >= 3
        autopep8_cmd = calls[0][0][0]
        assert "autopep8" in autopep8_cmd[2]
        assert "--diff" in autopep8_cmd
        assert "--in-place" not in autopep8_cmd

    @patch("evaluator.engine.subprocess.run")
    def test_autopep8_select_rules(self, mock_run, evaluator, tmp_path):
        """autopep8 only targets safe formatting rules."""
        (tmp_path / "code.py").write_text("x = 1\n", encoding="utf-8")
        mock_run.return_value = MagicMock(returncode=0, stdout="")

        evaluator._run_lint(["code.py"], tmp_path)

        autopep8_cmd = mock_run.call_args_list[0][0][0]
        select_arg = next((a for a in autopep8_cmd if a.startswith("--select=")), None)
        assert select_arg is not None
        rules = select_arg
        assert "E203" in rules  # whitespace before ':'
        assert "E303" in rules  # too many blank lines
        assert "W291" in rules  # trailing whitespace

    @patch("evaluator.engine.subprocess.run")
    def test_autopep8_not_found_graceful(self, mock_run, evaluator, tmp_path):
        """When autopep8 is not installed, lint still runs."""
        (tmp_path / "code.py").write_text("x = 1\n", encoding="utf-8")
        mock_run.side_effect = [
            FileNotFoundError("autopep8 not found"),  # autopep8
            FileNotFoundError("autoflake not found"),  # autoflake dry-run
            MagicMock(returncode=0, stdout=""),  # flake8
        ]

        passed, msg = evaluator._run_lint(["code.py"], tmp_path)
        assert passed  # flake8 clean → pass

    @patch("evaluator.engine.subprocess.run")
    def test_autopep8_timeout_graceful(self, mock_run, evaluator, tmp_path):
        """When autopep8 times out, lint still runs."""
        import subprocess as sp
        (tmp_path / "code.py").write_text("x = 1\n", encoding="utf-8")
        mock_run.side_effect = [
            sp.TimeoutExpired("autopep8", 30),  # autopep8 timeout
            MagicMock(returncode=0, stdout=""),  # autoflake
            MagicMock(returncode=0, stdout=""),  # flake8
        ]

        passed, msg = evaluator._run_lint(["code.py"], tmp_path)
        assert passed

    @patch("evaluator.engine.subprocess.run")
    def test_no_files_skips_autopep8(self, mock_run, evaluator, tmp_path):
        """When no targets, autopep8 is never called."""
        passed, msg = evaluator._run_lint(["missing.py"], tmp_path)
        assert passed
        mock_run.assert_not_called()
