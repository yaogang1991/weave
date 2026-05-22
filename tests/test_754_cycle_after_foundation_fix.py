"""Tests for #754: cycle detection after foundation dependency auto-fix.

Verifies that:
1. Adding a foundation edge that creates a cycle raises PlanValidationError
2. Adding a foundation edge that does NOT create a cycle works normally
"""
import pytest

from orchestrator.plan_validator import PlanValidator, PlanValidationError


def test_cycle_detected_after_foundation_auto_fix():
    """Foundation auto-fix that creates a cycle is caught (#754)."""
    validator = PlanValidator(auto_fix=True)

    # Node A (foundation by keyword) → B exists
    # Auto-fix will try to add B → A (foundation dep), creating cycle
    plan = {
        "nodes": [
            {
                "id": "impl_foundation",
                "agent_type": "generator",
                "task": "Create shared foundation models",
            },
            {
                "id": "impl_auth",
                "agent_type": "generator",
                "task": "Implement auth module",
            },
        ],
        "edges": [
            # impl_auth depends on impl_foundation — this is normal
            {"from": "impl_auth", "to": "impl_foundation"},
        ],
    }

    # The auto-fix sees impl_auth has no foundation dep FROM foundation
    # (it has edge TO foundation, not FROM foundation).
    # Wait — let me re-check the logic. The edge_set stores (from, to).
    # Edge is impl_auth → impl_foundation.
    # The check is: does (fid, nid) exist? fid=impl_foundation, nid=impl_auth.
    # That's (impl_foundation, impl_auth) — NOT in edge_set.
    # So it adds impl_foundation → impl_auth.
    # Now: impl_foundation → impl_auth → impl_foundation = CYCLE!
    with pytest.raises(PlanValidationError, match="cycle"):
        validator.validate(plan)


def test_no_cycle_when_foundation_dep_already_exists():
    """No cycle when foundation dep already exists normally (#754)."""
    validator = PlanValidator(auto_fix=True)

    plan = {
        "nodes": [
            {
                "id": "impl_foundation",
                "agent_type": "generator",
                "task": "Create shared foundation models",
            },
            {
                "id": "impl_auth",
                "agent_type": "generator",
                "task": "Implement auth module",
            },
        ],
        "edges": [
            # Foundation dep already exists correctly
            {"from": "impl_foundation", "to": "impl_auth"},
        ],
    }

    result = validator.validate(plan)
    assert result is not None
    # No cycle error, no new edges added
    assert len(result["edges"]) == 1


def test_foundation_auto_fix_adds_edge_without_cycle():
    """Foundation auto-fix adds edge when no cycle results (#754)."""
    validator = PlanValidator(auto_fix=True)

    plan = {
        "nodes": [
            {
                "id": "impl_foundation",
                "agent_type": "generator",
                "task": "Create shared foundation models",
            },
            {
                "id": "impl_auth",
                "agent_type": "generator",
                "task": "Implement auth module",
            },
            {
                "id": "eval_auth",
                "agent_type": "evaluator",
                "task": "Evaluate auth",
            },
        ],
        "edges": [
            {"from": "impl_auth", "to": "eval_auth"},
        ],
    }

    result = validator.validate(plan)
    # Foundation edge was added: impl_foundation → impl_auth
    assert len(result["edges"]) == 2
    foundation_edge = next(
        (e for e in result["edges"]
         if e["from"] == "impl_foundation" and e["to"] == "impl_auth"),
        None,
    )
    assert foundation_edge is not None
    assert foundation_edge["dependency_type"] == "hard"
