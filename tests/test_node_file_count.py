"""Tests for #284: plan validator per-node file count limit.

Verifies that the PlanValidator warns when a generator node's task
description implies creating more than MAX_FILES_PER_NODE files,
forcing decomposition into multiple parallel nodes.
"""

from __future__ import annotations

from orchestrator.plan_validator import PlanValidator


def _valid_plan():
    """Minimal valid plan structure."""
    return {
        "nodes": [
            {"id": "plan", "agent_type": "planner", "task": "Plan it"},
            {"id": "impl", "agent_type": "generator", "task": "Implement"},
            {"id": "eval", "agent_type": "evaluator", "task": "Evaluate"},
        ],
        "edges": [
            {"from": "plan", "to": "impl"},
            {"from": "impl", "to": "eval"},
        ],
    }


def test_no_warning_for_small_task():
    """A generator with few files should not trigger a warning."""
    plan = _valid_plan()
    plan["nodes"][1]["task"] = (
        "Create core.py and utils.py for the library"
    )
    validator = PlanValidator()
    validator.validate(plan)
    assert not any("files" in w for w in validator.warnings)


def test_warning_for_many_file_mentions():
    """A generator with >15 .py file mentions should warn."""
    files = ", ".join(f"module_{i}.py" for i in range(20))
    plan = _valid_plan()
    plan["nodes"][1]["task"] = (
        f"Create all source files: {files}"
    )
    validator = PlanValidator()
    validator.validate(plan)
    file_warnings = [w for w in validator.warnings if "files" in w]
    assert len(file_warnings) == 1
    assert "Decompose" in file_warnings[0]


def test_no_warning_for_evaluator():
    """Non-generator nodes should not trigger file count warnings."""
    plan = _valid_plan()
    files = ", ".join(f"module_{i}.py" for i in range(20))
    plan["nodes"][0]["task"] = (
        f"Plan the implementation of {files}"
    )
    validator = PlanValidator()
    validator.validate(plan)
    assert not any("files" in w for w in validator.warnings)


def test_warning_at_boundary():
    """Exactly MAX_FILES_PER_NODE+1 file mentions should warn."""
    limit = PlanValidator.MAX_FILES_PER_NODE
    files = ", ".join(f"f{i}.py" for i in range(limit + 1))
    plan = _valid_plan()
    plan["nodes"][1]["task"] = f"Implement: {files}"
    validator = PlanValidator()
    validator.validate(plan)
    file_warnings = [w for w in validator.warnings if "files" in w]
    assert len(file_warnings) == 1


def test_no_warning_at_exact_limit():
    """Exactly MAX_FILES_PER_NODE file mentions should NOT warn."""
    limit = PlanValidator.MAX_FILES_PER_NODE
    files = ", ".join(f"f{i}.py" for i in range(limit))
    plan = _valid_plan()
    plan["nodes"][1]["task"] = f"Implement: {files}"
    validator = PlanValidator()
    validator.validate(plan)
    assert not any("files" in w for w in validator.warnings)


def test_max_files_per_node_constant():
    """MAX_FILES_PER_NODE should be 15."""
    assert PlanValidator.MAX_FILES_PER_NODE == 15


def test_planning_prompt_has_decomposition_rule():
    """Planning prompt should include the decomposition rule."""
    from pathlib import Path
    prompt = Path("orchestrator/prompts/planning.md").read_text(encoding="utf-8")
    assert "15" in prompt
    assert "decomposition" in prompt.lower() or "Decompose" in prompt
    assert "foundation" in prompt.lower()
