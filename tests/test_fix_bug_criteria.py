"""Tests for bug-fix verification criteria (issue #117).

Verifies:
1. FILE_CHANGED — agent must actually modify target file(s)
2. PATTERN_ABSENT — buggy code pattern must be gone
3. PATTERN_PRESENT — fix code pattern must exist
4. fix_bug template uses structured criteria
"""
import pytest
from pathlib import Path
from unittest.mock import MagicMock

from core.models import SuccessCriterion, CriterionType
from evaluator.checkers.bugfix_patterns import BugfixPatternChecker
from evaluator.models import EvaluationContext


@pytest.fixture
def checker():
    return BugfixPatternChecker()


def _make_context(work_dir=None, artifacts=None):
    return EvaluationContext(
        work_dir=work_dir or Path("."),
        artifacts=artifacts,
        session_store=MagicMock(),
    )


class TestFileChanged:
    def test_passes_when_file_in_output_artifacts(self, checker):
        crit = SuccessCriterion(type=CriterionType.FILE_CHANGED, path="service.py")
        ctx = _make_context(artifacts=["service.py", "utils.py"])
        result = checker.check(crit, ctx)
        assert result.passed
        assert "changed" in result.message.lower()

    def test_fails_when_file_not_in_artifacts(self, checker):
        crit = SuccessCriterion(type=CriterionType.FILE_CHANGED, path="service.py")
        ctx = _make_context(artifacts=["utils.py"])
        result = checker.check(crit, ctx)
        assert not result.passed
        assert "not changed" in result.message.lower()

    def test_fails_when_no_artifacts(self, checker):
        crit = SuccessCriterion(type=CriterionType.FILE_CHANGED, path="service.py")
        ctx = _make_context(artifacts=None)
        result = checker.check(crit, ctx)
        assert not result.passed

    def test_multiple_files(self, checker):
        crit = SuccessCriterion(type=CriterionType.FILE_CHANGED, path="a.py, b.py")
        ctx = _make_context(artifacts=["a.py", "b.py"])
        result = checker.check(crit, ctx)
        assert result.passed

    def test_no_path_passes_with_any_artifacts(self, checker):
        crit = SuccessCriterion(type=CriterionType.FILE_CHANGED)
        ctx = _make_context(artifacts=["anything.py"])
        result = checker.check(crit, ctx)
        assert result.passed

    def test_no_path_no_artifacts_fails(self, checker):
        crit = SuccessCriterion(type=CriterionType.FILE_CHANGED)
        ctx = _make_context(artifacts=None)
        result = checker.check(crit, ctx)
        assert not result.passed


class TestPatternAbsent:
    def test_passes_when_pattern_gone(self, checker, tmp_path):
        (tmp_path / "service.py").write_text("x = get_dependencies()\n")
        crit = SuccessCriterion(
            type=CriterionType.PATTERN_ABSENT,
            path="service.py",
            pattern=r"n\.dependencies",
        )
        ctx = _make_context(work_dir=tmp_path)
        result = checker.check(crit, ctx)
        assert result.passed
        assert "absent" in result.message.lower()

    def test_fails_when_pattern_still_present(self, checker, tmp_path):
        (tmp_path / "service.py").write_text("result = n.dependencies\n")
        crit = SuccessCriterion(
            type=CriterionType.PATTERN_ABSENT,
            path="service.py",
            pattern=r"n\.dependencies",
        )
        ctx = _make_context(work_dir=tmp_path)
        result = checker.check(crit, ctx)
        assert not result.passed
        assert "still present" in result.message.lower()

    def test_passes_when_file_missing(self, checker, tmp_path):
        crit = SuccessCriterion(
            type=CriterionType.PATTERN_ABSENT,
            path="nonexistent.py",
            pattern="bug",
        )
        ctx = _make_context(work_dir=tmp_path)
        result = checker.check(crit, ctx)
        assert result.passed

    def test_skips_when_no_path(self, checker, tmp_path):
        crit = SuccessCriterion(type=CriterionType.PATTERN_ABSENT, pattern="bug")
        ctx = _make_context(work_dir=tmp_path)
        result = checker.check(crit, ctx)
        assert result.passed

    def test_invalid_regex_fails(self, checker, tmp_path):
        (tmp_path / "code.py").write_text("x = 1\n")
        crit = SuccessCriterion(
            type=CriterionType.PATTERN_ABSENT,
            path="code.py",
            pattern="[invalid",
        )
        ctx = _make_context(work_dir=tmp_path)
        result = checker.check(crit, ctx)
        assert not result.passed
        assert "invalid" in result.message.lower()


class TestPatternPresent:
    def test_passes_when_pattern_exists(self, checker, tmp_path):
        (tmp_path / "service.py").write_text("result = get_deps(node)\n")
        crit = SuccessCriterion(
            type=CriterionType.PATTERN_PRESENT,
            path="service.py",
            pattern=r"get_deps\(",
        )
        ctx = _make_context(work_dir=tmp_path)
        result = checker.check(crit, ctx)
        assert result.passed

    def test_fails_when_pattern_missing(self, checker, tmp_path):
        (tmp_path / "service.py").write_text("x = 1\n")
        crit = SuccessCriterion(
            type=CriterionType.PATTERN_PRESENT,
            path="service.py",
            pattern=r"get_deps\(",
        )
        ctx = _make_context(work_dir=tmp_path)
        result = checker.check(crit, ctx)
        assert not result.passed
        assert "not found" in result.message.lower()

    def test_fails_when_file_missing(self, checker, tmp_path):
        crit = SuccessCriterion(
            type=CriterionType.PATTERN_PRESENT,
            path="nonexistent.py",
            pattern="fix",
        )
        ctx = _make_context(work_dir=tmp_path)
        result = checker.check(crit, ctx)
        assert not result.passed
        assert "does not exist" in result.message.lower()


class TestFixBugTemplateCriteria:
    def test_template_has_structured_criteria(self):
        """fix_bug template should use file_changed/pattern criteria."""
        import yaml
        template_path = Path(__file__).parent.parent / "templates" / "fix_bug.yaml"
        with open(template_path) as f:
            template = yaml.safe_load(f)

        gen_node = next(n for n in template["nodes"] if n["id"] == "generator_fix")
        criteria = gen_node.get("success_criteria", [])

        types = [c["type"] for c in criteria]
        assert "file_changed" in types, "Missing file_changed criterion"
        assert "lint" in types, "Missing lint criterion"
        assert template["version"] == "2.0"

    def test_new_criterion_types_in_model(self):
        """New criterion types must be in the enum."""
        assert CriterionType.FILE_CHANGED.value == "file_changed"
        assert CriterionType.PATTERN_ABSENT.value == "pattern_absent"
        assert CriterionType.PATTERN_PRESENT.value == "pattern_present"

    def test_success_criterion_has_pattern_field(self):
        """SuccessCriterion model must have pattern field."""
        crit = SuccessCriterion(
            type=CriterionType.PATTERN_ABSENT,
            path="file.py",
            pattern=r"bug_pattern",
        )
        assert crit.pattern == r"bug_pattern"
