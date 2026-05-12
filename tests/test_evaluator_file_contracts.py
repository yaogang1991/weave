"""Tests for #160: file_exists vs file_pattern contract separation.

Covers:
- FILE_PATTERN criterion: glob matching against real files on disk
- FILE_EXISTS criterion: actionable feedback with expected vs actual files
- FILE_PATTERN with empty directory, empty files, multiple matches
- Planner prompt includes file_pattern description
- Generator prompt includes file path contract rule
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.models import CriterionType, SuccessCriterion
from evaluator.engine import EvaluatorEngine


@pytest.fixture
def engine():
    return EvaluatorEngine(MagicMock())


# =============================================================================
# FILE_PATTERN criterion
# =============================================================================


class TestFilePattern:
    """FILE_PATTERN matches glob patterns against real non-empty files."""

    def test_pattern_matches_files(self, engine, tmp_path):
        """Non-empty file matching pattern → PASS."""
        (tmp_path / "reporter").mkdir()
        (tmp_path / "reporter" / "engine.py").write_text("x = 1")
        crit = SuccessCriterion(
            type=CriterionType.FILE_PATTERN,
            pattern="reporter/*.py",
            description="report module exists",
        )
        passed, msg, auto = engine._check_criterion(crit, str(tmp_path))
        assert passed is True
        assert "Matched" in msg
        assert "engine.py" in msg

    def test_pattern_no_match(self, engine, tmp_path):
        """No files matching pattern → FAIL."""
        (tmp_path / "other").mkdir()
        (tmp_path / "other" / "main.py").write_text("x = 1")
        crit = SuccessCriterion(
            type=CriterionType.FILE_PATTERN,
            pattern="reporter/*.py",
            description="report module exists",
        )
        passed, msg, auto = engine._check_criterion(crit, str(tmp_path))
        assert passed is False
        assert "No non-empty files matched" in msg

    def test_pattern_empty_file_excluded(self, engine, tmp_path):
        """Empty file matching pattern → does NOT count as match."""
        (tmp_path / "reporter").mkdir()
        (tmp_path / "reporter" / "engine.py").write_text("")
        crit = SuccessCriterion(
            type=CriterionType.FILE_PATTERN,
            pattern="reporter/*.py",
            description="report module exists",
        )
        passed, msg, auto = engine._check_criterion(crit, str(tmp_path))
        assert passed is False

    def test_pattern_multiple_matches(self, engine, tmp_path):
        """Multiple matching files → PASS, lists up to 10."""
        (tmp_path / "reporter").mkdir()
        for i in range(3):
            (tmp_path / "reporter" / f"mod{i}.py").write_text(f"x = {i}")
        crit = SuccessCriterion(
            type=CriterionType.FILE_PATTERN,
            pattern="reporter/*.py",
            description="report modules exist",
        )
        passed, msg, auto = engine._check_criterion(crit, str(tmp_path))
        assert passed is True
        assert "3 file(s)" in msg

    def test_pattern_uses_path_fallback(self, engine, tmp_path):
        """When pattern is empty but path is set, uses path as pattern."""
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_foo.py").write_text("def test_x(): pass")
        crit = SuccessCriterion(
            type=CriterionType.FILE_PATTERN,
            path="tests/test_*.py",
            description="test file exists",
        )
        passed, msg, auto = engine._check_criterion(crit, str(tmp_path))
        assert passed is True

    def test_pattern_no_pattern_no_path(self, engine, tmp_path):
        """No pattern and no path → skip (pass by default)."""
        crit = SuccessCriterion(
            type=CriterionType.FILE_PATTERN,
            description="no pattern",
        )
        passed, msg, auto = engine._check_criterion(crit, str(tmp_path))
        assert passed is True
        assert "skipped" in msg

    def test_pattern_via_evaluate_stage(self, engine, tmp_path):
        """FILE_PATTERN works end-to-end via evaluate_stage."""
        (tmp_path / "reporter").mkdir()
        (tmp_path / "reporter" / "engine.py").write_text("x = 1")
        crit = SuccessCriterion(
            type=CriterionType.FILE_PATTERN,
            pattern="reporter/*.py",
            description="report module",
        )
        result = engine.evaluate_stage(
            session_id="s1",
            stage_name="test",
            criteria=[crit],
            artifact_path=str(tmp_path),
            work_dir=str(tmp_path),
        )
        assert result.passed is True


# =============================================================================
# FILE_EXISTS actionable feedback
# =============================================================================


class TestFileExistsFeedback:
    """FILE_EXISTS returns actionable feedback showing expected vs actual."""

    def test_missing_file_shows_actionable_hint(self, engine, tmp_path):
        """Missing file: feedback mentions alternative (file_pattern)."""
        crit = SuccessCriterion(
            type=CriterionType.FILE_EXISTS,
            path="reporter/report_engine.py",
            description="report engine exists",
        )
        passed, msg, auto = engine._check_criterion(crit, str(tmp_path))
        assert passed is False
        assert "file_pattern" in msg
        assert "reporter/report_engine.py" in msg

    def test_found_and_missing_both_shown(self, engine, tmp_path):
        """When some files exist and some don't, both are shown."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("x = 1")
        crit = SuccessCriterion(
            type=CriterionType.FILE_EXISTS,
            path="src/main.py,src/utils.py",
            description="both files exist",
        )
        passed, msg, auto = engine._check_criterion(crit, str(tmp_path))
        assert passed is False
        assert "utils.py" in msg
        assert "main.py" in msg or "Found" in msg


# =============================================================================
# Prompt updates
# =============================================================================


class TestPromptUpdates:
    """Verify planner and generator prompts contain the new rules."""

    def test_planner_prompt_has_file_pattern(self):
        from orchestrator.intelligent_orchestrator import IntelligentOrchestrator
        prompt = IntelligentOrchestrator.PLANNING_PROMPT_TEMPLATE
        assert "file_pattern" in prompt
        assert "file_exists" in prompt
        assert "exact file path matters" in prompt.lower() or "exact" in prompt.lower()

    def test_planner_prompt_has_file_exists_vs_pattern_guidance(self):
        from orchestrator.intelligent_orchestrator import IntelligentOrchestrator
        prompt = IntelligentOrchestrator.PLANNING_PROMPT_TEMPLATE
        assert "file_exists vs file_pattern" in prompt or "file_exists" in prompt

    def test_generator_prompt_has_path_contract(self):
        from agent.agent_pool import WorkerAgent
        gen_prompt = WorkerAgent.SYSTEM_PROMPTS["generator"]
        assert "FILE PATH CONTRACT" in gen_prompt
        assert "EXACT path" in gen_prompt


# =============================================================================
# CriterionType enum
# =============================================================================


class TestCriterionTypeEnum:
    """FILE_PATTERN is registered in the CriterionType enum."""

    def test_file_pattern_in_enum(self):
        assert hasattr(CriterionType, "FILE_PATTERN")
        assert CriterionType.FILE_PATTERN.value == "file_pattern"

    def test_file_pattern_deserialization(self):
        crit = SuccessCriterion(type="file_pattern", pattern="*.py")
        assert crit.type == CriterionType.FILE_PATTERN

    def test_file_pattern_json_roundtrip(self):
        crit = SuccessCriterion(
            type=CriterionType.FILE_PATTERN,
            pattern="reporter/*.py",
            description="report module",
        )
        data = crit.model_dump()
        restored = SuccessCriterion(**data)
        assert restored.type == CriterionType.FILE_PATTERN
        assert restored.pattern == "reporter/*.py"
