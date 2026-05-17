"""
Evaluator contract tests: system-level quality gate verification.

Part of #178 PR 5: protect evaluator behavior from future regressions.
Tests exercise the public evaluator contract, not private helpers.
"""
from unittest.mock import MagicMock

from core.models import CriterionType, SuccessCriterion
from evaluator.engine import EvaluatorEngine
from evaluator.models import CheckResult
from evaluator.compat import normalize_criteria, parse_string_criterion


def _make_engine(**kwargs):
    mock_store = MagicMock()
    return EvaluatorEngine(session_store=mock_store, **kwargs)


# ---------------------------------------------------------------------------
# Contract: compat adapter correctly parses string criteria
# ---------------------------------------------------------------------------

class TestCompatAdapter:
    def test_tests_pass(self):
        crit = parse_string_criterion("tests pass")
        assert crit.type == CriterionType.TESTS_PASS

    def test_coverage_with_percentage(self):
        crit = parse_string_criterion("coverage >= 90%")
        assert crit.type == CriterionType.COVERAGE
        assert crit.target == 90.0

    def test_lint(self):
        crit = parse_string_criterion("lint clean")
        assert crit.type == CriterionType.LINT

    def test_file_exists_with_path(self):
        crit = parse_string_criterion("file_exists: app.py")
        assert crit.type == CriterionType.FILE_EXISTS
        assert crit.path == "app.py"

    def test_custom_fallback(self):
        crit = parse_string_criterion("something random")
        assert crit.type == CriterionType.CUSTOM

    def test_chinese_keyword(self):
        crit = parse_string_criterion("测试通过")
        assert crit.type == CriterionType.TESTS_PASS

    def test_normalize_preserves_structured(self):
        s = SuccessCriterion(type=CriterionType.LINT, description="test")
        result = normalize_criteria(["tests pass", s])
        assert len(result) == 2
        assert result[0].type == CriterionType.TESTS_PASS
        assert result[1] is s

    def test_normalize_json_string(self):
        json_str = '{"type": "tests_pass", "description": "from json"}'
        result = normalize_criteria([json_str])
        assert result[0].type == CriterionType.TESTS_PASS


# ---------------------------------------------------------------------------
# Contract: file_exists rejects missing files, accepts existing
# ---------------------------------------------------------------------------

class TestFileExistsContract:
    def test_rejects_missing_file(self, tmp_path):
        engine = _make_engine()
        passed, msg, was_auto = engine._check_criterion(
            SuccessCriterion(type=CriterionType.FILE_EXISTS, path="nonexistent.py"),
            str(tmp_path),
        )
        assert not passed
        assert was_auto
        assert "missing" in msg.lower() or "not found" in msg.lower()

    def test_accepts_existing_file(self, tmp_path):
        (tmp_path / "app.py").write_text("x = 1")
        engine = _make_engine()
        passed, msg, was_auto = engine._check_criterion(
            SuccessCriterion(type=CriterionType.FILE_EXISTS, path="app.py"),
            str(tmp_path),
        )
        assert passed
        assert was_auto


# ---------------------------------------------------------------------------
# Contract: file_pattern matches and rejects correctly
# ---------------------------------------------------------------------------

class TestFilePatternContract:
    def test_matches_existing_files(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("x = 1")
        engine = _make_engine()
        passed, msg, was_auto = engine._check_criterion(
            SuccessCriterion(type=CriterionType.FILE_PATTERN, pattern="src/*.py"),
            str(tmp_path),
        )
        assert passed

    def test_rejects_no_match(self, tmp_path):
        engine = _make_engine()
        passed, msg, was_auto = engine._check_criterion(
            SuccessCriterion(type=CriterionType.FILE_PATTERN, pattern="nonexist/*.py"),
            str(tmp_path),
        )
        assert not passed


# ---------------------------------------------------------------------------
# Contract: pattern_absent / pattern_present
# ---------------------------------------------------------------------------

class TestPatternContract:
    def test_pattern_absent_passes_when_gone(self, tmp_path):
        (tmp_path / "code.py").write_text("def good(): pass")
        engine = _make_engine()
        passed, msg, was_auto = engine._check_criterion(
            SuccessCriterion(
                type=CriterionType.PATTERN_ABSENT,
                path="code.py",
                pattern="buggy_code",
            ),
            str(tmp_path),
        )
        assert passed

    def test_pattern_absent_fails_when_present(self, tmp_path):
        (tmp_path / "code.py").write_text("buggy_code()")
        engine = _make_engine()
        passed, msg, was_auto = engine._check_criterion(
            SuccessCriterion(
                type=CriterionType.PATTERN_ABSENT,
                path="code.py",
                pattern="buggy_code",
            ),
            str(tmp_path),
        )
        assert not passed

    def test_pattern_present_passes_when_found(self, tmp_path):
        (tmp_path / "code.py").write_text("fixed_code()")
        engine = _make_engine()
        passed, msg, was_auto = engine._check_criterion(
            SuccessCriterion(
                type=CriterionType.PATTERN_PRESENT,
                path="code.py",
                pattern="fixed_code",
            ),
            str(tmp_path),
        )
        assert passed

    def test_pattern_present_fails_when_missing(self, tmp_path):
        (tmp_path / "code.py").write_text("old_code()")
        engine = _make_engine()
        passed, msg, was_auto = engine._check_criterion(
            SuccessCriterion(
                type=CriterionType.PATTERN_PRESENT,
                path="code.py",
                pattern="fixed_code",
            ),
            str(tmp_path),
        )
        assert not passed


# ---------------------------------------------------------------------------
# Contract: file_changed
# ---------------------------------------------------------------------------

class TestFileChangedContract:
    def test_passes_when_file_in_artifacts(self, tmp_path):
        engine = _make_engine()
        passed, msg, was_auto = engine._check_criterion(
            SuccessCriterion(type=CriterionType.FILE_CHANGED, path="app.py"),
            str(tmp_path),
            output_artifacts=["app.py"],
        )
        assert passed

    def test_fails_when_file_not_in_artifacts(self, tmp_path):
        engine = _make_engine()
        passed, msg, was_auto = engine._check_criterion(
            SuccessCriterion(type=CriterionType.FILE_CHANGED, path="app.py"),
            str(tmp_path),
            output_artifacts=["other.py"],
        )
        assert not passed


# ---------------------------------------------------------------------------
# Contract: test_file_exists
# ---------------------------------------------------------------------------

class TestTestFileExistsContract:
    def test_passes_with_test_files(self, tmp_path):
        engine = _make_engine()
        passed, msg, was_auto = engine._check_criterion(
            SuccessCriterion(type=CriterionType.TEST_FILE_EXISTS),
            str(tmp_path),
            output_artifacts=["app.py", "test_app.py"],
        )
        assert passed

    def test_fails_without_test_files(self, tmp_path):
        engine = _make_engine()
        passed, msg, was_auto = engine._check_criterion(
            SuccessCriterion(type=CriterionType.TEST_FILE_EXISTS),
            str(tmp_path),
            output_artifacts=["app.py"],
        )
        assert not passed


# ---------------------------------------------------------------------------
# Contract: custom criterion is uncheckable (passes with warning)
# ---------------------------------------------------------------------------

class TestCustomCriterionContract:
    def test_custom_passes_with_warning(self, tmp_path):
        engine = _make_engine()
        passed, msg, was_auto = engine._check_criterion(
            SuccessCriterion(type=CriterionType.CUSTOM, description="manual review needed"),
            str(tmp_path),
        )
        assert passed
        assert not was_auto  # Not auto-verifiable
        assert "manual" in msg.lower() or "auto-verify" in msg.lower()


# ---------------------------------------------------------------------------
# Contract: pluggable checker overrides built-in
# ---------------------------------------------------------------------------

class TestPluggableCheckerContract:
    def test_custom_checker_overrides_builtin(self, tmp_path):
        engine = _make_engine()

        class AlwaysPass:
            def check(self, criterion, context):
                return CheckResult(passed=True, message="Override!")

        engine.register_checker(CriterionType.FILE_EXISTS, AlwaysPass())
        passed, msg, was_auto = engine._check_criterion(
            SuccessCriterion(type=CriterionType.FILE_EXISTS, path="nonexistent.py"),
            str(tmp_path),
        )
        assert passed
        assert "Override" in msg

    def test_builtin_used_when_no_override(self, tmp_path):
        engine = _make_engine()
        passed, msg, was_auto = engine._check_criterion(
            SuccessCriterion(type=CriterionType.FILE_EXISTS, path="nonexistent.py"),
            str(tmp_path),
        )
        assert not passed  # Built-in correctly fails
