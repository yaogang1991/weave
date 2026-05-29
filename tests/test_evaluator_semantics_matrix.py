"""
Semantic matrix tests for evaluator criterion semantics (#269).

Validates the documented behavior in docs/evaluator_criterion_semantics.md:
- Each criterion type: PASS / FAIL / WARN outcomes
- Hard vs soft classification
- Threshold interaction (hard veto, soft downgrade)
- Artifact verification (phantom file detection)
- False pass / false fail prevention scenarios

This is the normative test suite — any behavior change that breaks these
tests likely indicates a regression in evaluator semantics.
"""

import pytest
from unittest.mock import patch
from pathlib import Path

from core.models import CriterionType, EvalStatus, SuccessCriterion
from core.subprocess_runner import SubprocessResult
from evaluator.engine import EvaluatorEngine


@pytest.fixture
def tmp_store(tmp_path):
    from session.store import SessionStore
    return SessionStore(base_path=str(tmp_path / "events"))


@pytest.fixture
def evaluator(tmp_store):
    """Strict mode evaluator (no threshold)."""
    return EvaluatorEngine(tmp_store)


def _file(path: Path, content: str | None = None) -> Path:
    """Helper: create a file with content.

    Default content is valid Python to pass the import smoke test (#344).
    """
    if content is None:
        content = "# auto-generated\npass\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# =====================================================================
# FILE_EXISTS (Hard)
# =====================================================================

class TestFileExistsSemantics:
    """FILE_EXISTS: required files must exist on disk."""

    def test_pass_all_files_exist(self, evaluator, tmp_path):
        _file(tmp_path / "a.py")
        _file(tmp_path / "b.py")
        r = evaluator.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.FILE_EXISTS, path="a.py,b.py",
                             description="files"),
        ], str(tmp_path))
        assert r.passed
        assert r.score == 10.0

    def test_fail_missing_file(self, evaluator, tmp_path):
        _file(tmp_path / "a.py")
        r = evaluator.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.FILE_EXISTS, path="a.py,missing.py",
                             description="files"),
        ], str(tmp_path))
        assert not r.passed
        assert "FAIL" in r.feedback
        assert "missing" in r.feedback.lower()

    def test_pass_vacuous_no_candidates(self, evaluator, tmp_path):
        """No path specified, no output_artifacts → PASS (vacuously true)."""
        r = evaluator.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.FILE_EXISTS, description="files"),
        ], str(tmp_path))
        assert r.passed

    def test_pass_loose_glob_fallback(self, evaluator, tmp_path):
        """File not at exact path but found by stem glob."""
        _file(tmp_path / "deep" / "my_module.py")
        r = evaluator.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.FILE_EXISTS, path="my_module.py",
                             description="module"),
        ], str(tmp_path))
        assert r.passed

    def test_hard_cannot_be_overridden_by_threshold(self, tmp_store, tmp_path):
        """FILE_EXISTS failure vetoes threshold override."""
        _file(tmp_path / "a.py")
        ev = EvaluatorEngine(tmp_store, pass_threshold=3.0)
        r = ev.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.FILE_EXISTS, path="a.py",
                             description="file a"),
            SuccessCriterion(type=CriterionType.FILE_EXISTS, path="missing.py",
                             description="file b"),
        ], str(tmp_path))
        assert not r.passed  # hard criterion veto


# =====================================================================
# FILE_PATTERN (Hard)
# =====================================================================

class TestFilePatternSemantics:
    """FILE_PATTERN: files matching glob pattern must exist."""

    def test_pass_pattern_matched(self, evaluator, tmp_path):
        _file(tmp_path / "src" / "app.py")
        r = evaluator.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.FILE_PATTERN, pattern="src/*.py",
                             description="source files"),
        ], str(tmp_path))
        assert r.passed

    def test_fail_no_match(self, evaluator, tmp_path):
        r = evaluator.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.FILE_PATTERN, pattern="nonexistent/*.py",
                             description="source files"),
        ], str(tmp_path))
        assert not r.passed

    def test_pass_recursive_fallback_nested(self, evaluator, tmp_path):
        """Single-level pattern finds nested files via recursive fallback (#253)."""
        _file(tmp_path / "lib" / "core" / "validator.py")
        r = evaluator.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.FILE_PATTERN, pattern="lib/*.py",
                             description="lib files"),
        ], str(tmp_path))
        assert r.passed

    def test_pass_no_pattern_skipped(self, evaluator, tmp_path):
        """No pattern specified → PASS (skipped)."""
        r = evaluator.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.FILE_PATTERN, description="files"),
        ], str(tmp_path))
        assert r.passed


# =====================================================================
# TESTS_PASS (Hard)
# =====================================================================

class TestTestsPassSemantics:
    """TESTS_PASS: pytest execution against scoped test files."""

    @patch("evaluator.runner.run_with_progress")
    def test_pass_all_tests_pass(self, mock_run, evaluator, tmp_path):
        mock_run.return_value = SubprocessResult(returncode=0, stdout="2 passed", stderr="")
        r = evaluator.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.TESTS_PASS, description="tests"),
        ], str(tmp_path))
        assert r.passed

    @patch("evaluator.runner.run_with_progress")
    def test_fail_tests_fail(self, mock_run, evaluator, tmp_path):
        mock_run.return_value = SubprocessResult(returncode=1,
                                          stdout="FAILED test_x - AssertionError", stderr="")
        r = evaluator.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.TESTS_PASS,
                             test_path=str(tmp_path / "test_x.py"),
                             description="tests"),
        ], str(tmp_path))
        assert not r.passed

    def test_pass_no_test_files_warns(self, evaluator, tmp_path):
        """No test files found → PASS with WARN (uncheckable)."""
        _file(tmp_path / "app.py")
        r = evaluator.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.TESTS_PASS, description="tests"),
        ], str(tmp_path), output_artifacts=["app.py"])
        assert r.passed
        assert "WARN" in r.feedback

    @patch("evaluator.runner.run_with_progress")
    def test_timeout_actionable_feedback(self, mock_run, evaluator, tmp_path):
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="pytest", timeout=60)
        r = evaluator.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.TESTS_PASS,
                             test_path=str(tmp_path / "test_x.py"),
                             description="tests"),
        ], str(tmp_path))
        assert not r.passed
        assert "timed out" in r.feedback.lower()


# =====================================================================
# PATTERN_PRESENT / PATTERN_ABSENT (Hard)
# =====================================================================

class TestPatternSemantics:
    """PATTERN_PRESENT / PATTERN_ABSENT: regex pattern checks in files."""

    def test_present_pass(self, evaluator, tmp_path):
        _file(tmp_path / "fix.py", "def fixed_function():\n    pass\n")
        r = evaluator.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.PATTERN_PRESENT,
                             path="fix.py", pattern="fixed_function",
                             description="fix present"),
        ], str(tmp_path))
        assert r.passed

    def test_present_fail_not_found(self, evaluator, tmp_path):
        _file(tmp_path / "fix.py", "no match here\n")
        r = evaluator.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.PATTERN_PRESENT,
                             path="fix.py", pattern="expected_pattern",
                             description="fix present"),
        ], str(tmp_path))
        assert not r.passed

    def test_present_fail_file_missing(self, evaluator, tmp_path):
        r = evaluator.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.PATTERN_PRESENT,
                             path="nonexistent.py", pattern="x",
                             description="fix present"),
        ], str(tmp_path))
        assert not r.passed

    def test_absent_pass(self, evaluator, tmp_path):
        _file(tmp_path / "bug.py", "def clean_code():\n    pass\n")
        r = evaluator.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.PATTERN_ABSENT,
                             path="bug.py", pattern="buggy_pattern",
                             description="bug removed"),
        ], str(tmp_path))
        assert r.passed

    def test_absent_fail_still_present(self, evaluator, tmp_path):
        _file(tmp_path / "bug.py", "buggy_pattern = True\n")
        r = evaluator.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.PATTERN_ABSENT,
                             path="bug.py", pattern="buggy_pattern",
                             description="bug removed"),
        ], str(tmp_path))
        assert not r.passed

    def test_absent_pass_file_missing(self, evaluator, tmp_path):
        """File doesn't exist → trivially absent → PASS."""
        r = evaluator.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.PATTERN_ABSENT,
                             path="nonexistent.py", pattern="x",
                             description="bug removed"),
        ], str(tmp_path))
        assert r.passed

    def test_no_path_pattern_skipped(self, evaluator, tmp_path):
        """No path or pattern → PASS (skipped)."""
        r = evaluator.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.PATTERN_PRESENT,
                             description="pattern"),
        ], str(tmp_path))
        assert r.passed


# =====================================================================
# TEST_FILE_EXISTS (Hard)
# =====================================================================

class TestTestFileExistsSemantics:
    """TEST_FILE_EXISTS: agent must produce test files in output_artifacts."""

    def test_pass_test_files_produced(self, evaluator, tmp_path):
        _file(tmp_path / "app.py")
        _file(tmp_path / "test_app.py")
        r = evaluator.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.TEST_FILE_EXISTS,
                             description="has tests"),
        ], str(tmp_path), output_artifacts=["app.py", "test_app.py"])
        assert r.passed

    def test_fail_no_test_files(self, evaluator, tmp_path):
        r = evaluator.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.TEST_FILE_EXISTS,
                             description="has tests"),
        ], str(tmp_path), output_artifacts=["app.py", "utils.py"])
        assert not r.passed

    def test_fail_no_artifacts(self, evaluator, tmp_path):
        r = evaluator.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.TEST_FILE_EXISTS,
                             description="has tests"),
        ], str(tmp_path))
        assert not r.passed

    def test_pass_test_in_tests_dir(self, evaluator, tmp_path):
        _file(tmp_path / "tests" / "test_app.py")
        r = evaluator.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.TEST_FILE_EXISTS,
                             description="has tests"),
        ], str(tmp_path), output_artifacts=["tests/test_app.py"])
        assert r.passed


# =====================================================================
# COVERAGE (Soft)
# =====================================================================

class TestCoverageSemantics:
    """COVERAGE: test coverage percentage target check."""

    @patch("evaluator.runner.run_with_progress")
    def test_pass_above_target(self, mock_run, evaluator, tmp_path):
        _file(tmp_path / "test_app.py")
        _file(tmp_path / "app.py")
        mock_run.return_value = SubprocessResult(
            returncode=0,
            stdout="test_app.py .\nTOTAL   10   1   90%\n",
            stderr="",
        )
        r = evaluator.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.COVERAGE, target=80,
                             description="coverage"),
        ], str(tmp_path), output_artifacts=["app.py"])
        assert r.passed

    @patch("evaluator.runner.run_with_progress")
    def test_fail_below_target(self, mock_run, evaluator, tmp_path):
        mock_run.return_value = SubprocessResult(
            returncode=0,
            stdout="TOTAL   10   8   20%\n",
            stderr="",
        )
        r = evaluator.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.COVERAGE, target=80,
                             description="coverage"),
        ], str(tmp_path), output_artifacts=["app.py", "test_app.py"])
        assert not r.passed

    def test_fail_no_test_files(self, evaluator, tmp_path):
        """No scoped test files → cannot verify coverage → FAIL."""
        r = evaluator.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.COVERAGE, target=80,
                             description="coverage"),
        ], str(tmp_path), output_artifacts=["app.py"])
        assert not r.passed


# =====================================================================
# LINT (Soft)
# =====================================================================

class TestLintSemantics:
    """LINT: no new lint issues on changed lines."""

    @patch("evaluator.runner.run_with_progress")
    def test_pass_clean(self, mock_run, evaluator, tmp_path):
        _file(tmp_path / "app.py", "x = 1\n")
        mock_run.return_value = SubprocessResult(returncode=0, stdout="", stderr="")
        r = evaluator.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.LINT, description="lint"),
        ], str(tmp_path), output_artifacts=["app.py"])
        assert r.passed

    def test_pass_no_files_to_lint(self, evaluator, tmp_path):
        r = evaluator.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.LINT, description="lint"),
        ], str(tmp_path), output_artifacts=[])
        assert r.passed

    @patch("evaluator.runner.run_with_progress")
    def test_warn_no_linter_available(self, mock_run, evaluator, tmp_path):
        """No flake8/ruff → WARN (not hard FAIL)."""
        _file(tmp_path / "app.py")
        import subprocess  # noqa: F401

        def _mock_run(cmd, **kwargs):
            # Import smoke test calls should succeed
            if cmd[1:3] == ["-c", "import app"]:
                return SubprocessResult(returncode=0, stdout="", stderr="")
            raise FileNotFoundError("no flake8")

        mock_run.side_effect = _mock_run
        r = evaluator.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.LINT, description="lint"),
        ], str(tmp_path), output_artifacts=["app.py"])
        assert r.passed
        assert "WARN" in r.feedback


# =====================================================================
# NO_CRITICAL (Soft)
# =====================================================================

class TestNoCriticalSemantics:
    """NO_CRITICAL: no TODO/FIXME/HACK markers."""

    def test_pass_clean(self, evaluator, tmp_path):
        _file(tmp_path / "app.py", "x = 1\n")
        r = evaluator.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.NO_CRITICAL,
                             description="no markers"),
        ], str(tmp_path), output_artifacts=["app.py"])
        assert r.passed

    def test_fail_has_markers(self, evaluator, tmp_path):
        _file(tmp_path / "app.py", "# TODO: fix this\n# FIXME: bad\n")
        r = evaluator.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.NO_CRITICAL,
                             description="no markers"),
        ], str(tmp_path), output_artifacts=["app.py"])
        assert not r.passed

    def test_pass_no_artifacts(self, evaluator, tmp_path):
        r = evaluator.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.NO_CRITICAL,
                             description="no markers"),
        ], str(tmp_path))
        assert r.passed

    def test_soft_can_be_overridden_by_threshold(self, tmp_store, tmp_path):
        """NO_CRITICAL failure can be overridden by pass_threshold."""
        _file(tmp_path / "a.py", "# TODO: fix\n")
        _file(tmp_path / "b.py", "ok\n")
        _file(tmp_path / "c.py", "ok\n")
        ev = EvaluatorEngine(tmp_store, pass_threshold=5.0)
        r = ev.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.FILE_EXISTS, path="a.py",
                             description="a"),
            SuccessCriterion(type=CriterionType.FILE_EXISTS, path="b.py",
                             description="b"),
            SuccessCriterion(type=CriterionType.FILE_EXISTS, path="c.py",
                             description="c"),
            SuccessCriterion(type=CriterionType.NO_CRITICAL,
                             description="markers"),
        ], str(tmp_path), output_artifacts=["a.py"])
        # score = 7.5, threshold = 5.0, NO_CRITICAL is soft → PASS
        assert r.passed
        assert "WARN" in r.feedback


# =====================================================================
# FILE_CHANGED (Soft)
# =====================================================================

class TestFileChangedSemantics:
    """FILE_CHANGED: agent modified specified files."""

    def test_pass_files_in_artifacts(self, evaluator, tmp_path):
        _file(tmp_path / "app.py")
        r = evaluator.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.FILE_CHANGED, path="app.py",
                             description="changed"),
        ], str(tmp_path), output_artifacts=["app.py"])
        assert r.passed

    def test_fail_file_not_in_artifacts(self, evaluator, tmp_path):
        r = evaluator.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.FILE_CHANGED, path="app.py",
                             description="changed"),
        ], str(tmp_path), output_artifacts=["other.py"])
        assert not r.passed

    def test_pass_no_path_with_artifacts(self, evaluator, tmp_path):
        _file(tmp_path / "app.py")
        r = evaluator.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.FILE_CHANGED,
                             description="changed"),
        ], str(tmp_path), output_artifacts=["app.py"])
        assert r.passed


# =====================================================================
# CUSTOM (Soft, not auto-checkable)
# =====================================================================

class TestCustomSemantics:
    """CUSTOM: always passes, manual review recommended."""

    def test_pass_with_warn(self, evaluator, tmp_path):
        r = evaluator.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.CUSTOM,
                             description="code must be beautiful"),
        ], str(tmp_path))
        assert r.passed
        assert "WARN" in r.feedback

    def test_custom_is_soft(self, tmp_store, tmp_path):
        """CUSTOM doesn't block threshold override."""
        ev = EvaluatorEngine(tmp_store, pass_threshold=3.0)
        r = ev.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.CUSTOM,
                             description="review needed"),
        ], str(tmp_path))
        assert r.passed


# =====================================================================
# Artifact Verification (Mandatory)
# =====================================================================

class TestArtifactVerification:
    """Phantom artifact detection: reported files must exist on disk."""

    def test_phantom_artifact_forces_fail(self, evaluator, tmp_path):
        _file(tmp_path / "real.py")
        r = evaluator.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.FILE_EXISTS, path="real.py,phantom.py",
                             description="real"),
        ], str(tmp_path), output_artifacts=["real.py", "phantom.py"])
        assert not r.passed
        assert "artifact" in r.feedback.lower()
        assert r.score == 0.0

    def test_all_real_artifacts_pass(self, evaluator, tmp_path):
        _file(tmp_path / "real.py")
        r = evaluator.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.FILE_EXISTS, path="real.py",
                             description="real"),
        ], str(tmp_path), output_artifacts=["real.py"])
        assert r.passed


# =====================================================================
# Threshold + Hard Criteria Interaction
# =====================================================================

class TestThresholdHardInteraction:
    """Verify threshold mechanism respects hard criteria vetoes."""

    def test_hard_veto_overrides_threshold(self, tmp_store, tmp_path):
        """FILE_EXISTS fails → overall FAIL even with score > threshold."""
        for f in ["a.py", "b.py", "c.py"]:
            _file(tmp_path / f)
        ev = EvaluatorEngine(tmp_store, pass_threshold=3.0)
        r = ev.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.FILE_EXISTS, path="a.py",
                             description="a"),
            SuccessCriterion(type=CriterionType.FILE_EXISTS, path="b.py",
                             description="b"),
            SuccessCriterion(type=CriterionType.FILE_EXISTS, path="c.py",
                             description="c"),
            SuccessCriterion(type=CriterionType.FILE_EXISTS, path="missing.py",
                             description="d"),
        ], str(tmp_path))
        assert not r.passed

    def test_soft_downgrade_with_threshold(self, tmp_store, tmp_path):
        """Soft criteria failure → WARN when score meets threshold."""
        _file(tmp_path / "a.py", "# TODO: fix\n")
        _file(tmp_path / "b.py", "ok\n")
        _file(tmp_path / "c.py", "ok\n")
        ev = EvaluatorEngine(tmp_store, pass_threshold=5.0)
        r = ev.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.FILE_EXISTS, path="a.py",
                             description="a"),
            SuccessCriterion(type=CriterionType.FILE_EXISTS, path="b.py",
                             description="b"),
            SuccessCriterion(type=CriterionType.FILE_EXISTS, path="c.py",
                             description="c"),
            SuccessCriterion(type=CriterionType.NO_CRITICAL,
                             description="markers"),
        ], str(tmp_path), output_artifacts=["a.py"])
        assert r.passed
        assert "WARN" in r.feedback
        assert "FAIL" not in r.feedback

    def test_strict_mode_all_must_pass(self, tmp_store, tmp_path):
        """Without threshold, all criteria must pass."""
        _file(tmp_path / "a.py")
        ev = EvaluatorEngine(tmp_store)
        r = ev.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.FILE_EXISTS, path="a.py",
                             description="a"),
            SuccessCriterion(type=CriterionType.NO_CRITICAL,
                             description="markers"),
        ], str(tmp_path), output_artifacts=["a.py"])
        assert r.passed  # both pass


# =====================================================================
# False Pass / False Fail Prevention
# =====================================================================

class TestFalsePassPrevention:
    """Regression tests for known false pass scenarios."""

    def test_no_tests_no_false_pass_with_test_file_exists(self, evaluator, tmp_path):
        """TEST_FILE_EXISTS prevents fake pass when no tests created."""
        r = evaluator.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.TEST_FILE_EXISTS,
                             description="has tests"),
        ], str(tmp_path), output_artifacts=["app.py"])
        assert not r.passed

    def test_phantom_files_no_false_pass(self, evaluator, tmp_path):
        """Artifact verification catches phantom files."""
        r = evaluator.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.FILE_EXISTS,
                             description="files"),
        ], str(tmp_path), output_artifacts=["ghost.py"])
        assert not r.passed

    def test_no_scoped_tests_coverage_no_false_pass(self, evaluator, tmp_path):
        """Coverage fails when no scoped test files available."""
        r = evaluator.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.COVERAGE, target=80,
                             description="coverage"),
        ], str(tmp_path), output_artifacts=["app.py"])
        assert not r.passed


class TestFalseFailPrevention:
    """Regression tests for known false fail scenarios."""

    def test_loose_glob_prevents_false_fail(self, evaluator, tmp_path):
        """FILE_EXISTS uses glob fallback to find files in subdirectories."""
        _file(tmp_path / "src" / "module.py")
        r = evaluator.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.FILE_EXISTS, path="module.py",
                             description="module"),
        ], str(tmp_path))
        assert r.passed

    def test_recursive_glob_prevents_nested_false_fail(self, evaluator, tmp_path):
        """FILE_PATTERN recursive fallback for nested files (#253)."""
        _file(tmp_path / "lib" / "core" / "deep.py")
        r = evaluator.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.FILE_PATTERN, pattern="lib/*.py",
                             description="lib"),
        ], str(tmp_path))
        assert r.passed

    def test_no_linter_is_warn_not_fail(self, tmp_path):
        """Missing linter → WARN, not hard FAIL (#200)."""
        _file(tmp_path / "app.py")
        from session.store import SessionStore
        store = SessionStore(base_path=str(tmp_path / "events"))
        ev = EvaluatorEngine(store)

        def _mock_run(cmd, **kwargs):
            # Import smoke test calls should succeed
            if len(cmd) >= 3 and cmd[1] == "-c" and "import" in cmd[2]:
                return SubprocessResult(returncode=0, stdout="", stderr="")
            raise FileNotFoundError("no flake8")

        with patch("evaluator.runner.run_with_progress", side_effect=_mock_run):
            r = ev.evaluate_stage("s1", "impl", [
                SuccessCriterion(type=CriterionType.LINT, description="lint"),
            ], str(tmp_path), output_artifacts=["app.py"])
            assert r.passed


# =====================================================================
# Evaluation Status (Threshold-Assisted Pass vs Clean Pass)
# =====================================================================

class TestEvaluationStatus:
    """Verify eval_status correctly distinguishes pass quality (#276)."""

    def test_clean_pass_all_criteria_passed(self, evaluator, tmp_path):
        """All criteria pass → CLEAN_PASS."""
        _file(tmp_path / "app.py")
        r = evaluator.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.FILE_EXISTS, path="app.py",
                             description="file"),
        ], str(tmp_path), output_artifacts=["app.py"])
        assert r.passed
        assert r.eval_status == EvalStatus.CLEAN_PASS

    def test_partial_pass_threshold_override(self, tmp_store, tmp_path):
        """Threshold-assisted pass → PARTIAL_PASS, not CLEAN_PASS."""
        _file(tmp_path / "a.py", "# TODO: fix\n")
        _file(tmp_path / "b.py", "ok\n")
        _file(tmp_path / "c.py", "ok\n")
        ev = EvaluatorEngine(tmp_store, pass_threshold=5.0)
        r = ev.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.FILE_EXISTS, path="a.py",
                             description="a"),
            SuccessCriterion(type=CriterionType.FILE_EXISTS, path="b.py",
                             description="b"),
            SuccessCriterion(type=CriterionType.FILE_EXISTS, path="c.py",
                             description="c"),
            SuccessCriterion(type=CriterionType.NO_CRITICAL,
                             description="markers"),
        ], str(tmp_path), output_artifacts=["a.py"])
        assert r.passed
        assert r.eval_status == EvalStatus.PARTIAL_PASS
        assert "WARN" in r.feedback

    def test_partial_pass_has_warnings_metadata(self, tmp_store, tmp_path):
        """Threshold-assisted pass: criteria_results show soft failures.

        Downstream evaluator (DAG engine handoff) uses criteria_results to
        detect has_warnings = not all(criteria_results.values()).
        """
        _file(tmp_path / "a.py", "# TODO: fix\n")
        _file(tmp_path / "b.py", "ok\n")
        ev = EvaluatorEngine(tmp_store, pass_threshold=3.0)
        r = ev.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.FILE_EXISTS, path="a.py",
                             description="a"),
            SuccessCriterion(type=CriterionType.FILE_EXISTS, path="b.py",
                             description="b"),
            SuccessCriterion(type=CriterionType.NO_CRITICAL,
                             description="markers"),
        ], str(tmp_path), output_artifacts=["a.py"])
        assert r.passed
        assert r.eval_status == EvalStatus.PARTIAL_PASS
        # criteria_results contains the soft failure → has_warnings = True
        assert not all(r.criteria_results.values())

    def test_failed_status(self, evaluator, tmp_path):
        """Hard criterion failure → FAILED."""
        r = evaluator.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.FILE_EXISTS, path="missing.py",
                             description="missing"),
        ], str(tmp_path))
        assert not r.passed
        assert r.eval_status == EvalStatus.FAILED

    def test_warned_status_uncheckable(self, evaluator, tmp_path):
        """All checked criteria pass but uncheckable criterion → WARNED."""
        _file(tmp_path / "app.py")
        r = evaluator.evaluate_stage("s1", "impl", [
            SuccessCriterion(type=CriterionType.CUSTOM,
                             description="manual review"),
        ], str(tmp_path))
        assert r.passed
        assert r.eval_status == EvalStatus.WARNED
