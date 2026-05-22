"""Tests for #740: PlanValidator enforces foundation node dependencies.

Verifies that:
1. Foundation node is identified by task keywords
2. Impl nodes without foundation dependency get auto-fixed
3. Nodes already depending on foundation are not modified
4. No foundation node in plan → no changes
5. Foundation identified by node ID (not just task description)
"""
from orchestrator.plan_validator import PlanValidator


def _base_plan():
    """Return a minimal plan with foundation + impl nodes."""
    return {
        "nodes": [
            {"id": "plan_1", "agent_type": "planner",
             "task": "Plan the project"},
            {"id": "impl_foundation", "agent_type": "generator",
             "task": "Create foundation: database models and shared utilities"},
            {"id": "impl_auth", "agent_type": "generator",
             "task": "Implement authentication module"},
            {"id": "impl_rooms", "agent_type": "generator",
             "task": "Implement rooms and scenes"},
        ],
        "edges": [
            {"from": "plan_1", "to": "impl_foundation",
             "dependency_type": "hard"},
        ],
        "reasoning": "test plan",
    }


def test_foundation_identified_and_deps_added():
    """Impl nodes without foundation dep get auto-fixed (#740)."""
    validator = PlanValidator(auto_fix=True)
    result = validator.validate(_base_plan())

    edge_pairs = {(e["from"], e["to"]) for e in result["edges"]}
    assert ("impl_foundation", "impl_auth") in edge_pairs
    assert ("impl_foundation", "impl_rooms") in edge_pairs
    assert any("#740" in w for w in validator.warnings)


def test_existing_foundation_deps_not_duplicated():
    """Nodes already depending on foundation are not modified (#740)."""
    plan = _base_plan()
    # Add existing dependency for impl_auth
    plan["edges"].append({
        "from": "impl_foundation", "to": "impl_auth",
        "dependency_type": "hard",
    })

    validator = PlanValidator(auto_fix=True)
    result = validator.validate(plan)

    # Count edges from foundation to impl_auth
    auth_edges = [
        e for e in result["edges"]
        if e["from"] == "impl_foundation" and e["to"] == "impl_auth"
    ]
    assert len(auth_edges) == 1  # Not duplicated


def test_no_foundation_node_no_changes():
    """Plan without foundation node → no edges added (#740)."""
    plan = {
        "nodes": [
            {"id": "plan_1", "agent_type": "planner",
             "task": "Plan the project"},
            {"id": "impl_1", "agent_type": "generator",
             "task": "Implement feature A"},
            {"id": "impl_2", "agent_type": "generator",
             "task": "Implement feature B"},
        ],
        "edges": [],
        "reasoning": "test",
    }

    validator = PlanValidator(auto_fix=True)
    result = validator.validate(plan)

    assert len(result["edges"]) == 0


def test_foundation_identified_by_node_id():
    """Foundation node identified by ID even without task keywords (#740)."""
    plan = {
        "nodes": [
            {"id": "impl_base", "agent_type": "generator",
             "task": "Create database models and config"},
            {"id": "impl_api", "agent_type": "generator",
             "task": "Implement API endpoints"},
        ],
        "edges": [],
        "reasoning": "test",
    }

    validator = PlanValidator(auto_fix=True)
    result = validator.validate(plan)

    edge_pairs = {(e["from"], e["to"]) for e in result["edges"]}
    assert ("impl_base", "impl_api") in edge_pairs


def test_non_generator_nodes_not_affected():
    """Planner and evaluator nodes don't get foundation deps (#740)."""
    plan = _base_plan()
    plan["nodes"].append({
        "id": "eval_1", "agent_type": "evaluator",
        "task": "Evaluate all outputs",
    })

    validator = PlanValidator(auto_fix=True)
    result = validator.validate(plan)

    # No edge from foundation to evaluator
    eval_edges = [
        e for e in result["edges"]
        if e["to"] == "eval_1" and e["from"] == "impl_foundation"
    ]
    assert len(eval_edges) == 0
