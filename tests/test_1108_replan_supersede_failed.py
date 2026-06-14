"""Tests for #1108: replan supersedes replaced failed nodes.

merge_dag_results now marks old FAILED/SKIPPED nodes (not present in the
replan) as SUPERSEDED instead of leaving them FAILED. This distinguishes
"replaced by replan" from "failed in the final plan" and stops the DAG
from accumulating indistinguishable dead FAILED nodes across replans
(#1108). Successful old nodes are preserved (still counted, #720);
PENDING/RUNNING nodes are preserved as-is in case the replan still needs
them.
"""
from core.dag_models import DAG, DAGNode
from core.dag_replan import merge_dag_results
from core.models import NodeStatus


def _node(
    nid: str,
    status: NodeStatus = NodeStatus.PENDING,
    agent_type: str = "generator",
) -> DAGNode:
    return DAGNode(
        id=nid,
        agent_type=agent_type,
        task_description=f"task {nid}",
        status=status,
    )


class TestMergeDagResultsSupersedesFailed:
    """#1108: replaced failed/skipped nodes become SUPERSEDED on merge."""

    def test_failed_old_node_marked_superseded(self):
        old = DAG(reasoning="old")
        old = old.add_node(_node("impl", NodeStatus.FAILED))
        new = DAG(reasoning="new")
        new = new.add_node(_node("impl_v2"))

        merged = merge_dag_results(old, new)

        assert "impl" in merged.nodes
        assert merged.nodes["impl"].status == NodeStatus.SUPERSEDED
        assert "impl_v2" in merged.nodes

    def test_skipped_old_node_marked_superseded(self):
        old = DAG(reasoning="old")
        old = old.add_node(_node("sk", NodeStatus.SKIPPED))
        new = DAG(reasoning="new")
        new = new.add_node(_node("sk_v2"))

        merged = merge_dag_results(old, new)

        assert merged.nodes["sk"].status == NodeStatus.SUPERSEDED

    def test_successful_old_node_preserved(self):
        old = DAG(reasoning="old")
        old = old.add_node(_node("plan", NodeStatus.SUCCESS, "planner"))
        new = DAG(reasoning="new")
        new = new.add_node(_node("impl_v2"))

        merged = merge_dag_results(old, new)

        # Successful node not in replan stays SUCCESS (counted per #720).
        assert merged.nodes["plan"].status == NodeStatus.SUCCESS

    def test_pending_old_node_preserved(self):
        """A PENDING old node not in the replan is preserved as-is — the
        replan may still need it to run (e.g. an evaluator assessing the
        replacement node's output)."""
        old = DAG(reasoning="old")
        old = old.add_node(_node("eval", NodeStatus.PENDING, "evaluator"))
        new = DAG(reasoning="new")
        new = new.add_node(_node("impl_v2"))

        merged = merge_dag_results(old, new)

        assert merged.nodes["eval"].status == NodeStatus.PENDING

    def test_failed_node_in_replan_not_touched(self):
        """A FAILED node that IS in the replan (same id) is handled by the
        'in new_dag' branch — the new (PENDING) version wins, no supersede."""
        old = DAG(reasoning="old")
        old = old.add_node(_node("impl", NodeStatus.FAILED))
        new = DAG(reasoning="new")
        new = new.add_node(_node("impl", NodeStatus.PENDING))  # retry same id

        merged = merge_dag_results(old, new)

        assert merged.nodes["impl"].status == NodeStatus.PENDING

    def test_repeated_replan_does_not_accumulate_failed(self):
        """#1108 core scenario: across two replans, replaced FAILED nodes
        become SUPERSEDED — distinguishable from a node that fails in the
        final plan."""
        # Replan 1: impl (FAILED) replaced by impl_v2.
        old1 = DAG(reasoning="r1")
        old1 = old1.add_node(_node("impl", NodeStatus.FAILED))
        new1 = DAG(reasoning="new1")
        new1 = new1.add_node(_node("impl_v2"))
        merged1 = merge_dag_results(old1, new1)
        assert merged1.nodes["impl"].status == NodeStatus.SUPERSEDED
        assert merged1.nodes["impl_v2"].status == NodeStatus.PENDING

        # impl_v2 runs and fails; replan 2 replaces it with impl_v3.
        merged1.update_node("impl_v2", status=NodeStatus.FAILED)
        new2 = DAG(reasoning="new2")
        new2 = new2.add_node(_node("impl_v3"))
        merged2 = merge_dag_results(merged1, new2)

        # Both prior nodes superseded; only the latest replacement is PENDING.
        assert merged2.nodes["impl"].status == NodeStatus.SUPERSEDED
        assert merged2.nodes["impl_v2"].status == NodeStatus.SUPERSEDED
        assert merged2.nodes["impl_v3"].status == NodeStatus.PENDING
