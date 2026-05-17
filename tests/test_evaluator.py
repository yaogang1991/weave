"""
Tests for evaluator/engine.py — criterion checking, scoring, evaluation flow.
"""
import json
import sys
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.models import EvaluationResult, SuccessCriterion, CriterionType
from evaluator.engine import EvaluatorEngine


@pytest.fixture
def tmp_store(tmp_path):
    from session.store import SessionStore
    return SessionStore(base_path=str(tmp_path / "events"))


@pytest.fixture
def evaluator(tmp_store):
    return EvaluatorEngine(tmp_store, auto_format_before_eval=True)


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

    @patch("evaluator.runner.subprocess.run")
    def test_tests_pass(self, mock_run, evaluator, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stdout="2 passed")
        passed, msg = evaluator._run_tests(tmp_path)
        assert passed
        assert "passed" in msg.lower()

    @patch("evaluator.runner.subprocess.run")
    def test_tests_fail(self, mock_run, evaluator, tmp_path):
        mock_run.return_value = MagicMock(returncode=1, stdout="1 failed")
        passed, msg = evaluator._run_tests(tmp_path)
        assert not passed

    def test_tests_pass_uses_shell_false(self, evaluator, tmp_path):
        """Verify _run_tests never uses shell=True."""
        with patch("evaluator.runner.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="ok")
            evaluator._run_tests(tmp_path)
            _, kwargs = mock_run.call_args
            assert kwargs.get("shell") is not True
            # Verify fixed command
            args = mock_run.call_args[0][0]
            assert args[0] == sys.executable
            assert args[1] == "-m"
            assert args[2] == "pytest"

    @patch("evaluator.runner.subprocess.run")
    def test_lint_clean(self, mock_run, evaluator, tmp_path):
        (tmp_path / "code.py").write_text("x = 1\n", encoding="utf-8")
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        passed, msg = evaluator._run_lint(["code.py"], tmp_path)
        assert passed

    @patch("evaluator.runner.subprocess.run")
    def test_lint_dirty(self, mock_run, evaluator, tmp_path):
        (tmp_path / "code.py").write_text("x = 1\n", encoding="utf-8")
        mock_run.return_value = MagicMock(returncode=1, stdout="E501 line too long")
        passed, msg = evaluator._run_lint(["code.py"], tmp_path)
        assert not passed
        assert "issues" in msg.lower()

    def test_lint_uses_shell_false(self, evaluator, tmp_path):
        """Verify _run_lint never uses shell=True."""
        (tmp_path / "code.py").write_text("x = 1\n", encoding="utf-8")
        with patch("evaluator.runner.subprocess.run") as mock_run:
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
        with patch("evaluator.runner.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            evaluator._run_lint(["a.py", "b.py"], tmp_path)
            calls = mock_run.call_args_list
            # Call order: autoflake, autopep8, flake8
            assert len(calls) >= 3
            autoflake_cmd = calls[0][0][0]
            assert autoflake_cmd[0:3] == [sys.executable, "-m", "autoflake"]
            # autoflake args must contain resolved absolute paths
            for resolved in [str(tmp_path / "a.py"), str(tmp_path / "b.py")]:
                assert resolved in autoflake_cmd

    def test_lint_continues_without_autoflake(self, evaluator, tmp_path):
        """If autoflake is not installed (FileNotFoundError), flake8 still runs."""
        (tmp_path / "code.py").write_text("x = 1\n", encoding="utf-8")
        with patch("evaluator.runner.subprocess.run") as mock_run:
            # autoflake → not found, autopep8 → not found, flake8 → success
            mock_run.side_effect = [
                FileNotFoundError("autoflake not found"),
                FileNotFoundError("autopep8 not found"),
                MagicMock(returncode=0, stdout=""),
            ]
            passed, msg = evaluator._run_lint(["code.py"], tmp_path)
            assert passed
            assert mock_run.call_count == 3

    def test_lint_autoflake_error_still_runs_flake8(self, evaluator, tmp_path):
        """If autoflake raises an unexpected error, flake8 still runs."""
        (tmp_path / "code.py").write_text("x = 1\n", encoding="utf-8")
        with patch("evaluator.runner.subprocess.run") as mock_run:
            mock_run.side_effect = [
                RuntimeError("autoflake crashed"),
                RuntimeError("autopep8 crashed"),
                MagicMock(returncode=0, stdout=""),
            ]
            passed, msg = evaluator._run_lint(["code.py"], tmp_path)
            assert passed
            assert mock_run.call_count == 3

    def test_lint_autoflake_then_flake8_on_same_targets(self, evaluator, tmp_path):
        """Autoflake in-place and flake8 verify the same resolved files (#283)."""
        (tmp_path / "code.py").write_text("import os\n", encoding="utf-8")
        with patch("evaluator.runner.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            evaluator._run_lint(["code.py"], tmp_path)
            calls = mock_run.call_args_list
            autoflake_cmd = calls[0][0][0]
            # In-place mode: has --in-place, no --check (#283)
            assert "--in-place" in autoflake_cmd
            assert "--check" not in autoflake_cmd
            autopep8_cmd = calls[1][0][0]
            assert "autopep8" in autopep8_cmd[2]
            assert "--in-place" in autopep8_cmd
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

    @patch("evaluator.runner.subprocess.run")
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
    @patch("evaluator.runner.subprocess.run")
    def test_all_pass(self, mock_run, evaluator, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stdout="OK\nTOTAL    100%")
        result = evaluator.evaluate_stage(
            "s1", "impl", ["tests pass", "lint clean"], str(tmp_path)
        )
        assert isinstance(result, EvaluationResult)
        assert result.passed
        assert result.score > 0

    @patch("evaluator.runner.subprocess.run")
    def test_coverage_unparseable_emits_warn_not_pass(self, mock_run, evaluator, tmp_path):
        """Coverage parse failure should produce WARN, not PASS (#152)."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout="2 passed in 0.01s\n", stderr="",
        )
        # Create the artifact on disk so phantom check (#234) doesn't fail
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / "module.py").write_text("x = 1\n", encoding="utf-8")
        result = evaluator.evaluate_stage(
            "s1", "impl",
            [SuccessCriterion(
                type=CriterionType.COVERAGE, target=80,
                description="coverage >= 80%",
            )],
            str(tmp_path),
            output_artifacts=["src/module.py"],
        )
        # When no scoped test files found, coverage cannot be verified.
        # _check_coverage returns (False, ..., False) — criterion fails as
        # uncheckable, so overall result depends on other criteria.
        # With only this one criterion, overall_passed = False.
        # Feedback should indicate coverage was not verified.
        assert "WARN" in result.feedback or "not verified" in result.feedback.lower() or "cannot verify" in result.feedback.lower()
        # coverage should be in suggestions (uncheckable list)
        assert "coverage >= 80%" in result.suggestions

    def test_uncheckable_criterion_passes_with_warning(self, evaluator, tmp_path):
        result = evaluator.evaluate_stage(
            "s1", "impl", ["code must be elegant"], str(tmp_path)
        )
        assert result.passed
        assert "manual review" in result.feedback.lower()

    @patch("evaluator.runner.subprocess.run")
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


class TestShadowInitDetection:
    """Tests for #221: detect shadowing __init__.py in test subdirs.

    The evaluator must NOT modify/delete workspace files. It should only
    report shadowing as diagnostic feedback so the generator can fix it.
    """

    def test_detect_shadowing_init(self, evaluator, tmp_path):
        """_detect_shadowing_test_inits reports shadowing but does not delete."""
        # Create root package
        (tmp_path / "configlib").mkdir()
        (tmp_path / "configlib" / "__init__.py").write_text("# root pkg", encoding="utf-8")
        # Create shadowing test subdir init
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "configlib").mkdir()
        shadow_file = tmp_path / "tests" / "configlib" / "__init__.py"
        shadow_file.write_text("# shadow pkg", encoding="utf-8")

        warnings = EvaluatorEngine._detect_shadowing_test_inits(tmp_path)
        assert len(warnings) == 1
        assert "tests/configlib/__init__.py shadows root configlib/ package" in warnings[0]
        # File must NOT be deleted — evaluator only reports
        assert shadow_file.exists(), "evaluator must not delete shadowing __init__.py"

    def test_no_shadowing_when_no_root_package(self, evaluator, tmp_path):
        """No warning if test subdir has __init__.py but no root package."""
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "mylib").mkdir()
        (tmp_path / "tests" / "mylib" / "__init__.py").write_text("# ok", encoding="utf-8")

        warnings = EvaluatorEngine._detect_shadowing_test_inits(tmp_path)
        assert warnings == []

    def test_no_shadowing_when_no_tests_dir(self, evaluator, tmp_path):
        """No warning if tests/ directory does not exist."""
        warnings = EvaluatorEngine._detect_shadowing_test_inits(tmp_path)
        assert warnings == []

    @patch("evaluator.runner.subprocess.run")
    def test_shadowing_warning_in_test_failure_feedback(self, mock_run, evaluator, tmp_path):
        """Shadowing diagnostic is appended to test failure feedback."""
        # Set up shadowing structure
        (tmp_path / "configlib").mkdir()
        (tmp_path / "configlib" / "__init__.py").write_text("# root", encoding="utf-8")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "configlib").mkdir()
        shadow_file = tmp_path / "tests" / "configlib" / "__init__.py"
        shadow_file.write_text("# shadow", encoding="utf-8")

        mock_run.return_value = MagicMock(returncode=1, stdout="FAILED test_something")
        passed, msg = evaluator._run_tests(tmp_path)
        assert not passed
        assert "shadow" in msg.lower()
        assert "tests/configlib" in msg
        # File must still exist — no deletion
        assert shadow_file.exists()

    @patch("evaluator.runner.subprocess.run")
    def test_shadowing_warning_in_test_pass_feedback(self, mock_run, evaluator, tmp_path):
        """Shadowing diagnostic is included even when tests pass."""
        (tmp_path / "configlib").mkdir()
        (tmp_path / "configlib" / "__init__.py").write_text("# root", encoding="utf-8")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "configlib").mkdir()
        shadow_file = tmp_path / "tests" / "configlib" / "__init__.py"
        shadow_file.write_text("# shadow", encoding="utf-8")

        mock_run.return_value = MagicMock(returncode=0, stdout="2 passed")
        passed, msg = evaluator._run_tests(tmp_path)
        assert passed
        assert "shadow" in msg.lower()
        assert "tests/configlib" in msg
        # File must still exist — no deletion
        assert shadow_file.exists()
