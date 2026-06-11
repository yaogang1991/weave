"""Tests for orchestrator/budget_planner.py — token budget allocation."""
from __future__ import annotations

import pytest

from core.models import DAG, DAGNode
from orchestrator.budget_planner import BudgetPlanner


@pytest.fixture
def planner():
    return BudgetPlanner()


def _make_dag(nodes_spec: list[tuple[str, str]]) -> DAG:
    """Create a DAG from (id, agent_type) tuples."""
    dag = DAG(reasoning="test")
    for nid, atype in nodes_spec:
        dag = dag.add_node(DAGNode(id=nid, agent_type=atype, task_description=f"task for {nid}"))
    return dag


class TestAllocateBudget:
    def test_single_generator_gets_full_budget(self, planner):
        dag = _make_dag([("g1", "generator")])
        result = planner.allocate_budget(dag, 10000)
        assert result.nodes["g1"].token_budget == 10000

    def test_two_generators_split_equally(self, planner):
        dag = _make_dag([("g1", "generator"), ("g2", "generator")])
        planner.allocate_budget(dag, 10000)
        assert dag.nodes["g1"].token_budget == dag.nodes["g2"].token_budget

    def test_mixed_types_get_weighted_allocation(self, planner):
        dag = _make_dag([
            ("p1", "planner"),
            ("g1", "generator"),
            ("e1", "evaluator"),
        ])
        planner.allocate_budget(dag, 100000)
        assert dag.nodes["g1"].token_budget > dag.nodes["e1"].token_budget
        assert dag.nodes["e1"].token_budget > dag.nodes["p1"].token_budget

    def test_minimum_budget_floor(self, planner):
        dag = _make_dag([("x1", "custom_type")])
        planner.allocate_budget(dag, 100)
        assert dag.nodes["x1"].token_budget >= 1024

    def test_zero_budget_returns_unchanged(self, planner):
        dag = _make_dag([("g1", "generator")])
        result = planner.allocate_budget(dag, 0)
        assert result.nodes["g1"].token_budget == 8192

    def test_negative_budget_returns_unchanged(self, planner):
        dag = _make_dag([("g1", "generator")])
        result = planner.allocate_budget(dag, -100)
        assert result.nodes["g1"].token_budget == 8192

    def test_empty_dag_returns_unchanged(self, planner):
        dag = DAG(reasoning="empty")
        result = planner.allocate_budget(dag, 10000)
        assert len(result.nodes) == 0

    def test_unknown_agent_type_gets_default_weight(self, planner):
        dag = _make_dag([("x1", "custom_agent")])
        planner.allocate_budget(dag, 10000)
        assert dag.nodes["x1"].token_budget > 0

    def test_returns_same_dag_object(self, planner):
        dag = _make_dag([("g1", "generator")])
        result = planner.allocate_budget(dag, 10000)
        assert result is dag


class TestCheckBudgetFeasibility:
    def test_no_warnings_when_safe(self, planner):
        dag = _make_dag([("g1", "generator")])
        dag.nodes["g1"].estimated_tokens = 1000
        dag.nodes["g1"].token_budget = 10000
        warnings = planner.check_budget_feasibility(dag)
        assert warnings == []

    def test_warning_when_near_budget(self, planner):
        dag = _make_dag([("g1", "generator")])
        dag.nodes["g1"].estimated_tokens = 9500
        dag.nodes["g1"].token_budget = 10000
        warnings = planner.check_budget_feasibility(dag)
        assert len(warnings) == 1
        assert "g1" in warnings[0]

    def test_no_warning_for_zero_estimated_tokens(self, planner):
        dag = _make_dag([("g1", "generator")])
        dag.nodes["g1"].estimated_tokens = 0
        dag.nodes["g1"].token_budget = 10000
        warnings = planner.check_budget_feasibility(dag)
        assert warnings == []

    def test_no_warning_for_zero_budget(self, planner):
        dag = _make_dag([("g1", "generator")])
        dag.nodes["g1"].estimated_tokens = 5000
        dag.nodes["g1"].token_budget = 0
        warnings = planner.check_budget_feasibility(dag)
        assert warnings == []

    def test_multiple_warnings(self, planner):
        dag = _make_dag([("g1", "generator"), ("g2", "generator")])
        for nid in dag.nodes:
            dag.nodes[nid].estimated_tokens = 9500
            dag.nodes[nid].token_budget = 10000
        warnings = planner.check_budget_feasibility(dag)
        assert len(warnings) == 2

    def test_exactly_at_threshold_no_warning(self, planner):
        dag = _make_dag([("g1", "generator")])
        dag.nodes["g1"].estimated_tokens = 9000
        dag.nodes["g1"].token_budget = 10000
        warnings = planner.check_budget_feasibility(dag)
        assert warnings == []

    def test_just_over_threshold_warns(self, planner):
        dag = _make_dag([("g1", "generator")])
        dag.nodes["g1"].estimated_tokens = 9001
        dag.nodes["g1"].token_budget = 10000
        warnings = planner.check_budget_feasibility(dag)
        assert len(warnings) == 1
