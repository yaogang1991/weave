"""
Tests for #292: planner node count limit.

The PlanValidator rejects plans with more than MAX_NODES (10) nodes,
preventing JSON truncation when the LLM generates oversized DAGs.
The orchestrator retries once with explicit feedback when this occurs.
"""
import pytest

from orchestrator.plan_validator import PlanValidator, PlanValidationError


def _make_plan(num_nodes: int) -> dict:
    """Generate a valid plan with the given number of nodes."""
    nodes = [{"id": f"node_{i}", "agent_type": "generator", "task": f"task {i}"}
             for i in range(num_nodes)]
    edges = [{"from": f"node_{i}", "to": f"node_{i+1}"}
             for i in range(num_nodes - 1)]
    return {"nodes": nodes, "edges": edges}


class TestNodeCountLimit:
    def test_plan_at_limit_passes(self):
        """Plan with exactly MAX_NODES (10) should pass validation."""
        plan = _make_plan(10)
        validator = PlanValidator()
        result = validator.validate(plan)
        assert result is not None

    def test_plan_under_limit_passes(self):
        """Plan with fewer than MAX_NODES should pass validation."""
        plan = _make_plan(5)
        validator = PlanValidator()
        result = validator.validate(plan)
        assert result is not None

    def test_plan_over_limit_rejected(self):
        """Plan with more than MAX_NODES (11) should be rejected."""
        plan = _make_plan(11)
        validator = PlanValidator()
        with pytest.raises(PlanValidationError, match="11 nodes"):
            validator.validate(plan)

    def test_plan_far_over_limit_rejected(self):
        """Plan with 20 nodes (the R21 scenario) should be rejected."""
        plan = _make_plan(20)
        validator = PlanValidator()
        with pytest.raises(PlanValidationError, match="20 nodes"):
            validator.validate(plan)

    def test_error_message_includes_max(self):
        """Error message should mention the maximum allowed."""
        plan = _make_plan(15)
        validator = PlanValidator()
        with pytest.raises(PlanValidationError, match="maximum 10"):
            validator.validate(plan)

    def test_empty_plan_passes(self):
        """Plan with no nodes should pass (edge case)."""
        validator = PlanValidator()
        result = validator.validate({"nodes": [], "edges": []})
        assert result is not None

    def test_single_node_plan_passes(self):
        """Plan with 1 node should pass."""
        plan = _make_plan(1)
        validator = PlanValidator()
        result = validator.validate(plan)
        assert result is not None

    def test_max_nodes_constant(self):
        """MAX_NODES is accessible for documentation/testing."""
        assert PlanValidator.MAX_NODES == 10
