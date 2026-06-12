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


def _make_hub_plan(num_generators: int = 5, explicit_deps: bool = False):
    """Helper: create a plan with 1 foundation + N generators + 1 evaluator."""
    nodes = [
        {"id": "plan", "agent_type": "planner", "task": "Plan the project"},
        {"id": "impl_foundation", "agent_type": "generator",
         "task": "Create foundation and shared utilities"},
        {"id": "eval", "agent_type": "evaluator", "task": "Evaluate outputs"},
    ]
    edges = [
        {"from": "plan", "to": "impl_foundation", "dependency_type": "hard"},
        {"from": "plan", "to": "eval", "dependency_type": "hard"},
    ]
    for i in range(num_generators):
        nid = f"impl_gen_{i}"
        nodes.append({
            "id": nid,
            "agent_type": "generator",
            "task": f"Implement feature {i}",
        })
        if explicit_deps:
            edges.append({
                "from": "impl_foundation", "to": nid,
                "dependency_type": "hard",
            })
        edges.append({
            "from": nid, "to": "eval",
            "dependency_type": "hard",
        })
    return {"nodes": nodes, "edges": edges}


def test_foundation_auto_add_not_softened_by_hub():
    """Auto-added foundation deps remain hard even with fan-out >= 4 (#1043)."""
    plan = _make_hub_plan(num_generators=5, explicit_deps=False)
    v = PlanValidator()
    result = v.validate(plan)

    foundation_edges = [
        e for e in result["edges"]
        if e["from"] == "impl_foundation" and e["to"].startswith("impl_gen_")
    ]
    assert len(foundation_edges) == 5
    for edge in foundation_edges:
        assert edge["dependency_type"] == "hard", (
            f"Auto-added foundation edge {edge['from']} -> {edge['to']} "
            f"should remain hard, got {edge['dependency_type']}"
        )

    preserved = [w for w in v.warnings if "Preserved" in w and "#1043" in w]
    assert preserved, f"Expected preservation warning, got: {v.warnings}"


def test_hub_softening_still_works_for_explicit_edges():
    """Explicitly-planned hub edges should still be softened (#959)."""
    plan = _make_hub_plan(num_generators=5, explicit_deps=True)
    v = PlanValidator()
    result = v.validate(plan)

    foundation_edges = [
        e for e in result["edges"]
        if e["from"] == "impl_foundation" and e["to"].startswith("impl_gen_")
    ]
    assert len(foundation_edges) == 5
    softened = [e for e in foundation_edges if e["dependency_type"] == "soft"]
    assert len(softened) == 5


def test_mixed_auto_and_explicit_hub():
    """Mix of auto-added and explicit edges: only explicit softened."""
    nodes = [
        {"id": "plan", "agent_type": "planner", "task": "Plan"},
        {"id": "impl_foundation", "agent_type": "generator",
         "task": "Create foundation"},
        {"id": "eval", "agent_type": "evaluator", "task": "Evaluate"},
        # gen_0..gen_3: explicit deps on foundation (4 explicit, meets threshold)
        {"id": "impl_gen_0", "agent_type": "generator",
         "task": "Implement feature 0"},
        {"id": "impl_gen_1", "agent_type": "generator",
         "task": "Implement feature 1"},
        {"id": "impl_gen_2", "agent_type": "generator",
         "task": "Implement feature 2"},
        {"id": "impl_gen_3", "agent_type": "generator",
         "task": "Implement feature 3"},
        # gen_4, gen_5: NO dep on foundation (auto-added)
        {"id": "impl_gen_4", "agent_type": "generator",
         "task": "Implement feature 4"},
        {"id": "impl_gen_5", "agent_type": "generator",
         "task": "Implement feature 5"},
    ]
    edges = [
        {"from": "plan", "to": "impl_foundation", "dependency_type": "hard"},
        {"from": "plan", "to": "eval", "dependency_type": "hard"},
        # Explicit deps for gen_0..gen_3
        {"from": "impl_foundation", "to": "impl_gen_0",
         "dependency_type": "hard"},
        {"from": "impl_foundation", "to": "impl_gen_1",
         "dependency_type": "hard"},
        {"from": "impl_foundation", "to": "impl_gen_2",
         "dependency_type": "hard"},
        {"from": "impl_foundation", "to": "impl_gen_3",
         "dependency_type": "hard"},
    ]
    for i in range(6):
        edges.append({
            "from": f"impl_gen_{i}", "to": "eval",
            "dependency_type": "hard",
        })

    v = PlanValidator()
    result = v.validate({"nodes": nodes, "edges": edges})

    foundation_edges = {
        e["to"]: e
        for e in result["edges"]
        if e["from"] == "impl_foundation"
        and e["to"].startswith("impl_gen_")
    }

    # Explicit (gen_0..gen_3): 4 explicit edges meet hub threshold -> softened
    for i in range(4):
        nid = f"impl_gen_{i}"
        assert foundation_edges[nid]["dependency_type"] == "soft", (
            f"Explicit edge to {nid} should be softened"
        )

    # Auto-added (gen_4, gen_5): excluded from softening -> remain hard
    for i in range(4, 6):
        nid = f"impl_gen_{i}"
        assert foundation_edges[nid]["dependency_type"] == "hard", (
            f"Auto-added edge to {nid} should remain hard"
        )


# -- Publishing node dependency tests (rule 19) --


class TestPublishingNodeDependencies:
    """Rule 19: publishing nodes must depend on all impl+eval nodes."""

    def test_push_pr_missing_deps_auto_added(self):
        """push_pr without deps on impl nodes gets auto-corrected."""
        plan = {
            "nodes": [
                {"id": "plan", "agent_type": "planner", "task": "Plan"},
                {"id": "impl_fix", "agent_type": "generator",
                 "task": "Implement fix"},
                {"id": "impl_tests", "agent_type": "generator",
                 "task": "Write tests"},
                {"id": "push_pr", "agent_type": "generator",
                 "task": "Push PR to GitHub"},
            ],
            "edges": [
                {"from": "plan", "to": "impl_fix"},
                {"from": "plan", "to": "impl_tests"},
                {"from": "plan", "to": "push_pr"},  # WRONG: parallel with impl
            ],
        }
        v = PlanValidator()
        result = v.validate(plan)

        pub_edges = [
            e for e in result["edges"] if e["to"] == "push_pr"
        ]
        # Should now have edges from plan, impl_fix, impl_tests
        sources = {e["from"] for e in pub_edges}
        assert "impl_fix" in sources, "push_pr should depend on impl_fix"
        assert "impl_tests" in sources, "push_pr should depend on impl_tests"
        assert any("auto-added" in w for w in v.warnings)

    def test_push_pr_already_correct_no_change(self):
        """push_pr already depending on all impl nodes — no auto-add."""
        plan = {
            "nodes": [
                {"id": "plan", "agent_type": "planner", "task": "Plan"},
                {"id": "impl_fix", "agent_type": "generator",
                 "task": "Implement fix"},
                {"id": "eval", "agent_type": "evaluator", "task": "Evaluate"},
                {"id": "push_pr", "agent_type": "generator",
                 "task": "Push PR to GitHub"},
            ],
            "edges": [
                {"from": "plan", "to": "impl_fix"},
                {"from": "impl_fix", "to": "eval"},
                {"from": "eval", "to": "push_pr"},
            ],
        }
        v = PlanValidator()
        result = v.validate(plan)

        pub_edges = [
            e for e in result["edges"] if e["to"] == "push_pr"
        ]
        # Only the explicit eval -> push_pr edge, no auto-added
        assert len(pub_edges) == 1
        assert pub_edges[0]["from"] == "eval"
        assert not any("auto-added" in w for w in v.warnings)

    def test_no_publishing_node_no_change(self):
        """Plans without publishing nodes are unaffected."""
        plan = {
            "nodes": [
                {"id": "plan", "agent_type": "planner", "task": "Plan"},
                {"id": "impl", "agent_type": "generator",
                 "task": "Implement"},
                {"id": "eval", "agent_type": "evaluator", "task": "Evaluate"},
            ],
            "edges": [
                {"from": "plan", "to": "impl"},
                {"from": "impl", "to": "eval"},
            ],
        }
        v = PlanValidator()
        result = v.validate(plan)
        assert len(result["edges"]) == 2

    def test_publishing_detected_by_node_id(self):
        """Publishing nodes detected by ID pattern (push_pr, deploy, release)."""
        plan = {
            "nodes": [
                {"id": "plan", "agent_type": "planner", "task": "Plan"},
                {"id": "impl", "agent_type": "generator",
                 "task": "Implement"},
                {"id": "deploy", "agent_type": "generator",
                 "task": "Ship it"},
            ],
            "edges": [
                {"from": "plan", "to": "impl"},
                {"from": "plan", "to": "deploy"},
            ],
        }
        v = PlanValidator()
        result = v.validate(plan)

        deploy_edges = [
            e for e in result["edges"] if e["to"] == "deploy"
        ]
        sources = {e["from"] for e in deploy_edges}
        assert "impl" in sources
        assert any("auto-added" in w for w in v.warnings)
