"""Tests for product-driven DAG edge derivation.

Validates that edges are correctly derived from node product declarations,
supplemented from LLM edges, and validated for consistency.
"""
import pytest

from orchestrator.planner import derive_edges_from_products, _has_products
from orchestrator.plan_validator import PlanValidator, PlanValidationError


# ---------------------------------------------------------------------------
# _has_products helper
# ---------------------------------------------------------------------------

class TestHasProducts:
    def test_no_products(self):
        nodes = [
            {"id": "a", "agent_type": "planner"},
            {"id": "b", "agent_type": "generator"},
        ]
        assert _has_products(nodes) is False

    def test_with_output_products(self):
        nodes = [
            {"id": "a", "output_products": ["plan"]},
            {"id": "b"},
        ]
        assert _has_products(nodes) is True

    def test_with_input_products(self):
        nodes = [
            {"id": "a"},
            {"id": "b", "input_products": ["plan"]},
        ]
        assert _has_products(nodes) is True

    def test_with_empty_products(self):
        nodes = [
            {"id": "a", "output_products": []},
            {"id": "b", "input_products": []},
        ]
        assert _has_products(nodes) is False

    def test_empty_nodes(self):
        assert _has_products([]) is False


# ---------------------------------------------------------------------------
# derive_edges_from_products core logic
# ---------------------------------------------------------------------------

class TestDeriveEdgesFromProducts:
    def test_linear_chain(self):
        """plan -> impl -> test -> eval"""
        nodes = [
            {"id": "plan", "output_products": ["implementation_plan"]},
            {"id": "impl", "input_products": ["implementation_plan"],
             "output_products": ["source_code"]},
            {"id": "test", "input_products": ["source_code"],
             "output_products": ["test_files"]},
            {"id": "eval", "input_products": ["test_files"]},
        ]
        edges = derive_edges_from_products(nodes)
        edge_set = {(e["from"], e["to"]) for e in edges}
        assert ("plan", "impl") in edge_set
        assert ("impl", "test") in edge_set
        assert ("test", "eval") in edge_set
        assert len(edges) == 3

    def test_fan_out(self):
        """plan -> [impl_a, impl_b] (parallel)"""
        nodes = [
            {"id": "plan", "output_products": ["implementation_plan"]},
            {"id": "impl_a", "input_products": ["implementation_plan"],
             "output_products": ["source_a"]},
            {"id": "impl_b", "input_products": ["implementation_plan"],
             "output_products": ["source_b"]},
        ]
        edges = derive_edges_from_products(nodes)
        edge_set = {(e["from"], e["to"]) for e in edges}
        assert ("plan", "impl_a") in edge_set
        assert ("plan", "impl_b") in edge_set
        assert len(edges) == 2

    def test_fan_in(self):
        """[impl_a, impl_b] -> eval"""
        nodes = [
            {"id": "impl_a", "output_products": ["source_a"]},
            {"id": "impl_b", "output_products": ["source_b"]},
            {"id": "eval", "input_products": ["source_a", "source_b"]},
        ]
        edges = derive_edges_from_products(nodes)
        edge_set = {(e["from"], e["to"]) for e in edges}
        assert ("impl_a", "eval") in edge_set
        assert ("impl_b", "eval") in edge_set
        assert len(edges) == 2

    def test_diamond(self):
        """plan -> [impl, test] -> eval (fan-out then fan-in)"""
        nodes = [
            {"id": "plan", "output_products": ["implementation_plan"]},
            {"id": "impl", "input_products": ["implementation_plan"],
             "output_products": ["source_code"]},
            {"id": "test", "input_products": ["implementation_plan"],
             "output_products": ["test_files"]},
            {"id": "eval", "input_products": ["source_code", "test_files"]},
        ]
        edges = derive_edges_from_products(nodes)
        edge_set = {(e["from"], e["to"]) for e in edges}
        assert ("plan", "impl") in edge_set
        assert ("plan", "test") in edge_set
        assert ("impl", "eval") in edge_set
        assert ("test", "eval") in edge_set
        assert len(edges) == 4

    def test_duplicate_output_products_raises(self):
        """Two nodes producing the same product -> PlanValidationError."""
        nodes = [
            {"id": "a", "output_products": ["source_code"]},
            {"id": "b", "output_products": ["source_code"]},
            {"id": "c", "input_products": ["source_code"]},
        ]
        with pytest.raises(PlanValidationError, match="Duplicate output product"):
            derive_edges_from_products(nodes)

    def test_unsatisfied_input_products_no_error(self):
        """Input product with no producer -> no error, edge skipped."""
        nodes = [
            {"id": "a", "output_products": ["x"]},
            {"id": "b", "input_products": ["x", "y"]},  # y has no producer
        ]
        edges = derive_edges_from_products(nodes)
        edge_set = {(e["from"], e["to"]) for e in edges}
        assert ("a", "b") in edge_set
        # Only one edge: y has no producer so it's skipped
        assert len(edges) == 1

    def test_empty_products_returns_empty(self):
        """No products declared -> no edges derived."""
        nodes = [
            {"id": "a"},
            {"id": "b"},
        ]
        edges = derive_edges_from_products(nodes)
        assert edges == []

    def test_all_edges_are_hard(self):
        """Product-derived edges should all be hard deps."""
        nodes = [
            {"id": "plan", "output_products": ["plan_out"]},
            {"id": "impl", "input_products": ["plan_out"]},
        ]
        edges = derive_edges_from_products(nodes)
        for edge in edges:
            assert edge["dependency_type"] == "hard"

    def test_real_world_bug_fix_scenario(self):
        """The exact scenario from job_e8e8125ef386:
        plan -> impl_fix, impl_tests (parallel) -> eval -> push_pr
        """
        nodes = [
            {"id": "plan", "agent_type": "planner",
             "output_products": ["issue_analysis"]},
            {"id": "impl_fix", "agent_type": "generator",
             "input_products": ["issue_analysis"],
             "output_products": ["source_code"]},
            {"id": "impl_tests", "agent_type": "generator",
             "input_products": ["issue_analysis"],
             "output_products": ["test_files"]},
            {"id": "eval", "agent_type": "evaluator",
             "input_products": ["source_code", "test_files"],
             "output_products": ["eval_result"]},
            {"id": "push_pr", "agent_type": "generator",
             "input_products": ["source_code", "test_files", "eval_result"]},
        ]
        edges = derive_edges_from_products(nodes)
        edge_set = {(e["from"], e["to"]) for e in edges}

        # push_pr must depend on impl_fix, impl_tests, AND eval
        assert ("impl_fix", "push_pr") in edge_set
        assert ("impl_tests", "push_pr") in edge_set
        assert ("eval", "push_pr") in edge_set
        # eval depends on impl_fix and impl_tests
        assert ("impl_fix", "eval") in edge_set
        assert ("impl_tests", "eval") in edge_set
        # impl_fix and impl_tests depend on plan
        assert ("plan", "impl_fix") in edge_set
        assert ("plan", "impl_tests") in edge_set
        assert len(edges) == 7


# ---------------------------------------------------------------------------
# Supplement from LLM edges (orphaned node soft deps)
# ---------------------------------------------------------------------------

class TestDeriveEdgesFromProductsSupplement:
    def test_orphaned_node_supplemented_from_llm_edges(self):
        """Node with no incoming product edges gets LLM edge as soft dep."""
        nodes = [
            {"id": "plan", "agent_type": "planner",
             "output_products": ["plan_out"]},
            {"id": "impl", "agent_type": "generator",
             "input_products": ["plan_out"],
             "output_products": ["source_code"]},
            {"id": "notify", "agent_type": "generator"},
        ]
        llm_edges = [
            {"from": "impl", "to": "notify"},
        ]
        edges = derive_edges_from_products(nodes, llm_edges=llm_edges)
        edge_set = {(e["from"], e["to"]) for e in edges}

        # Product-derived edge
        assert ("plan", "impl") in edge_set
        # Supplemented soft dep for orphaned 'notify'
        assert ("impl", "notify") in edge_set
        # Find the supplemented edge and verify it's soft
        notify_edge = next(e for e in edges if e["to"] == "notify")
        assert notify_edge["dependency_type"] == "soft"

    def test_planner_node_not_supplemented(self):
        """Planner nodes have no incoming edges by nature — not orphaned."""
        nodes = [
            {"id": "plan", "agent_type": "planner",
             "output_products": ["plan_out"]},
            {"id": "impl", "agent_type": "generator",
             "input_products": ["plan_out"]},
        ]
        llm_edges = [
            {"from": "impl", "to": "plan"},  # wrong direction, should be ignored
        ]
        edges = derive_edges_from_products(nodes, llm_edges=llm_edges)
        edge_set = {(e["from"], e["to"]) for e in edges}
        assert ("plan", "impl") in edge_set
        assert ("impl", "plan") not in edge_set

    def test_no_supplement_when_no_llm_edges(self):
        """No llm_edges provided -> no supplementation."""
        nodes = [
            {"id": "plan", "output_products": ["plan_out"]},
            {"id": "orphan", "agent_type": "generator"},
        ]
        edges = derive_edges_from_products(nodes)
        assert len(edges) == 0

    def test_supplement_ignores_invalid_node_refs(self):
        """LLM edges referencing non-existent nodes are skipped."""
        nodes = [
            {"id": "plan", "agent_type": "planner",
             "output_products": ["plan_out"]},
            {"id": "impl", "agent_type": "generator",
             "input_products": ["plan_out"]},
            {"id": "orphan", "agent_type": "generator"},
        ]
        llm_edges = [
            {"from": "nonexistent", "to": "orphan"},
        ]
        edges = derive_edges_from_products(nodes, llm_edges=llm_edges)
        edge_set = {(e["from"], e["to"]) for e in edges}
        # Only the product-derived edge
        assert edge_set == {("plan", "impl")}


# ---------------------------------------------------------------------------
# PlanValidator product consistency checks
# ---------------------------------------------------------------------------

class TestPlanValidatorProductConsistency:
    def test_valid_products_pass(self):
        """No duplicate outputs, all inputs satisfied -> no errors."""
        plan = {
            "nodes": [
                {"id": "a", "output_products": ["x"]},
                {"id": "b", "input_products": ["x"], "output_products": ["y"]},
            ],
            "edges": [{"from": "a", "to": "b"}],
        }
        result = PlanValidator().validate(plan)
        assert result is not None

    def test_duplicate_output_products_fails(self):
        """Two nodes producing the same product -> PlanValidationError."""
        plan = {
            "nodes": [
                {"id": "a", "output_products": ["x"]},
                {"id": "b", "output_products": ["x"]},
            ],
            "edges": [],
        }
        with pytest.raises(PlanValidationError, match="Duplicate output product"):
            PlanValidator().validate(plan)

    def test_unsatisfied_input_products_warns(self):
        """Input product with no producer -> warning, no error."""
        plan = {
            "nodes": [
                {"id": "a", "output_products": ["x"]},
                {"id": "b", "input_products": ["x", "missing"]},
            ],
            "edges": [{"from": "a", "to": "b"}],
        }
        v = PlanValidator()
        v.validate(plan)
        assert any("missing" in w for w in v.warnings)

    def test_no_products_no_extra_checks(self):
        """Plans without products should still pass validation."""
        plan = {
            "nodes": [{"id": "a"}, {"id": "b"}],
            "edges": [{"from": "a", "to": "b"}],
        }
        result = PlanValidator().validate(plan)
        assert result is not None


# ---------------------------------------------------------------------------
# Integration: _plan_to_dag preserves products
# ---------------------------------------------------------------------------

class TestPlanToDagProducts:
    def test_products_passed_to_dag_node(self):
        """Products from plan_data are stored on DAGNode instances."""
        from orchestrator.planner import Planner
        from core.models import OrchestratorPlan

        plan_data = {
            "nodes": [
                {"id": "plan", "agent_type": "planner",
                 "task": "Plan", "output_products": ["plan_out"]},
                {"id": "impl", "agent_type": "generator",
                 "task": "Implement",
                 "input_products": ["plan_out"],
                 "output_products": ["source_code"]},
            ],
            "edges": [{"from": "plan", "to": "impl"}],
        }
        plan = OrchestratorPlan(**plan_data)
        dag = Planner._plan_to_dag(plan)

        assert dag.nodes["plan"].output_products == ["plan_out"]
        assert dag.nodes["impl"].input_products == ["plan_out"]
        assert dag.nodes["impl"].output_products == ["source_code"]

    def test_products_empty_by_default(self):
        """DAGNodes without products have empty lists."""
        from orchestrator.planner import Planner
        from core.models import OrchestratorPlan

        plan_data = {
            "nodes": [
                {"id": "a", "agent_type": "generator", "task": "Do stuff"},
            ],
            "edges": [],
        }
        plan = OrchestratorPlan(**plan_data)
        dag = Planner._plan_to_dag(plan)
        assert dag.nodes["a"].input_products == []
        assert dag.nodes["a"].output_products == []
