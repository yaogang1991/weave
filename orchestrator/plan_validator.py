"""
Plan validator: structural validation for orchestrator plans.

Validates:
- Duplicate node IDs → raise PlanValidationError (no auto-fix, DAG semantics ambiguous)
- Dangling edges → raise PlanValidationError
- Cycle detection → raise PlanValidationError

Design: validation-only, never silently mutates the plan. This avoids
introducing subtle semantic changes when duplicate IDs make edge
ownership ambiguous. Instead, the orchestrator is asked to replan.
"""

from __future__ import annotations


class PlanValidationError(Exception):
    """Raised when a plan has structural errors."""


class PlanValidator:
    """Validates orchestrator plan structure. No mutations."""

    def __init__(self, auto_fix: bool = False) -> None:
        # auto_fix is accepted for API compat but validation-only is always used
        self.auto_fix = auto_fix
        self.warnings: list[str] = []

    def validate(self, plan_data: dict) -> dict:
        """Validate plan structure. Returns plan_data unchanged on success.

        Raises PlanValidationError on any structural error.
        """
        self.warnings.clear()
        nodes = plan_data.get("nodes", [])
        edges = plan_data.get("edges", [])

        node_ids = set()
        for node in nodes:
            nid = node.get("id")
            if not nid:
                continue
            if nid in node_ids:
                raise PlanValidationError(f"Duplicate node ID: {nid}")
            node_ids.add(nid)

        # Check for dangling edges
        for edge in edges:
            from_id = edge.get("from")
            to_id = edge.get("to")
            if from_id not in node_ids:
                raise PlanValidationError(
                    f"Dangling edge: source node '{from_id}' does not exist"
                )
            if to_id not in node_ids:
                raise PlanValidationError(
                    f"Dangling edge: target node '{to_id}' does not exist"
                )

        # Cycle detection via DFS
        adj: dict[str, list[str]] = {nid: [] for nid in node_ids}
        for edge in edges:
            adj[edge["from"]].append(edge["to"])

        visited: set[str] = set()
        in_stack: set[str] = set()

        def has_cycle(node_id: str) -> bool:
            visited.add(node_id)
            in_stack.add(node_id)
            for neighbor in adj.get(node_id, []):
                if neighbor in in_stack:
                    return True
                if neighbor not in visited and has_cycle(neighbor):
                    return True
            in_stack.remove(node_id)
            return False

        for nid in node_ids:
            if nid not in visited:
                if has_cycle(nid):
                    raise PlanValidationError("Plan contains a cycle")

        return plan_data
