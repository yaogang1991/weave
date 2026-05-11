"""Plan validation and auto-fix for Orchestrator output."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class PlanValidationError(Exception):
    """Raised when a plan fails structural validation."""


class PlanValidator:
    """Validates and optionally auto-fixes OrchestratorPlan data.

    Checks structural invariants (required fields, node consistency)
    and applies safe fixes when ``auto_fix=True``.
    """

    def __init__(self, auto_fix: bool = False) -> None:
        self.auto_fix = auto_fix
        self.warnings: list[str] = []

    def validate(self, plan_data: dict[str, Any]) -> dict[str, Any]:
        """Validate and optionally fix a plan dict. Returns the (fixed) dict."""
        self.warnings = []

        nodes = plan_data.get("nodes", [])
        if not nodes:
            self.warnings.append("Plan has no nodes")
            return plan_data

        node_ids = {n.get("id") for n in nodes}

        # Check dependency references
        for node in nodes:
            for dep in node.get("depends_on", []):
                if dep not in node_ids:
                    msg = f"Node '{node.get('id')}' depends on unknown node '{dep}'"
                    if self.auto_fix:
                        node["depends_on"] = [
                            d for d in node.get("depends_on", []) if d in node_ids
                        ]
                        self.warnings.append(f"Auto-fixed: removed {msg}")
                    else:
                        raise PlanValidationError(msg)

        return plan_data
