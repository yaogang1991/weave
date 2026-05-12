"""
Tests for #178 PR 1: CriterionChecker abstraction.

Verifies pluggable checker registration, dispatch, and fallback behavior.
"""
import pytest
from pathlib import Path
from unittest.mock import MagicMock

from core.models import CriterionType, SuccessCriterion
from evaluator.engine import EvaluatorEngine
from evaluator.models import CheckResult, CheckSeverity, EvaluationContext


def _make_engine():
    mock_store = MagicMock()
    return EvaluatorEngine(session_store=mock_store)


class TestEvaluationContext:
    def test_context_creation(self):
        ctx = EvaluationContext(work_dir=Path("/tmp/work"))
        assert ctx.work_dir == Path("/tmp/work")
        assert ctx.artifacts is None
        assert ctx.node_id is None

    def test_context_with_artifacts(self):
        ctx = EvaluationContext(
            work_dir=Path("/tmp/work"),
            artifacts=["main.py", "test_main.py"],
        )
        assert ctx.artifacts == ["main.py", "test_main.py"]


class TestCheckResult:
    def test_pass_result(self):
        r = CheckResult(passed=True, message="OK")
        assert r.passed
        assert r.severity == CheckSeverity.NORMAL

    def test_fail_with_warning(self):
        r = CheckResult(
            passed=False,
            message="Cannot verify",
            severity=CheckSeverity.WARNING,
        )
        assert r.severity == CheckSeverity.WARNING

    def test_metadata(self):
        r = CheckResult(
            passed=True,
            message="Clean",
            metadata={"issues": 0},
        )
        assert r.metadata["issues"] == 0


class TestCheckerRegistration:
    def test_register_and_dispatch(self):
        engine = _make_engine()

        class FakeChecker:
            def check(self, criterion, context):
                return CheckResult(
                    passed=True,
                    message=f"Fake check for {criterion.type.value}",
                )

        engine.register_checker(CriterionType.TESTS_PASS, FakeChecker())

        passed, msg, was_auto = engine._check_criterion(
            SuccessCriterion(type=CriterionType.TESTS_PASS, description="tests"),
            "/tmp/work",
        )
        assert passed
        assert "Fake check" in msg
        assert was_auto

    def test_fallback_when_no_checker_registered(self):
        engine = _make_engine()

        # No checker registered for FILE_EXISTS — should use built-in
        passed, msg, was_auto = engine._check_criterion(
            SuccessCriterion(type=CriterionType.FILE_EXISTS, path="nonexistent.py"),
            "/tmp/work",
        )
        # Built-in should run (file doesn't exist so will fail)
        assert not passed
        assert was_auto

    def test_multiple_checkers_registered(self):
        engine = _make_engine()

        class TestsChecker:
            def check(self, criterion, context):
                return CheckResult(passed=True, message="tests ok")

        class LintChecker:
            def check(self, criterion, context):
                return CheckResult(passed=False, message="lint failed")

        engine.register_checker(CriterionType.TESTS_PASS, TestsChecker())
        engine.register_checker(CriterionType.LINT, LintChecker())

        p1, _, _ = engine._check_criterion(
            SuccessCriterion(type=CriterionType.TESTS_PASS, description="tests"),
            "/tmp/work",
        )
        p2, m2, _ = engine._check_criterion(
            SuccessCriterion(type=CriterionType.LINT, description="lint"),
            "/tmp/work",
        )
        assert p1
        assert not p2
        assert "lint failed" in m2


class TestCheckSeverityMapping:
    def test_normal_severity_is_auto(self):
        engine = _make_engine()

        class Checker:
            def check(self, criterion, context):
                return CheckResult(
                    passed=True, message="ok",
                    severity=CheckSeverity.NORMAL,
                )

        engine.register_checker(CriterionType.CUSTOM, Checker())
        _, _, was_auto = engine._check_criterion(
            SuccessCriterion(type=CriterionType.CUSTOM, description="custom"),
            "/tmp/work",
        )
        assert was_auto

    def test_warning_severity_is_not_auto(self):
        engine = _make_engine()

        class Checker:
            def check(self, criterion, context):
                return CheckResult(
                    passed=True, message="manual review",
                    severity=CheckSeverity.WARNING,
                )

        engine.register_checker(CriterionType.CUSTOM, Checker())
        _, _, was_auto = engine._check_criterion(
            SuccessCriterion(type=CriterionType.CUSTOM, description="custom"),
            "/tmp/work",
        )
        assert not was_auto

    def test_error_severity_is_auto(self):
        engine = _make_engine()

        class Checker:
            def check(self, criterion, context):
                return CheckResult(
                    passed=False, message="check error",
                    severity=CheckSeverity.ERROR,
                )

        engine.register_checker(CriterionType.CUSTOM, Checker())
        _, _, was_auto = engine._check_criterion(
            SuccessCriterion(type=CriterionType.CUSTOM, description="custom"),
            "/tmp/work",
        )
        assert was_auto


class TestExistingBehaviorPreserved:
    def test_custom_without_checker_passes_with_warning(self):
        """Built-in CUSTOM behavior: pass with was_auto=False."""
        engine = _make_engine()
        passed, msg, was_auto = engine._check_criterion(
            SuccessCriterion(type=CriterionType.CUSTOM, description="do stuff"),
            "/tmp/work",
        )
        assert passed
        assert not was_auto
        assert "Cannot auto-verify" in msg

    def test_file_exists_still_works(self, tmp_path):
        """Built-in FILE_EXISTS checker still functions."""
        (tmp_path / "app.py").write_text("print('hi')", encoding="utf-8")
        engine = _make_engine()
        passed, msg, was_auto = engine._check_criterion(
            SuccessCriterion(type=CriterionType.FILE_EXISTS, path="app.py"),
            str(tmp_path),
        )
        assert passed
        assert was_auto
