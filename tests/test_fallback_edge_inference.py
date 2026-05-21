"""Tests for #689: fallback edge inference when LLM produces empty edges.

When the LLM generates a DAG with no edges, _plan_to_dag should infer
dependency edges from agent types:
1. All nodes depend on planner nodes
2. Non-generator, non-planner nodes depend on generators
3. Evaluators depend on generators
"""
from core.models import DAG, DAGNode
from orchestrator.intelligent_orchestrator import IntelligentOrchestrator


def _make_dag(nodes, edges=None):
    """Create a DAG with given nodes and edges."""
    dag = DAG(reasoning="test")
    for nid, agent_type in nodes:
        dag.add_node(DAGNode(
            id=nid,
            agent_type=agent_type,
            task_description=f"task for {nid}",
        ))
    if edges:
        for from_n, to_n in edges:
            dag.add_edge(from_n, to_n)
    return dag


class TestFallbackEdgeInference:
    """Verify _infer_fallback_edges creates correct edges."""

    def test_empty_edges_infers_dependencies(self):
        """When edges is empty, fallback edges are inferred (#689)."""
        dag = _make_dag([
            ("plan", "planner"),
            ("impl_core", "generator"),
            ("impl_cli", "generator"),
            ("impl_tests", "generator"),
            ("eval", "evaluator"),
        ])
        result = IntelligentOrchestrator._infer_fallback_edges(dag)

        edge_pairs = {(e.from_node, e.to_node) for e in result.edges}

        # plan → all generators and evaluator
        assert ("plan", "impl_core") in edge_pairs
        assert ("plan", "impl_cli") in edge_pairs
        assert ("plan", "impl_tests") in edge_pairs
        assert ("plan", "eval") in edge_pairs

        # generators → evaluator
        assert ("impl_core", "eval") in edge_pairs
        assert ("impl_cli", "eval") in edge_pairs
        assert ("impl_tests", "eval") in edge_pairs

    def test_no_duplicate_edges(self):
        """Existing edges are not duplicated."""
        dag = _make_dag([
            ("plan", "planner"),
            ("impl", "generator"),
            ("eval", "evaluator"),
        ], edges=[("plan", "impl")])
        result = IntelligentOrchestrator._infer_fallback_edges(dag)

        plan_impl_count = sum(
            1 for e in result.edges
            if e.from_node == "plan" and e.to_node == "impl"
        )
        assert plan_impl_count == 1  # Not duplicated

    def test_plan_only_dag(self):
        """Single planner node with no edges — nothing to infer."""
        dag = _make_dag([("plan", "planner")])
        result = IntelligentOrchestrator._infer_fallback_edges(dag)
        assert len(result.edges) == 0

    def test_generators_only(self):
        """Multiple generators with no planner — no edges between them."""
        dag = _make_dag([
            ("impl_a", "generator"),
            ("impl_b", "generator"),
        ])
        result = IntelligentOrchestrator._infer_fallback_edges(dag)
        assert len(result.edges) == 0

    def test_evaluator_depends_on_all_generators(self):
        """Evaluator gets edges from all generators."""
        dag = _make_dag([
            ("impl_a", "generator"),
            ("impl_b", "generator"),
            ("impl_c", "generator"),
            ("eval", "evaluator"),
        ])
        result = IntelligentOrchestrator._infer_fallback_edges(dag)

        edge_pairs = {(e.from_node, e.to_node) for e in result.edges}
        assert ("impl_a", "eval") in edge_pairs
        assert ("impl_b", "eval") in edge_pairs
        assert ("impl_c", "eval") in edge_pairs

    def test_r13_reproduction_scenario(self):
        """Reproduce R13: 7 nodes, no edges — all should get proper ordering."""
        dag = _make_dag([
            ("plan", "planner"),
            ("impl_foundation", "generator"),
            ("impl_html_toc", "generator"),
            ("impl_cli", "generator"),
            ("impl_pdf_rst_themes", "generator"),
            ("impl_tests", "generator"),
            ("eval", "evaluator"),
        ])
        result = IntelligentOrchestrator._infer_fallback_edges(dag)

        edge_pairs = {(e.from_node, e.to_node) for e in result.edges}

        # plan → all others
        for nid in [
            "impl_foundation", "impl_html_toc", "impl_cli",
            "impl_pdf_rst_themes", "impl_tests", "eval",
        ]:
            assert ("plan", nid) in edge_pairs, f"Missing plan → {nid}"

        # All generators → eval
        for nid in [
            "impl_foundation", "impl_html_toc", "impl_cli",
            "impl_pdf_rst_themes", "impl_tests",
        ]:
            assert (nid, "eval") in edge_pairs, f"Missing {nid} → eval"

        # Generators should NOT depend on each other
        assert ("impl_foundation", "impl_cli") not in edge_pairs

    def test_preserves_existing_edges(self):
        """Existing edges from plan are preserved alongside inferred ones."""
        dag = _make_dag([
            ("plan", "planner"),
            ("impl_a", "generator"),
            ("impl_b", "generator"),
            ("eval", "evaluator"),
        ], edges=[("plan", "impl_a"), ("impl_a", "impl_b")])
        result = IntelligentOrchestrator._infer_fallback_edges(dag)

        edge_pairs = {(e.from_node, e.to_node) for e in result.edges}
        # Original edges preserved
        assert ("plan", "impl_a") in edge_pairs
        assert ("impl_a", "impl_b") in edge_pairs
        # Inferred edges added
        assert ("plan", "impl_b") in edge_pairs
        assert ("plan", "eval") in edge_pairs
        assert ("impl_a", "eval") in edge_pairs
        assert ("impl_b", "eval") in edge_pairs
