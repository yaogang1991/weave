"""Tests for bug-fix verification criteria (issue #117).

Verifies:
1. FILE_CHANGED — agent must actually modify target file(s)
2. PATTERN_ABSENT — buggy code pattern must be gone
3. PATTERN_PRESENT — fix code pattern must exist
4. fix_bug template uses structured criteria
"""
import re
import pytest
from pathlib import Path
from unittest.mock import MagicMock

from core.models import SuccessCriterion, CriterionType
from evaluator.engine import EvaluatorEngine


@pytest.fixture
def engine():
    return EvaluatorEngine(MagicMock())


class TestFileChanged:
    def test_passes_when_file_in_output_artifacts(self, engine):
        crit = SuccessCriterion(type=CriterionType.FILE_CHANGED, path="service.py")
        passed, msg = engine._check_file_changed(crit, output_artifacts=["service.py", "utils.py"])
        assert passed
        assert "changed" in msg.lower()

    def test_fails_when_file_not_in_artifacts(self, engine):
        crit = SuccessCriterion(type=CriterionType.FILE_CHANGED, path="service.py")
        passed, msg = engine._check_file_changed(crit, output_artifacts=["utils.py"])
        assert not passed
        assert "not changed" in msg.lower()

    def test_fails_when_no_artifacts(self, engine):
        crit = SuccessCriterion(type=CriterionType.FILE_CHANGED, path="service.py")
        passed, msg = engine._check_file_changed(crit, output_artifacts=None)
        assert not passed
        assert "no files changed" in msg.lower()

    def test_multiple_files(self, engine):
        crit = SuccessCriterion(type=CriterionType.FILE_CHANGED, path="a.py, b.py")
        passed, msg = engine._check_file_changed(crit, output_artifacts=["a.py", "b.py"])
        assert passed

    def test_no_path_passes_with_any_artifacts(self, engine):
        crit = SuccessCriterion(type=CriterionType.FILE_CHANGED)
        passed, msg = engine._check_file_changed(crit, output_artifacts=["anything.py"])
        assert passed

    def test_no_path_no_artifacts_fails(self, engine):
        crit = SuccessCriterion(type=CriterionType.FILE_CHANGED)
        passed, msg = engine._check_file_changed(crit, output_artifacts=None)
        assert not passed


class TestPatternAbsent:
    def test_passes_when_pattern_gone(self, engine, tmp_path):
        (tmp_path / "service.py").write_text("x = get_dependencies()\n")
        crit = SuccessCriterion(
            type=CriterionType.PATTERN_ABSENT,
            path="service.py",
            pattern=r"n\.dependencies",
        )
        passed, msg = engine._check_pattern_absent(crit, tmp_path)
        assert passed
        assert "absent" in msg.lower()

    def test_fails_when_pattern_still_present(self, engine, tmp_path):
        (tmp_path / "service.py").write_text("result = n.dependencies\n")
        crit = SuccessCriterion(
            type=CriterionType.PATTERN_ABSENT,
            path="service.py",
            pattern=r"n\.dependencies",
        )
        passed, msg = engine._check_pattern_absent(crit, tmp_path)
        assert not passed
        assert "still present" in msg.lower()

    def test_passes_when_file_missing(self, engine, tmp_path):
        crit = SuccessCriterion(
            type=CriterionType.PATTERN_ABSENT,
            path="nonexistent.py",
            pattern="bug",
        )
        passed, msg = engine._check_pattern_absent(crit, tmp_path)
        assert passed

    def test_skips_when_no_path(self, engine, tmp_path):
        crit = SuccessCriterion(type=CriterionType.PATTERN_ABSENT, pattern="bug")
        passed, msg = engine._check_pattern_absent(crit, tmp_path)
        assert passed

    def test_invalid_regex_fails(self, engine, tmp_path):
        (tmp_path / "code.py").write_text("x = 1\n")
        crit = SuccessCriterion(
            type=CriterionType.PATTERN_ABSENT,
            path="code.py",
            pattern="[invalid",
        )
        passed, msg = engine._check_pattern_absent(crit, tmp_path)
        assert not passed
        assert "invalid regex" in msg.lower()


class TestPatternPresent:
    def test_passes_when_pattern_exists(self, engine, tmp_path):
        (tmp_path / "service.py").write_text("result = get_deps(node)\n")
        crit = SuccessCriterion(
            type=CriterionType.PATTERN_PRESENT,
            path="service.py",
            pattern=r"get_deps\(",
        )
        passed, msg = engine._check_pattern_present(crit, tmp_path)
        assert passed

    def test_fails_when_pattern_missing(self, engine, tmp_path):
        (tmp_path / "service.py").write_text("x = 1\n")
        crit = SuccessCriterion(
            type=CriterionType.PATTERN_PRESENT,
            path="service.py",
            pattern=r"get_deps\(",
        )
        passed, msg = engine._check_pattern_present(crit, tmp_path)
        assert not passed
        assert "not found" in msg.lower()

    def test_fails_when_file_missing(self, engine, tmp_path):
        crit = SuccessCriterion(
            type=CriterionType.PATTERN_PRESENT,
            path="nonexistent.py",
            pattern="fix",
        )
        passed, msg = engine._check_pattern_present(crit, tmp_path)
        assert not passed
        assert "does not exist" in msg.lower()


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
