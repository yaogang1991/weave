"""Tests for orchestrator/plan_validator.py"""
import pytest
from orchestrator.plan_validator import PlanValidator, PlanValidationError


def test_valid_plan():
    plan = {
        "nodes": [{"id": "a"}, {"id": "b"}],
        "edges": [{"from": "a", "to": "b"}],
    }
    result = PlanValidator().validate(plan)
    assert result is plan


def test_duplicate_node_id_raises():
    plan = {
        "nodes": [{"id": "a"}, {"id": "a"}, {"id": "b"}],
        "edges": [{"from": "a", "to": "b"}],
    }
    with pytest.raises(PlanValidationError, match="Duplicate node ID: a"):
        PlanValidator().validate(plan)


def test_duplicate_with_edge_ambiguity():
    """nodes=[A, A, B], edges=[A->B] — should raise, not auto-fix."""
    plan = {
        "nodes": [{"id": "A"}, {"id": "A"}, {"id": "B"}],
        "edges": [{"from": "A", "to": "B"}],
    }
    with pytest.raises(PlanValidationError, match="Duplicate"):
        PlanValidator().validate(plan)


def test_dangling_edge_source():
    plan = {
        "nodes": [{"id": "b"}],
        "edges": [{"from": "a", "to": "b"}],
    }
    with pytest.raises(PlanValidationError, match="source node 'a' does not exist"):
        PlanValidator().validate(plan)


def test_dangling_edge_target():
    plan = {
        "nodes": [{"id": "a"}],
        "edges": [{"from": "a", "to": "b"}],
    }
    with pytest.raises(PlanValidationError, match="target node 'b' does not exist"):
        PlanValidator().validate(plan)


def test_cycle_detection():
    plan = {
        "nodes": [{"id": "a"}, {"id": "b"}, {"id": "c"}],
        "edges": [
            {"from": "a", "to": "b"},
            {"from": "b", "to": "c"},
            {"from": "c", "to": "a"},
        ],
    }
    with pytest.raises(PlanValidationError, match="cycle"):
        PlanValidator().validate(plan)


def test_self_loop():
    plan = {
        "nodes": [{"id": "a"}],
        "edges": [{"from": "a", "to": "a"}],
    }
    with pytest.raises(PlanValidationError, match="cycle"):
        PlanValidator().validate(plan)


def test_empty_plan():
    plan = {"nodes": [], "edges": []}
    result = PlanValidator().validate(plan)
    assert result is plan


def test_no_edges():
    plan = {
        "nodes": [{"id": "a"}, {"id": "b"}],
        "edges": [],
    }
    result = PlanValidator().validate(plan)
    assert result is plan


def test_auto_fix_flag_accepted_but_no_mutation():
    """auto_fix=True is accepted for API compat but never mutates."""
    plan = {
        "nodes": [{"id": "a"}, {"id": "a"}],
        "edges": [],
    }
    with pytest.raises(PlanValidationError):
        PlanValidator(auto_fix=True).validate(plan)


def test_collision_free_id_not_confused():
    """Ensure A_1 is not confused with auto-renamed A."""
    plan = {
        "nodes": [{"id": "A"}, {"id": "A_1"}, {"id": "A"}],
        "edges": [],
    }
    with pytest.raises(PlanValidationError, match="Duplicate node ID: A"):
        PlanValidator().validate(plan)
