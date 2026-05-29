"""Budget-aware DAG planning: distributes a total token budget across nodes.

Uses a weighted allocation strategy based on agent type:
  - planner nodes:   15%
  - generator nodes:  50%
  - evaluator nodes:  25%
  - other nodes:      10%

If a weight category has zero nodes, its share is redistributed
proportionally to the remaining categories.  Per-node budget within a
category is divided equally, then floored at a minimum of 1024 tokens.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.dag_models import DAG

logger = logging.getLogger(__name__)

# Allocation weights by agent type.
_AGENT_WEIGHTS: dict[str, float] = {
    "planner": 0.15,
    "generator": 0.50,
    "evaluator": 0.25,
}
_DEFAULT_WEIGHT: float = 0.10
_MIN_NODE_BUDGET: int = 1024


class BudgetPlanner:
    """Distributes a total token budget across DAG nodes by agent type."""

    def allocate_budget(self, dag: DAG, total_budget: int) -> DAG:
        """Set ``token_budget`` on each DAGNode according to weighted allocation.

        Parameters
        ----------
        dag:
            The execution DAG (mutated in-place via ``dag.update_node``).
        total_budget:
            Total token budget to distribute.  Must be > 0.

        Returns
        -------
        DAG
            The same DAG object (for chaining).
        """
        if total_budget <= 0 or not dag.nodes:
            return dag

        # Group nodes by weight category.
        groups: dict[float, list[str]] = {}
        for nid, node in dag.nodes.items():
            w = _AGENT_WEIGHTS.get(node.agent_type, _DEFAULT_WEIGHT)
            groups.setdefault(w, []).append(nid)

        # If any weight has zero nodes, redistribute its share.
        total_weight = sum(groups.keys())
        if total_weight == 0:
            return dag

        for nid, node in dag.nodes.items():
            w = _AGENT_WEIGHTS.get(node.agent_type, _DEFAULT_WEIGHT)
            share = total_budget * (w / total_weight)
            count = len(groups[w])
            per_node = max(int(share / count), _MIN_NODE_BUDGET)

            dag.update_node(nid, token_budget=per_node)
            logger.debug(
                "BudgetPlanner: node '%s' (type=%s) budget=%d",
                nid, node.agent_type, per_node,
            )

        logger.info(
            "BudgetPlanner: allocated %d tokens across %d nodes",
            total_budget, len(dag.nodes),
        )
        return dag

    def check_budget_feasibility(self, dag: DAG) -> list[str]:
        """Return warnings for nodes likely to exceed their budget.

        A node is flagged when its ``estimated_tokens`` (set by
        TokenEstimator) exceeds 90% of its ``token_budget``.

        Returns
        -------
        list[str]
            Human-readable warning strings (empty if no issues).
        """
        warnings: list[str] = []
        threshold = 0.90

        for nid, node in dag.nodes.items():
            if node.estimated_tokens <= 0:
                continue
            if node.token_budget <= 0:
                continue
            ratio = node.estimated_tokens / node.token_budget
            if ratio > threshold:
                warnings.append(
                    f"Node '{nid}' (type={node.agent_type}) estimated at "
                    f"{node.estimated_tokens} tokens is {ratio:.0%} of its "
                    f"{node.token_budget}-token budget"
                )

        if warnings:
            logger.warning(
                "BudgetPlanner: %d feasibility warning(s)", len(warnings),
            )
        return warnings
