"""Test dependency-aware cascade skip (#259).

Verifies that when an upstream node fails, only its direct dependents
are skipped — not all downstream nodes. Nodes whose dependencies all
succeeded should continue executing.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.dag_engine import DAGExecutionEngine
from core.models import (
    DAG,
    DAGEdge,
    DAGNode,
    FailureDecision,
    NodeStatus,
)


def _make_node(nid: str, agent_type: str = "generator", **kw) -> DAGNode:
    defaults = {
        "id": nid,
        "agent_type": agent_type,
        "task_description": f"Task for {nid}",
        "max_retries": 0,
    }
    defaults.update(kw)
    return DAGNode(**defaults)


async def _noop_executor(node, artifacts, **kwargs):
    return {"artifacts": [], "summary": "done"}


async def _skip_handler(dag, node_id, error):
    """Default handler: skip the failed node (don't abort entire DAG)."""
    return FailureDecision(action="skip", reasoning="skip failed node")


async def _abort_handler(dag, node_id, error):
    return FailureDecision(action="abort", reasoning="abort")


class TestDependencyAwareSkip:
    """Only nodes depending on failed predecessors should be skipped."""

    @pytest.mark.asyncio
    async def test_node_without_failed_dep_executes(self):
        """D depends on B (success) only — should execute even if A fails."""
        dag = DAG(
            nodes={
                "A": _make_node("A"),
                "B": _make_node("B"),
                "C": _make_node("C"),
                "D": _make_node("D"),
            },
            edges=[
                DAGEdge(from_node="A", to_node="C"),
                DAGEdge(from_node="B", to_node="D"),
            ],
        )

        async def selective_executor(node, artifacts, **kwargs):
            if node.id == "A":
                raise RuntimeError("A failed")
            return {"artifacts": [], "summary": f"{node.id} done"}

        engine = DAGExecutionEngine(
            agent_executor=selective_executor,
            failure_handler=_skip_handler,
            max_parallel=5,
            enable_watchdog=False,
        )

        result = await engine.execute(dag)

        assert result.nodes["A"].status == NodeStatus.SKIPPED  # handler says skip
        assert result.nodes["B"].status == NodeStatus.SUCCESS
        # C depends on A (skipped) → skipped
        assert result.nodes["C"].status == NodeStatus.SKIPPED
        # D depends on B (success) → should execute and succeed
        assert result.nodes["D"].status == NodeStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_no_deps_node_always_executes(self):
        """Nodes with no dependencies should always execute."""
        dag = DAG(
            nodes={
                "A": _make_node("A"),
                "B": _make_node("B"),
                "C": _make_node("C"),
            },
            edges=[
                DAGEdge(from_node="A", to_node="C"),
            ],
        )

        async def selective_executor(node, artifacts, **kwargs):
            if node.id == "A":
                raise RuntimeError("A failed")
            return {"artifacts": [], "summary": f"{node.id} done"}

        engine = DAGExecutionEngine(
            agent_executor=selective_executor,
            failure_handler=_skip_handler,
            max_parallel=5,
            enable_watchdog=False,
        )

        result = await engine.execute(dag)

        assert result.nodes["A"].status == NodeStatus.SKIPPED
        # B has no deps → should always execute
        assert result.nodes["B"].status == NodeStatus.SUCCESS
        # C depends on A (skipped) → skipped
        assert result.nodes["C"].status == NodeStatus.SKIPPED

    @pytest.mark.asyncio
    async def test_abort_still_skips_all(self):
        """When failure_handler returns 'abort', all remaining nodes are skipped."""
        dag = DAG(
            nodes={
                "A": _make_node("A"),
                "B": _make_node("B"),
            },
            edges=[
                DAGEdge(from_node="A", to_node="B"),
            ],
        )

        async def fail_executor(node, artifacts, **kwargs):
            raise RuntimeError("fail")

        engine = DAGExecutionEngine(
            agent_executor=fail_executor,
            failure_handler=_abort_handler,
            max_parallel=5,
            enable_watchdog=False,
        )

        result = await engine.execute(dag)

        assert result.nodes["A"].status == NodeStatus.FAILED
        # B is in the next level → _skip_remaining applies on abort
        assert result.nodes["B"].status == NodeStatus.SKIPPED

    @pytest.mark.asyncio
    async def test_transitive_skip(self):
        """If A fails → B (depends on A) skipped → C (depends on B) skipped."""
        dag = DAG(
            nodes={
                "A": _make_node("A"),
                "B": _make_node("B"),
                "C": _make_node("C"),
            },
            edges=[
                DAGEdge(from_node="A", to_node="B"),
                DAGEdge(from_node="B", to_node="C"),
            ],
        )

        async def selective_executor(node, artifacts, **kwargs):
            if node.id == "A":
                raise RuntimeError("A failed")
            return {"artifacts": [], "summary": f"{node.id} done"}

        engine = DAGExecutionEngine(
            agent_executor=selective_executor,
            failure_handler=_skip_handler,
            max_parallel=5,
            enable_watchdog=False,
        )

        result = await engine.execute(dag)

        assert result.nodes["A"].status == NodeStatus.SKIPPED
        assert result.nodes["B"].status == NodeStatus.SKIPPED
        assert result.nodes["C"].status == NodeStatus.SKIPPED

    @pytest.mark.asyncio
    async def test_multi_level_partial_skip(self):
        """
        Diamond: A → B, A → C, B → D, C → E
        A fails → B skipped, C skipped → D skipped, E skipped
        But if we add F (no deps) → F should succeed.
        """
        dag = DAG(
            nodes={
                "A": _make_node("A"),
                "B": _make_node("B"),
                "C": _make_node("C"),
                "D": _make_node("D"),
                "E": _make_node("E"),
                "F": _make_node("F"),
            },
            edges=[
                DAGEdge(from_node="A", to_node="B"),
                DAGEdge(from_node="A", to_node="C"),
                DAGEdge(from_node="B", to_node="D"),
                DAGEdge(from_node="C", to_node="E"),
            ],
        )

        async def selective_executor(node, artifacts, **kwargs):
            if node.id == "A":
                raise RuntimeError("A failed")
            return {"artifacts": [], "summary": f"{node.id} done"}

        engine = DAGExecutionEngine(
            agent_executor=selective_executor,
            failure_handler=_skip_handler,
            max_parallel=5,
            enable_watchdog=False,
        )

        result = await engine.execute(dag)

        assert result.nodes["A"].status == NodeStatus.SKIPPED
        assert result.nodes["B"].status == NodeStatus.SKIPPED
        assert result.nodes["C"].status == NodeStatus.SKIPPED
        assert result.nodes["D"].status == NodeStatus.SKIPPED
        assert result.nodes["E"].status == NodeStatus.SKIPPED
        # F has no deps → should execute
        assert result.nodes["F"].status == NodeStatus.SUCCESS

    @pytest.mark.asyncio
    async def test_retry_failure_no_global_cascade(self):
        """When a node fails after retry, only its dependents are skipped."""
        dag = DAG(
            nodes={
                "A": _make_node("A", max_retries=1),
                "B": _make_node("B"),
                "C": _make_node("C"),
            },
            edges=[
                DAGEdge(from_node="A", to_node="C"),
            ],
        )

        async def fail_a_executor(node, artifacts, **kwargs):
            if node.id == "A":
                raise RuntimeError("A failed")
            return {"artifacts": [], "summary": f"{node.id} done"}

        async def retry_handler(dag, node_id, error):
            return FailureDecision(action="retry", reasoning="try again")

        engine = DAGExecutionEngine(
            agent_executor=fail_a_executor,
            failure_handler=retry_handler,
            max_parallel=5,
            enable_watchdog=False,
        )

        result = await engine.execute(dag)

        assert result.nodes["A"].status == NodeStatus.FAILED
        assert result.nodes["B"].status == NodeStatus.SUCCESS
        # C depends on A (failed) → skipped
        assert result.nodes["C"].status == NodeStatus.SKIPPED

    @pytest.mark.asyncio
    async def test_partial_success_in_diamond(self):
        """
        Two parallel chains:
          A → C → E
          B → D → F
        A fails, B succeeds. C skipped, D executes. E skipped, F executes.
        """
        dag = DAG(
            nodes={
                "A": _make_node("A"),
                "B": _make_node("B"),
                "C": _make_node("C"),
                "D": _make_node("D"),
                "E": _make_node("E"),
                "F": _make_node("F"),
            },
            edges=[
                DAGEdge(from_node="A", to_node="C"),
                DAGEdge(from_node="B", to_node="D"),
                DAGEdge(from_node="C", to_node="E"),
                DAGEdge(from_node="D", to_node="F"),
            ],
        )

        async def selective_executor(node, artifacts, **kwargs):
            if node.id == "A":
                raise RuntimeError("A failed")
            return {"artifacts": [], "summary": f"{node.id} done"}

        engine = DAGExecutionEngine(
            agent_executor=selective_executor,
            failure_handler=_skip_handler,
            max_parallel=5,
            enable_watchdog=False,
        )

        result = await engine.execute(dag)

        assert result.nodes["A"].status == NodeStatus.SKIPPED
        assert result.nodes["B"].status == NodeStatus.SUCCESS
        # Chain 1: A failed → C skipped → E skipped
        assert result.nodes["C"].status == NodeStatus.SKIPPED
        assert result.nodes["E"].status == NodeStatus.SKIPPED
        # Chain 2: B succeeded → D executes → F executes
        assert result.nodes["D"].status == NodeStatus.SUCCESS
        assert result.nodes["F"].status == NodeStatus.SUCCESS
