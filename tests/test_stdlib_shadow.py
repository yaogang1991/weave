"""
Tests for #238: stdlib module name shadowing prevention.

Verifies check_stdlib_conflict utility and PlanValidator stdlib detection.
"""
import pytest

from orchestrator.plan_validator import PlanValidator, check_stdlib_conflict


class TestCheckStdlibConflict:
    def test_urllib_conflict(self):
        assert check_stdlib_conflict("urllib") == "urllib"

    def test_json_conflict(self):
        assert check_stdlib_conflict("json") == "json"

    def test_collections_conflict(self):
        assert check_stdlib_conflict("collections") == "collections"

    def test_no_conflict(self):
        assert check_stdlib_conflict("myapp") is None
        assert check_stdlib_conflict("urllib_utils") is None

    def test_case_insensitive(self):
        assert check_stdlib_conflict("Urllib") == "urllib"
        assert check_stdlib_conflict("JSON") == "json"

    def test_hyphen_ignored(self):
        # async-io normalizes to async_io which is not stdlib
        assert check_stdlib_conflict("async-io") is None


class TestPlanValidatorStdlibShadow:
    def test_warns_on_urllib_library(self):
        validator = PlanValidator()
        plan = {
            "nodes": [
                {"id": "impl", "task": 'Create a "urllib" library for URL routing'},
            ],
            "edges": [],
        }
        validator.validate(plan)
        assert any("urllib" in w for w in validator.warnings)

    def test_warns_on_library_prefix(self):
        validator = PlanValidator()
        plan = {
            "nodes": [
                {"id": "impl", "task": "Build a library json for parsing"},
            ],
            "edges": [],
        }
        validator.validate(plan)
        assert any("json" in w for w in validator.warnings)

    def test_no_warn_on_safe_names(self):
        validator = PlanValidator()
        plan = {
            "nodes": [
                {"id": "impl", "task": 'Create a "myurl_lib" library for URLs'},
            ],
            "edges": [],
        }
        validator.validate(plan)
        assert not any("myurl_lib" in w for w in validator.warnings)

    def test_no_warn_on_empty_task(self):
        validator = PlanValidator()
        plan = {
            "nodes": [{"id": "a", "task": ""}],
            "edges": [],
        }
        validator.validate(plan)
        assert len(validator.warnings) == 0

    def test_multiple_conflicts(self):
        validator = PlanValidator()
        plan = {
            "nodes": [
                {"id": "a", "task": 'Create "urllib" and "json" modules'},
                {"id": "b", "task": "Build a library io for streams"},
            ],
            "edges": [],
        }
        validator.validate(plan)
        conflicts = [w for w in validator.warnings if "stdlib" in w]
        # At least urllib and json detected
        assert len(conflicts) >= 2


class TestPlannerPromptStdlibGuidance:
    def test_planning_prompt_mentions_stdlib(self):
        from orchestrator.intelligent_orchestrator import IntelligentOrchestrator
        prompt = IntelligentOrchestrator.PLANNING_PROMPT_TEMPLATE
        assert "stdlib" in prompt.lower()
        assert "urllib" in prompt
        assert "shadowing" in prompt.lower() or "shadow" in prompt.lower()
