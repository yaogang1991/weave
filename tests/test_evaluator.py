"""
Tests for evaluator/engine.py — criterion checking, scoring, evaluation flow.
"""
import json
import sys
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
            assert args[0] == sys.executable
            assert args[1] == "-m"
            assert args[2] == "pytest"

    @patch("evaluator.engine.subprocess.run")
    def test_lint_clean(self, mock_run, evaluator, tmp_path):
        (tmp_path / "code.py").write_text("x = 1\n", encoding="utf-8")
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        passed, msg = evaluator._run_lint(["code.py"], tmp_path)
        assert passed

    @patch("evaluator.engine.subprocess.run")
    def test_lint_dirty(self, mock_run, evaluator, tmp_path):
        (tmp_path / "code.py").write_text("x = 1\n", encoding="utf-8")
        mock_run.return_value = MagicMock(returncode=1, stdout="E501 line too long")
        passed, msg = evaluator._run_lint(["code.py"], tmp_path)
        assert not passed
        assert "issues" in msg.lower()

    def test_lint_uses_shell_false(self, evaluator, tmp_path):
        """Verify _run_lint never uses shell=True."""
        (tmp_path / "code.py").write_text("x = 1\n", encoding="utf-8")
        with patch("evaluator.engine.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            evaluator._run_lint(["code.py"], tmp_path)
            args = mock_run.call_args[0][0]
            assert isinstance(args, list)
            assert args[0] == sys.executable

    def test_lint_autoflake_only_targets_resolved_files(self, evaluator, tmp_path):
        """autoflake must only be called with resolved target paths, not
        recursively scanning directories."""
        (tmp_path / "a.py").write_text("import os\n", encoding="utf-8")
        (tmp_path / "b.py").write_text("import sys\n", encoding="utf-8")
        with patch("evaluator.engine.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            evaluator._run_lint(["a.py", "b.py"], tmp_path)
            calls = mock_run.call_args_list
            # Call order: autopep8, autoflake, flake8
            assert len(calls) >= 3
            autoflake_cmd = calls[1][0][0]
            assert autoflake_cmd[0:3] == [sys.executable, "-m", "autoflake"]
            # autoflake args must contain resolved absolute paths
            for resolved in [str(tmp_path / "a.py"), str(tmp_path / "b.py")]:
                assert resolved in autoflake_cmd

    def test_lint_continues_without_autoflake(self, evaluator, tmp_path):
        """If autoflake is not installed (FileNotFoundError), flake8 still runs."""
        (tmp_path / "code.py").write_text("x = 1\n", encoding="utf-8")
        with patch("evaluator.engine.subprocess.run") as mock_run:
            # autopep8 → not found, autoflake → not found, flake8 → success
            mock_run.side_effect = [
                FileNotFoundError("autopep8 not found"),
                FileNotFoundError("autoflake not found"),
                MagicMock(returncode=0, stdout=""),
            ]
            passed, msg = evaluator._run_lint(["code.py"], tmp_path)
            assert passed
            assert mock_run.call_count == 3

    def test_lint_autoflake_error_still_runs_flake8(self, evaluator, tmp_path):
        """If autoflake raises an unexpected error, flake8 still runs."""
        (tmp_path / "code.py").write_text("x = 1\n", encoding="utf-8")
        with patch("evaluator.engine.subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=""),  # autopep8
                RuntimeError("autoflake crashed"),
                MagicMock(returncode=0, stdout=""),
            ]
            passed, msg = evaluator._run_lint(["code.py"], tmp_path)
            assert passed
            assert mock_run.call_count == 3

    def test_lint_autoflake_then_flake8_on_same_targets(self, evaluator, tmp_path):
        """Autoflake dry-run and flake8 verify the same resolved files."""
        (tmp_path / "code.py").write_text("import os\n", encoding="utf-8")
        with patch("evaluator.engine.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            evaluator._run_lint(["code.py"], tmp_path)
            calls = mock_run.call_args_list
            # Call order: autopep8[0], autoflake[1], flake8[2]
            autoflake_cmd = calls[1][0][0]
            # Ensure dry-run mode: has --check, no --in-place
            assert "--check" in autoflake_cmd
            assert "--in-place" not in autoflake_cmd
            flake8_cmd = calls[2][0][0]
            # autoflake: python -m autoflake --flags... <files>
            autoflake_files = [a for a in autoflake_cmd[7:] if not a.startswith("-")]
            # flake8: python -m flake8 <files> --flags...
            flake8_files = [a for a in flake8_cmd[4:] if not a.startswith("-")]
            assert autoflake_files == flake8_files

    def test_check_files_missing(self, evaluator, tmp_path):
        passed, msg = evaluator._check_files_exist(["missing.py"], tmp_path)
        assert not passed
        assert "missing" in msg.lower()

    def test_check_files_present(self, evaluator, tmp_path):
        (tmp_path / "exists.py").write_text("ok", encoding="utf-8")
        passed, msg = evaluator._check_files_exist(["exists.py"], tmp_path)
        assert passed

    def test_loose_match_by_name(self, evaluator, tmp_path):
        """File in subdirectory matches by name."""
        sub = tmp_path / "tools"
        sub.mkdir()
        (sub / "config_parser.py").write_text("ok", encoding="utf-8")
        passed, msg = evaluator._check_files_exist_loose(["src/config_parser.py"], tmp_path)
        assert passed
        assert "loose match" in msg.lower()

    def test_loose_match_by_stem(self, evaluator, tmp_path):
        """File matches by stem substring."""
        sub = tmp_path / "tests"
        sub.mkdir()
        (sub / "test_csv_processor_utils.py").write_text("ok", encoding="utf-8")
        passed, msg = evaluator._check_files_exist_loose(["tests/test_csv_processor.py"], tmp_path)
        assert passed

    def test_loose_match_still_finds_missing(self, evaluator, tmp_path):
        passed, msg = evaluator._check_files_exist_loose(["totally_missing_file.py"], tmp_path)
        assert not passed

    def test_file_exists_verifies_on_disk(self, evaluator, tmp_path):
        """FILE_EXISTS verifies files on disk, not just output_artifacts (#158)."""
        (tmp_path / "planned.py").write_text("x = 1\n", encoding="utf-8")
        (tmp_path / "actual.py").write_text("y = 2\n", encoding="utf-8")
        result = evaluator.evaluate_stage(
            "s1", "impl",
            [SuccessCriterion(type=CriterionType.FILE_EXISTS, path="planned.py", description="file")],
            str(tmp_path),
            output_artifacts=["actual.py"],
        )
        assert result.passed

    @patch("evaluator.engine.subprocess.run")
    def test_coverage_no_total_line_fails(self, mock_run, evaluator, tmp_path):
        """If pytest returns 0 but stdout has no TOTAL line, coverage is
        unverifiable — returns auto=False so evaluate_stage emits WARN (#152)."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout="2 passed in 0.01s\n",
            stderr="",
        )
        passed, msg, auto = evaluator._check_coverage(tmp_path, 80)
        # Unverifiable coverage → WARN (auto=False), not hard FAIL
        assert not auto
        assert "not verified" in msg


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

    @patch("evaluator.engine.subprocess.run")
    def test_coverage_unparseable_emits_warn_not_pass(self, mock_run, evaluator, tmp_path):
        """Coverage parse failure should produce WARN, not PASS (#152)."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout="2 passed in 0.01s\n", stderr="",
        )
        result = evaluator.evaluate_stage(
            "s1", "impl",
            [SuccessCriterion(
                type=CriterionType.COVERAGE, target=80,
                description="coverage >= 80%",
            )],
            str(tmp_path),
            output_artifacts=["src/module.py"],
        )
        # Overall passed=True because WARN doesn't fail the stage
        assert result.passed
        # But feedback must say WARN, not PASS
        assert "WARN" in result.feedback
        assert "could not be parsed" in result.feedback
        # coverage should be in suggestions (uncheckable list)
        assert "coverage >= 80%" in result.suggestions

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
