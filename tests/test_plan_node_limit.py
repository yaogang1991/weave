"""
Tests for #292: planner node count limit.

The PlanValidator rejects plans with more than max_nodes nodes,
preventing JSON truncation when the LLM generates oversized DAGs.
The orchestrator retries once with explicit feedback when this occurs.
"""
import pytest

from orchestrator.plan_validator import PlanValidator, PlanValidationError

MAX_NODES = 10


def _make_plan(num_nodes: int) -> dict:
    """Generate a valid plan with the given number of nodes."""
    nodes = [{"id": f"node_{i}", "agent_type": "generator", "task": f"task {i}"}
             for i in range(num_nodes)]
    edges = [{"from": f"node_{i}", "to": f"node_{i + 1}"}
             for i in range(num_nodes - 1)]
    return {"nodes": nodes, "edges": edges}


def _validator() -> PlanValidator:
    """Create a PlanValidator with max_nodes set to the test limit."""
    return PlanValidator(max_nodes=MAX_NODES)


class TestNodeCountLimit:
    def test_plan_at_limit_passes(self):
        """Plan with exactly max_nodes should pass validation."""
        plan = _make_plan(MAX_NODES)
        result = _validator().validate(plan)
        assert result is not None

    def test_plan_under_limit_passes(self):
        """Plan with fewer than max_nodes should pass validation."""
        plan = _make_plan(5)
        result = _validator().validate(plan)
        assert result is not None

    def test_plan_over_limit_rejected(self):
        """Plan with more than max_nodes should be rejected."""
        plan = _make_plan(MAX_NODES + 1)
        with pytest.raises(PlanValidationError, match=f"{MAX_NODES + 1} nodes"):
            _validator().validate(plan)

    def test_plan_far_over_limit_rejected(self):
        """Plan with 20 nodes (the R21 scenario) should be rejected."""
        plan = _make_plan(20)
        with pytest.raises(PlanValidationError, match="20 nodes"):
            _validator().validate(plan)

    def test_error_message_includes_max(self):
        """Error message should mention the maximum allowed."""
        plan = _make_plan(15)
        with pytest.raises(PlanValidationError, match=f"maximum {MAX_NODES}"):
            _validator().validate(plan)

    def test_empty_plan_passes(self):
        """Plan with no nodes should pass (edge case)."""
        result = _validator().validate({"nodes": [], "edges": []})
        assert result is not None

    def test_single_node_plan_passes(self):
        """Plan with 1 node should pass."""
        plan = _make_plan(1)
        result = _validator().validate(plan)
        assert result is not None

    def test_max_nodes_default(self):
        """PlanValidator accepts max_nodes via constructor."""
        validator = PlanValidator(max_nodes=MAX_NODES)
        assert validator.max_nodes == MAX_NODES
