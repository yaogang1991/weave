"""DAG replan logic — merge, rewire, circuit breaker, serialization (#1008).

Extracted from core.dag_engine to keep the engine focused on the
level-by-level execution loop.  All methods are stateless helpers
that operate on DAG instances and return new instances where
appropriate (immutability convention).
"""

from __future__ import annotations

import logging
from typing import Any

from core.models import DAG, DAGEdge, NodeStatus
from core.provider_health import FailureCategory
from core.quality_gate import QualityGate

logger = logging.getLogger(__name__)


def classify_failure(error: str) -> FailureCategory:
    """Classify a node failure by its error message for provider health tracking.

    Only API-level errors (connection failures, rate limits) should count
    toward the provider health threshold. Evaluation failures, timeouts,
    and stall detections are local issues, not provider issues (#921/#924).
    """
    if not error:
        return FailureCategory.API_ERROR  # conservative: unknown = API error
    error_lower = error.lower()
    if "rate_limit" in error_lower or "rate limit" in error_lower or "429" in error_lower:
        return FailureCategory.RATE_LIMIT
    if "stall" in error_lower:
        return FailureCategory.STALL
    if "eval" in error_lower:
        return FailureCategory.EVALUATION
    if "timeout" in error_lower:
        return FailureCategory.TIMEOUT
    return FailureCategory.API_ERROR  # conservative: unclassified = API error


# ---------------------------------------------------------------------------
# DAG merge & rewire (replan support)
# ---------------------------------------------------------------------------

def merge_dag_results(old_dag: DAG, new_dag: DAG) -> DAG:
    """Merge two DAGs, preserving successful node results from *old_dag*.

    For each node that succeeded in *old_dag* and also exists in *new_dag*,
    copy over its status, result, output_artifacts, and timestamps so the
    re-executed plan does not re-run already-completed work.

    Nodes in *old_dag* that don't exist in *new_dag* are also preserved
    so the execution summary counts ALL nodes, not just the replan
    subset (#720).  Their edges are also preserved so
    ``topological_levels()`` orders them correctly (#728).

    Returns a new DAG — original DAGs are not modified.
    """
    merged = new_dag
    for node_id, node in old_dag.nodes.items():
        if node_id in merged.nodes:
            if QualityGate.is_terminal_success(node.status):
                merged.update_node(
                    node_id,
                    status=node.status,
                    result=node.result,
                    output_artifacts=node.output_artifacts,
                    started_at=node.started_at,
                    completed_at=node.completed_at,
                )
        else:
            merged = merged.add_node(node.model_copy())

    # #728: Preserve old edges for nodes carried over.
    merged_edge_set = {(e.from_node, e.to_node) for e in merged.edges}
    new_edges = list(merged.edges)
    for edge in old_dag.edges:
        if (
            edge.from_node in merged.nodes
            and edge.to_node in merged.nodes
            and (edge.from_node, edge.to_node) not in merged_edge_set
        ):
            new_edges.append(edge.model_copy())
            merged_edge_set.add((edge.from_node, edge.to_node))

    if len(new_edges) != len(merged.edges):
        merged = merged.model_copy(update={"edges": new_edges})

    return merged


def rewire_replacement_edges(
    merged: DAG, old_dag: DAG, new_dag: DAG, failed_id: str,
) -> DAG:
    """Rewire downstream edges from *failed_id* to its replacement (#775).

    When replan generates a replacement node (e.g., plan_v2 replacing
    failed plan), downstream dependencies still point to the original.
    This detects the replacement by matching ``agent_type`` and rewires
    edges.

    Returns a new DAG with rewired edges — original DAG is not modified.
    """
    if failed_id not in old_dag.nodes:
        return merged

    failed_node = old_dag.nodes[failed_id]
    old_node_ids = set(old_dag.nodes.keys())

    candidates = [
        nid for nid, node in new_dag.nodes.items()
        if nid not in old_node_ids and node.agent_type == failed_node.agent_type
    ]
    if not candidates:
        return merged

    replacement_id = candidates[0]

    existing = {(e.from_node, e.to_node) for e in merged.edges}
    new_edges: list[DAGEdge] = []
    edges_added: list[DAGEdge] = []

    for edge in merged.edges:
        if edge.from_node == failed_id and edge.to_node in merged.nodes:
            new_key = (replacement_id, edge.to_node)
            if new_key not in existing:
                new_edge = edge.model_copy(update={"from_node": replacement_id})
                edges_added.append(new_edge)
                new_edges.append(new_edge)
                existing.add(new_key)
            # Skip the old edge (remove it by not appending)
        else:
            new_edges.append(edge)

    if edges_added:
        logger.info(
            "Rewired %d downstream edges from %s to replacement %s (#775)",
            len(edges_added), failed_id, replacement_id,
        )

    if len(new_edges) != len(merged.edges) or edges_added:
        return merged.model_copy(update={"edges": new_edges})

    return merged


# ---------------------------------------------------------------------------
# Evaluator target resolution
# ---------------------------------------------------------------------------

def find_evaluator_target(dag: DAG, eval_node_id: str) -> str | None:
    """Find the generator node that an evaluator is responsible for assessing.

    Heuristic: look for a generator node that is a direct dependency
    (i.e. has an edge → evaluator) and whose ID/domain matches the evaluator.
    """
    # Candidate 1: direct upstream generator with an edge to the evaluator
    candidates = [
        e.from_node for e in dag.edges
        if e.to_node == eval_node_id
        and dag.nodes[e.from_node].agent_type == "generator"
    ]
    if len(candidates) == 1:
        return candidates[0]

    # Candidate 2: name-based matching among upstream nodes only
    eval_name = eval_node_id.lower().replace("eval_", "")
    upstream_ids = {e.from_node for e in dag.edges if e.to_node == eval_node_id}
    for nid in upstream_ids:
        node = dag.nodes[nid]
        if node.agent_type != "generator":
            continue
        gen_name = nid.lower().replace("impl_", "").replace("gen_", "")
        if gen_name == eval_name:
            return nid

    # Candidate 3: any direct upstream generator
    if candidates:
        return candidates[0]

    return None


# ---------------------------------------------------------------------------
# Planner circuit breaker
# ---------------------------------------------------------------------------

def check_planner_circuit_break(
    dag: DAG,
    failed_id: str,
    streak: int,
    threshold: int,
) -> tuple[bool, int]:
    """Check circuit breaker for futile replans (#750, #795).

    Returns ``(should_block, updated_streak)``.  *should_block* is True
    when replan should be blocked due to too many consecutive failures
    indicating a provider-level issue rather than task-specific problems.
    """
    node = dag.nodes[failed_id]
    error_lower = (node.error or "").lower()
    is_provider_issue = (
        (node.agent_type == "planner" and "timeout" in error_lower)
        or "empty args" in error_lower
        or "degeneration" in error_lower
        or "{}" in (node.error or "")
    )
    if is_provider_issue:
        streak += 1
    else:
        streak = 0

    if streak >= threshold:
        logger.warning(
            "Replan circuit breaker tripped "
            "(%d consecutive provider issues, threshold %d) "
            "— blocking replan (#750, #795)",
            streak, threshold,
        )
        return True, streak
    return False, streak


# ---------------------------------------------------------------------------
# Auto-serialization of parallel generators
# ---------------------------------------------------------------------------

def auto_serialize_parallel_generators(
    dag: DAG,
    levels: list[list[str]],
) -> tuple[DAG, list[list[str]]]:
    """Auto-serialize parallel generators without ownership contracts (#272 EC4).

    When parallel generators at the same level have no ``owned_files`` AND
    no edges at all (standalone generators), insert implicit HARD edges
    to serialize them, preventing write conflicts.

    Returns ``(dag, levels)`` — dag may be a new instance with added edges.
    """
    from core.models import DependencyType

    edges_added = False
    for level in levels:
        generators = [
            nid for nid in level
            if dag.nodes[nid].agent_type == "generator"
        ]
        if len(generators) < 2:
            continue

        no_contract = [nid for nid in generators if not dag.nodes[nid].owned_files]
        if not no_contract:
            continue

        standalone = []
        for nid in no_contract:
            has_edge = any(
                e.from_node == nid or e.to_node == nid
                for e in dag.edges
            )
            if not has_edge:
                standalone.append(nid)

        if len(standalone) < 2:
            continue

        for i in range(1, len(standalone)):
            from_id = standalone[i - 1]
            to_id = standalone[i]
            dag = dag.add_edge(from_id, to_id, dependency_type=DependencyType.HARD)
            edges_added = True
            logger.info(
                "Auto-serialized standalone generators: %s → %s "
                "(no ownership contracts, no existing edges)",
                from_id, to_id,
            )

    if edges_added:
        return dag, dag.topological_levels()
    return dag, levels
