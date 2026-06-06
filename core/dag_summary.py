"""DAG execution summary computation.

Extracted from DAGExecutionEngine.get_execution_summary and
control_plane.service._compute_summary to eliminate code duplication (#1089).
"""
from __future__ import annotations

from typing import Any

from core.models import DAG, NodeStatus


def compute_dag_summary(
    dag: DAG,
    *,
    include_implementation_breakdown: bool = True,
    include_token_usage: bool = True,
    include_node_details: bool = True,
    budget_dict: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute a summary of DAG execution results.

    This is the single source of truth for DAG summary computation,
    used by both the engine and the service layer.

    Args:
        dag: The DAG to summarize.
        include_implementation_breakdown: Include impl_success/impl_total counts.
        include_token_usage: Include token usage aggregation.
        include_node_details: Include per-node detail dict.
        budget_dict: Optional budget summary dict to include.

    Returns:
        Summary dict with node counts, success metrics, and optional details.
    """
    total = len(dag.nodes)
    success = sum(1 for n in dag.nodes.values() if n.status == NodeStatus.SUCCESS)
    partial_pass = sum(1 for n in dag.nodes.values() if n.status == NodeStatus.PARTIAL_PASS)
    warned = sum(1 for n in dag.nodes.values() if n.status == NodeStatus.WARNED)
    failed = sum(1 for n in dag.nodes.values() if n.status == NodeStatus.FAILED)
    skipped = sum(1 for n in dag.nodes.values() if n.status == NodeStatus.SKIPPED)

    # #676: Evaluator failures are non-critical (informational only).
    # Exclude them from all_succeeded so implementation success
    # is not masked by evaluation timeouts.
    non_eval_failed = sum(
        1 for n in dag.nodes.values()
        if n.status == NodeStatus.FAILED and n.agent_type != "evaluator"
    )

    summary: dict[str, Any] = {
        "total_nodes": total,
        "success": success,
        "partial_pass": partial_pass,
        "warned": warned,
        "failed": failed,
        "skipped": skipped,
        "all_succeeded": (
            non_eval_failed == 0
            and skipped == 0
            and partial_pass == 0
        ),
    }

    # #724: Break down success by agent role
    if include_implementation_breakdown:
        impl_types = {"generator", "worker"}
        summary["implementation_success"] = sum(
            1 for n in dag.nodes.values()
            if n.status == NodeStatus.SUCCESS
            and n.agent_type in impl_types
        )
        summary["implementation_total"] = sum(
            1 for n in dag.nodes.values()
            if n.agent_type in impl_types
        )

    # Per-node details
    if include_node_details:
        summary["node_details"] = {
            nid: {
                "status": n.status.value,
                "agent": n.agent_type,
                "duration_ms": (
                    (n.completed_at - n.started_at).total_seconds() * 1000
                    if n.completed_at and n.started_at else None
                ),
                **(
                    {"eval_feedback": n.eval_feedback}
                    if n.eval_feedback else {}
                ),
            }
            for nid, n in dag.nodes.items()
        }

    # M4.2: Token usage aggregation
    if include_token_usage:
        total_input = 0
        total_output = 0
        for n in dag.nodes.values():
            tu = n.token_usage if hasattr(n, "token_usage") else {}
            total_input += tu.get("input_tokens", 0)
            total_output += tu.get("output_tokens", 0)
        summary["token_usage"] = {
            "total_input_tokens": total_input,
            "total_output_tokens": total_output,
            "total_tokens": total_input + total_output,
        }
        # M4.6: Aggregate actual_tokens
        actual_total = sum(
            n.actual_tokens for n in dag.nodes.values()
            if hasattr(n, "actual_tokens")
        )
        if actual_total > 0:
            summary["token_usage"]["actual_tokens_total"] = actual_total

    if budget_dict:
        summary["budget"] = budget_dict

    return summary
