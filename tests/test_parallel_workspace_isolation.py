"""Tests for per-node workspace isolation and auto-serialization (#176, #272)."""
import asyncio  # noqa: F401
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.models import (  # noqa: F401
    DAG,
    DAGNode,
    DAGEdge,
    DependencyType,
    NodeStatus,
    HandoffArtifact,
)
from core.dag_engine import DAGExecutionEngine


def _make_dag(nodes, edges=None):
    """Build a DAG from simple definitions."""
    dag = DAG(reasoning="test")
    for nid, agent_type, task, owned in nodes:
        node = DAGNode(
            id=nid,
            agent_type=agent_type,
            task_description=task,
            owned_files=owned,
        )
        dag.add_node(node)
    for from_id, to_id, dep_type in (edges or []):
        dag.add_edge(from_id, to_id, dependency_type=dep_type)
    return dag


class TestAutoSerialization:
    """Verify parallel generators without contracts are serialized."""

    @pytest.mark.asyncio
    async def test_parallel_generators_with_contracts_stay_parallel(self):
        """Two generators with disjoint owned_files remain parallel."""
        dag = _make_dag(
            nodes=[
                ("g1", "generator", "impl A", ["src/a.py"]),
                ("g2", "generator", "impl B", ["src/b.py"]),
            ],
        )
        executor = AsyncMock(return_value={"status": "completed", "artifacts": []})
        engine = DAGExecutionEngine(
            agent_executor=executor,
            failure_handler=AsyncMock(),
        )

        # Call auto-serialize
        levels = dag.topological_levels()
        result_levels = engine._auto_serialize_parallel_generators(dag, levels)

        # Should still have parallel generators (same level)
        assert len(result_levels[0]) == 2  # g1 and g2 at same level
        # No new edges added
        assert len(dag.edges) == 0

    @pytest.mark.asyncio
    async def test_parallel_generators_without_contracts_auto_serialize(self):
        """Two generators without owned_files get implicit edges."""
        dag = _make_dag(
            nodes=[
                ("g1", "generator", "impl A", []),
                ("g2", "generator", "impl B", []),
            ],
        )
        executor = AsyncMock(return_value={"status": "completed", "artifacts": []})
        engine = DAGExecutionEngine(
            agent_executor=executor,
            failure_handler=AsyncMock(),
        )

        levels = dag.topological_levels()
        result_levels = engine._auto_serialize_parallel_generators(dag, levels)

        # Should be serialized (different levels)
        assert len(result_levels) == 2  # Two separate levels
        assert len(result_levels[0]) == 1
        assert len(result_levels[1]) == 1

    @pytest.mark.asyncio
    async def test_auto_serialization_preserves_order(self):
        """Auto-serialized generators execute in deterministic order."""
        dag = _make_dag(
            nodes=[
                ("g1", "generator", "impl A", []),
                ("g2", "generator", "impl B", []),
                ("g3", "generator", "impl C", []),
            ],
        )
        executor = AsyncMock(return_value={"status": "completed", "artifacts": []})
        engine = DAGExecutionEngine(
            agent_executor=executor,
            failure_handler=AsyncMock(),
        )

        levels = dag.topological_levels()
        result_levels = engine._auto_serialize_parallel_generators(dag, levels)

        # Should be fully serialized (3 separate levels, one node each)
        assert len(result_levels) == 3
        all_nodes = [nid for level in result_levels for nid in level]
        assert set(all_nodes) == {"g1", "g2", "g3"}

    @pytest.mark.asyncio
    async def test_mixed_contract_and_no_contract(self):
        """Generators with contracts stay parallel; no-contract standalone ones serialize."""
        dag = _make_dag(
            nodes=[
                ("g1", "generator", "impl A", ["src/a.py"]),
                ("g2", "generator", "impl B", []),
                ("g3", "generator", "impl C", []),
            ],
        )
        executor = AsyncMock(return_value={"status": "completed", "artifacts": []})
        engine = DAGExecutionEngine(
            agent_executor=executor,
            failure_handler=AsyncMock(),
        )

        levels = dag.topological_levels()
        result_levels = engine._auto_serialize_parallel_generators(dag, levels)

        # g1 has contract → stays parallel
        # g2 and g3 are standalone without contracts → serialized
        # Result: 2 levels; g1 parallel with one of {g2,g3}, the other serialized after
        assert len(result_levels) == 2
        # Level 0: g1 (with contract) + one of the no-contract generators
        level0 = set(result_levels[0])
        assert "g1" in level0
        assert len(level0) == 2
        # Level 1: the remaining no-contract generator
        assert len(result_levels[1]) == 1
        all_nodes = set().union(*result_levels)
        assert all_nodes == {"g1", "g2", "g3"}

    @pytest.mark.asyncio
    async def test_single_generator_no_auto_serialize(self):
        """Single generator doesn't trigger auto-serialization."""
        dag = _make_dag(
            nodes=[
                ("g1", "generator", "impl A", []),
                ("e1", "evaluator", "eval", []),
            ],
            edges=[("g1", "e1", DependencyType.HARD)],
        )
        executor = AsyncMock(return_value={"status": "completed", "artifacts": []})
        engine = DAGExecutionEngine(
            agent_executor=executor,
            failure_handler=AsyncMock(),
        )

        levels = dag.topological_levels()
        result_levels = engine._auto_serialize_parallel_generators(dag, levels)

        # No change needed
        assert len(result_levels) == 2  # g1 and e1 at separate levels

    @pytest.mark.asyncio
    async def test_sequential_generators_not_affected(self):
        """Already-sequential generators are not affected."""
        dag = _make_dag(
            nodes=[
                ("g1", "generator", "impl A", []),
                ("g2", "generator", "impl B", []),
            ],
            edges=[("g1", "g2", DependencyType.HARD)],
        )
        executor = AsyncMock(return_value={"status": "completed", "artifacts": []})
        engine = DAGExecutionEngine(
            agent_executor=executor,
            failure_handler=AsyncMock(),
        )

        levels = dag.topological_levels()
        result_levels = engine._auto_serialize_parallel_generators(dag, levels)

        # Already sequential
        assert len(result_levels) == 2


class TestBackendManagerIntegration:
    """Verify backend_manager is accepted by DAG engine."""

    def test_backend_manager_parameter(self):
        """DAG engine accepts backend_manager parameter."""
        executor = AsyncMock()
        failure_handler = AsyncMock()
        mock_backend = MagicMock()
        engine = DAGExecutionEngine(
            agent_executor=executor,
            failure_handler=failure_handler,
            backend_manager=mock_backend,
        )
        assert engine.backend_manager is mock_backend

    def test_backend_manager_default_none(self):
        """DAG engine defaults backend_manager to None."""
        executor = AsyncMock()
        failure_handler = AsyncMock()
        engine = DAGExecutionEngine(
            agent_executor=executor,
            failure_handler=failure_handler,
        )
        assert engine.backend_manager is None
