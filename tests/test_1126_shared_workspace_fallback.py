"""Tests for #1126: SHARED workspace strategy must resolve to work_dir.

Root cause: ``NodeExecutor._prepare_stage`` left ``workspace_path=None``
under the SHARED strategy — the default (``dag_models.py`` documents
``SHARED = "Share the run's work_dir"``). Because the fallback was missing,
``work_dir`` (from ``--project``) never reached ``BackendContext``, so
external backends ran in the wrong directory and ``_discover_artifacts``
returned ``[]`` — every generator node failed with "zero output artifacts".
"""
from unittest.mock import AsyncMock

from core.models import DAG, DAGNode, NodeWorkspaceStrategy
from core.node_executor import NodeExecutor, NodeExecutorConfig
from core.watchdog import WatchdogService


def _make_shared_node(nid: str = "n1") -> DAGNode:
    """A node using the default SHARED workspace strategy."""
    node = DAGNode(id=nid, agent_type="generator", task_description="test")
    assert node.workspace_strategy == NodeWorkspaceStrategy.SHARED
    return node


def _make_dag(node: DAGNode) -> DAG:
    return DAG(nodes={node.id: node}, edges=[])


def _make_executor(
    work_dir: str | None = None,
    backend_manager: object | None = None,
) -> NodeExecutor:
    return NodeExecutor(
        agent_executor=AsyncMock(return_value={}),
        emit_func=AsyncMock(),
        watchdog=WatchdogService(),
        config=NodeExecutorConfig(
            work_dir=work_dir,
            backend_manager=backend_manager,
        ),
    )


class TestSharedWorkspaceFallback:
    """#1126: SHARED strategy should resolve to the project work_dir."""

    async def test_shared_strategy_uses_work_dir(self):
        """SHARED + work_dir → workspace_path must equal work_dir.

        This is the core bug: previously stayed None, so the claude_code
        backend ran in the weave directory instead of the target project.
        """
        work_dir = "/tmp/target-project"
        node = _make_shared_node()
        dag = _make_dag(node)
        executor = _make_executor(work_dir=work_dir, backend_manager=None)

        prep = await executor._prepare_stage(dag, node.id)

        assert prep is not None
        assert prep.workspace_path == work_dir

    async def test_shared_strategy_no_work_dir_stays_none(self):
        """Backward compat: no work_dir → None (backend falls back to cwd).

        Before #1126 fix this was the only behavior; it must be preserved
        so runs without ``--project`` keep working.
        """
        node = _make_shared_node()
        dag = _make_dag(node)
        executor = _make_executor(work_dir=None, backend_manager=None)

        prep = await executor._prepare_stage(dag, node.id)

        assert prep is not None
        assert prep.workspace_path is None


# NOTE: A third case — "WORKTREE setup_node failure falls back to work_dir"
# — is blocked by a separate, pre-existing bug: ``_prepare_stage`` emits
# ``event_type="workspace_isolation_failed"``, which is not a member of
# ``EventType`` and raises ``ValidationError`` before the fallback can run.
# That is independent of #1126 (it affects the isolation-failure path, not
# the default SHARED path) and should be fixed in its own change.
