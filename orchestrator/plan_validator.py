"""
Plan validator: structural validation and auto-fix for orchestrator plans.

Validates:
- Duplicate node IDs → auto-rename
- Dangling edges → remove
- Cycle detection → raise PlanValidationError
"""

from __future__ import annotations


class PlanValidationError(Exception):
    """Raised when a plan has unfixable structural errors (e.g. cycles)."""


class PlanValidator:
    """Validates and optionally auto-fixes orchestrator plan data."""

    def __init__(self, auto_fix: bool = False) -> None:
        self.auto_fix = auto_fix
        self.warnings: list[str] = []

    def validate(self, plan_data: dict) -> dict:
        """Validate plan structure. Returns (possibly fixed) plan data."""
        self.warnings.clear()
        nodes = plan_data.get("nodes", [])
        edges = plan_data.get("edges", [])

        node_ids = {n.get("id") for n in nodes if n.get("id")}

        # 1. Check for duplicate node IDs
        seen: dict[str, int] = {}
        for node in nodes:
            nid = node.get("id")
            if not nid:
                continue
            if nid in seen:
                if self.auto_fix:
                    new_id = f"{nid}_{seen[nid]}"
                    node["id"] = new_id
                    self.warnings.append(f"Renamed duplicate node: {nid} → {new_id}")
                    for edge in edges:
                        if edge.get("from") == nid:
                            edge["from"] = new_id
                        if edge.get("to") == nid:
                            edge["to"] = new_id
                    node_ids.add(new_id)
                else:
                    raise PlanValidationError(f"Duplicate node ID: {nid}")
            seen[nid] = seen.get(nid, 0) + 1

        # 2. Remove dangling edges
        valid_edges = []
        for edge in edges:
            from_id = edge.get("from")
            to_id = edge.get("to")
            if from_id in node_ids and to_id in node_ids:
                valid_edges.append(edge)
            else:
                if self.auto_fix:
                    self.warnings.append(
                        f"Removed dangling edge: {from_id} → {to_id}"
                    )
                else:
                    raise PlanValidationError(
                        f"Dangling edge: {from_id} → {to_id}"
                    )
        plan_data["edges"] = valid_edges

        # 3. Cycle detection via DFS
        adj: dict[str, list[str]] = {nid: [] for nid in node_ids}
        for edge in valid_edges:
            adj.get(edge["from"], []).append(edge["to"])

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
