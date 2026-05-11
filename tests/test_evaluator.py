"""
Tests for evaluator/engine.py — criterion checking, scoring, evaluation flow.
"""
import json
import pytest
from unittest.mock import MagicMock, patch

from core.models import EvaluationResult, SuccessCriterion, CriterionType
from evaluator.engine import EvaluatorEngine


@pytest.fixture
def tmp_store(tmp_path):
    from session.store import SessionStore
    return SessionStore(base_path=str(tmp_path / "events"))


@pytest.fixture
def evaluator(tmp_store):
    return EvaluatorEngine(tmp_store)


class TestCriterionChecking:
    def test_unrecognized_criterion(self, evaluator):
        """Unrecognized criteria return (True, warning msg, False) — pass by default."""
        crit = SuccessCriterion(type=CriterionType.CUSTOM, description="code must be beautiful")
        passed, msg, auto = evaluator._check_criterion(crit, "/tmp/nonexistent")
        assert passed
        assert not auto
        assert "cannot auto-verify" in msg.lower()

    def test_extract_percentage(self, evaluator):
        assert evaluator._extract_percentage("coverage 80%") == 80
        assert evaluator._extract_percentage("no percentage here") is None
        assert evaluator._extract_percentage("need 95% coverage") == 95

    @patch("evaluator.engine.subprocess.run")
    def test_tests_pass(self, mock_run, evaluator, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stdout="2 passed")
        passed, msg = evaluator._run_tests(tmp_path)
        assert passed
        assert "passed" in msg.lower()

    @patch("evaluator.engine.subprocess.run")
    def test_tests_fail(self, mock_run, evaluator, tmp_path):
        mock_run.return_value = MagicMock(returncode=1, stdout="1 failed")
        passed, msg = evaluator._run_tests(tmp_path)
        assert not passed

    def test_tests_pass_uses_shell_false(self, evaluator, tmp_path):
        """Verify _run_tests never uses shell=True."""
        with patch("evaluator.engine.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="ok")
            evaluator._run_tests(tmp_path)
            _, kwargs = mock_run.call_args
            assert kwargs.get("shell") is not True
            # Verify fixed command
            args = mock_run.call_args[0][0]
            assert args[0] == "python"
            assert args[1] == "-m"
            assert args[2] == "pytest"

    @patch("evaluator.engine.subprocess.run")
    def test_lint_clean(self, mock_run, evaluator, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        passed, msg = evaluator._run_lint(["."], tmp_path)
        assert passed

    @patch("evaluator.engine.subprocess.run")
    def test_lint_dirty(self, mock_run, evaluator, tmp_path):
        mock_run.return_value = MagicMock(returncode=1, stdout="E501 line too long")
        passed, msg = evaluator._run_lint(["."], tmp_path)
        assert not passed
        assert "issues" in msg.lower()

    def test_lint_uses_shell_false(self, evaluator, tmp_path):
        """Verify _run_lint never uses shell=True."""
        with patch("evaluator.engine.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            evaluator._run_lint(["."], tmp_path)
            args = mock_run.call_args[0][0]
            assert isinstance(args, list)
            assert args[0] == "python"

    def test_check_files_missing(self, evaluator, tmp_path):
        passed, msg = evaluator._check_files_exist(["missing.py"], tmp_path)
        assert not passed
        assert "missing" in msg.lower()

    def test_check_files_present(self, evaluator, tmp_path):
        (tmp_path / "exists.py").write_text("ok", encoding="utf-8")
        passed, msg = evaluator._check_files_exist(["exists.py"], tmp_path)
        assert passed


class TestEvaluateStage:
    @patch("evaluator.engine.subprocess.run")
    def test_all_pass(self, mock_run, evaluator, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stdout="OK\nTOTAL    100%")
        result = evaluator.evaluate_stage(
            "s1", "impl", ["tests pass", "lint clean"], str(tmp_path)
        )
        assert isinstance(result, EvaluationResult)
        assert result.passed
        assert result.score > 0

    def test_uncheckable_criterion_passes_with_warning(self, evaluator, tmp_path):
        result = evaluator.evaluate_stage(
            "s1", "impl", ["code must be elegant"], str(tmp_path)
        )
        assert result.passed
        assert "manual review" in result.feedback.lower()

    @patch("evaluator.engine.subprocess.run")
    def test_mix_pass_and_uncheckable(self, mock_run, evaluator, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stdout="OK")
        result = evaluator.evaluate_stage(
            "s1", "impl", ["tests pass", "code must follow SOLID principles"], str(tmp_path)
        )
        assert result.passed
        assert "manual review" in result.feedback.lower()

    def test_structured_file_exists(self, evaluator, tmp_path):
        (tmp_path / "hello.py").write_text("ok", encoding="utf-8")
        crit = json.dumps({"type": "file_exists", "path": "hello.py", "description": "file"})
        result = evaluator.evaluate_stage("s1", "impl", [crit], str(tmp_path))
        assert result.passed

    def test_structured_file_missing(self, evaluator, tmp_path):
        crit = json.dumps({"type": "file_exists", "path": "nope.py", "description": "file"})
        result = evaluator.evaluate_stage("s1", "impl", [crit], str(tmp_path))
        assert not result.passed

    def test_success_criterion_object(self, evaluator, tmp_path):
        (tmp_path / "hello.py").write_text("ok", encoding="utf-8")
        sc = SuccessCriterion(type=CriterionType.FILE_EXISTS, path="hello.py", description="file")
        result = evaluator.evaluate_stage("s1", "impl", [sc], str(tmp_path))
        assert result.passed

    def test_command_type_treated_as_uncheckable(self, evaluator, tmp_path):
        """COMMAND type should be treated as CUSTOM/uncheckable, never executed."""
        # Simulate a node created with a command criterion that got downgraded to CUSTOM
        node = SuccessCriterion(type=CriterionType.CUSTOM, description="danger")
        result = evaluator.evaluate_stage("s1", "impl", [node], str(tmp_path))
        assert result.passed  # Now passes with warning instead of hard-failing
        assert "cannot auto-verify" in result.feedback.lower()
