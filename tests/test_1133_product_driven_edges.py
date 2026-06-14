"""Tests for #1133: product-driven DAG edge derivation.

The LLM is unreliable at global topology — it places terminal nodes
(e.g. push_pr) at the same parallel level as the impl nodes they must
come after. When nodes declare input_products/output_products, the
PlanValidator derives deterministic edges by exact product-name matching,
pulling terminal/merge nodes to the correct level.
"""
import pytest

from orchestrator.plan_validator import PlanValidator, PlanValidationError


def _node(nid, agent_type="generator", ins=None, outs=None):
    return {
        "id": nid,
        "agent_type": agent_type,
        "task": f"task for {nid}",
        "input_products": ins or [],
        "output_products": outs or [],
    }


class TestProductDrivenEdgeDerivation:
    def test_terminal_node_pulled_after_all_producers(self):
        """Core #1133 fix: push_pr is pulled to the correct level even
        when the LLM placed it alongside impl nodes (no deps declared)."""
        nodes = [
            _node("plan", "planner", outs=["plan"]),
            _node("impl_fix", outs=["impl_fix_result"]),
            _node("impl_tests", outs=["impl_tests_result"]),
            _node("eval", "evaluator",
                  ins=["impl_fix_result", "impl_tests_result"],
                  outs=["eval_report"]),
            _node("push_pr",
                  ins=["impl_fix_result", "impl_tests_result", "eval_report"],
                  outs=["pull_request"]),
        ]
        # LLM put push_pr at the SAME level as impl_fix/impl_tests (no edge)
        edges = [
            {"from": "plan", "to": "impl_fix"},
            {"from": "plan", "to": "impl_tests"},
        ]
        validator = PlanValidator()
        result = validator.validate({"nodes": nodes, "edges": edges})

        edge_set = {(e["from"], e["to"]) for e in result["edges"]}
        # push_pr must depend on all three producers
        assert ("impl_fix", "push_pr") in edge_set
        assert ("impl_tests", "push_pr") in edge_set
        assert ("eval", "push_pr") in edge_set
        # eval must depend on impl_fix and impl_tests
        assert ("impl_fix", "eval") in edge_set
        assert ("impl_tests", "eval") in edge_set

    def test_topological_levels_terminal_last(self):
        """After product derivation the terminal node sits in the last level."""
        nodes = [
            _node("plan", "planner", outs=["plan"]),
            _node("impl_fix", outs=["fix"]),
            _node("push_pr", ins=["fix"], outs=["pr"]),
        ]
        edges = []  # LLM produced nothing
        validator = PlanValidator()
        result = validator.validate({"nodes": nodes, "edges": edges})

        from core.dag_models import DAG, DAGNode
        dag = DAG()
        for n in nodes:
            dag = dag.add_node(DAGNode(
                id=n["id"], agent_type=n["agent_type"],
                task_description=n["task"],
            ))
        for e in result["edges"]:
            dag = dag.add_edge(e["from"], e["to"])
        levels = dag.topological_levels()
        assert levels[-1] == ["push_pr"], f"push_pr should be terminal, got {levels}"
        assert "push_pr" not in levels[0]

    def test_duplicate_output_product_raises(self):
        """Defense 1: same output_product on two nodes → PlanValidationError."""
        nodes = [
            _node("a", outs=["shared"]),
            _node("b", outs=["shared"]),
            _node("c", ins=["shared"]),
        ]
        validator = PlanValidator()
        with pytest.raises(PlanValidationError, match="Duplicate output_product"):
            validator.validate({"nodes": nodes, "edges": []})

    def test_unsatisfied_input_product_warns(self):
        """Defense 2: input_product with no producer → warning."""
        nodes = [
            _node("a", ins=["missing_product"], outs=["a_out"]),
        ]
        validator = PlanValidator()
        validator.validate({"nodes": nodes, "edges": []})
        assert any(
            "missing_product" in w and "no producer" in w
            for w in validator.warnings
        )

    def test_backward_compat_no_products(self):
        """When no node declares products, edges are unchanged."""
        nodes = [
            {"id": "plan", "agent_type": "planner", "task": "p"},
            {"id": "impl", "agent_type": "generator", "task": "i"},
            {"id": "eval", "agent_type": "evaluator", "task": "e"},
        ]
        edges = [
            {"from": "plan", "to": "impl"},
            {"from": "impl", "to": "eval"},
        ]
        validator = PlanValidator()
        result = validator.validate({"nodes": nodes, "edges": edges})
        assert {(e["from"], e["to"]) for e in result["edges"]} == {
            ("plan", "impl"), ("impl", "eval"),
        }
        assert not any("Derived" in w and "#1133" in w for w in validator.warnings)

    def test_supplements_not_replaces(self):
        """Product derivation only ADDS edges; existing LLM edges preserved."""
        nodes = [
            _node("a", outs=["a_out"]),
            _node("b", ins=["a_out"], outs=["b_out"]),
        ]
        edges = [{"from": "a", "to": "b"}]  # already correct
        validator = PlanValidator()
        result = validator.validate({"nodes": nodes, "edges": edges})
        pairs = [(e["from"], e["to"]) for e in result["edges"]]
        assert ("a", "b") in pairs
        assert pairs.count(("a", "b")) == 1  # no duplicate

    def test_self_product_input_skipped(self):
        """A node consuming its own output_product does not self-loop."""
        nodes = [
            _node("a", ins=["a_out"], outs=["a_out"]),
        ]
        validator = PlanValidator()
        result = validator.validate({"nodes": nodes, "edges": []})
        assert result["edges"] == []

    def test_product_edges_excluded_from_hub_softening(self):
        """A producer feeding >=4 consumers via products keeps hard edges
        (product edges are genuine data deps, not speculative fan-out)."""
        consumers = [
            _node(f"c{i}", ins=["core"], outs=[f"c{i}_out"])
            for i in range(5)
        ]
        nodes = [_node("prod", outs=["core"])] + consumers
        validator = PlanValidator()
        result = validator.validate({"nodes": nodes, "edges": []})
        for e in result["edges"]:
            if e["from"] == "prod":
                assert e["dependency_type"] == "hard", (
                    f"product edge {e['from']}→{e['to']} should stay hard"
                )
