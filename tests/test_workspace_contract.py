"""
Tests for #176 PR 1: workspace isolation contract.

Verifies the NodeWorkspace models and BackendManager.setup_node/cleanup_node
methods for per-node workspace isolation.
"""
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.models import (
    DAGNode,
    NodeWorkspace,
    NodeWorkspaceResult,
    NodeWorkspaceStrategy,
)
from backend.lifecycle import BackendManager


class TestNodeWorkspaceModels:
    """Tests for NodeWorkspace and NodeWorkspaceResult models."""

    def test_shared_strategy_default(self):
        node = DAGNode(id="test", agent_type="generator", task_description="impl")
        assert node.workspace_strategy == NodeWorkspaceStrategy.SHARED

    def test_workspace_strategy_enum_values(self):
        assert NodeWorkspaceStrategy.SHARED.value == "shared"
        assert NodeWorkspaceStrategy.WORKTREE.value == "worktree"
        assert NodeWorkspaceStrategy.COPY.value == "copy"

    def test_node_workspace_defaults(self):
        ws = NodeWorkspace(node_id="impl")
        assert ws.strategy == NodeWorkspaceStrategy.SHARED
        assert ws.base_path == ""
        assert ws.workspace_path == ""
        assert ws.baseline_commit == ""

    def test_node_workspace_result_defaults(self):
        result = NodeWorkspaceResult(node_id="impl")
        assert result.changed_files == []
        assert result.patch_content == ""
        assert result.merge_status == "pending"
        assert result.conflicts == []

    def test_node_workspace_result_serialization(self):
        result = NodeWorkspaceResult(
            node_id="impl",
            changed_files=["parser.py"],
            patch_content="diff --git a/parser.py",
            merge_status="merged",
        )
        data = result.model_dump()
        restored = NodeWorkspaceResult(**data)
        assert restored.changed_files == ["parser.py"]
        assert restored.merge_status == "merged"

    def test_dag_node_with_explicit_strategy(self):
        node = DAGNode(
            id="impl",
            agent_type="generator",
            task_description="impl",
            workspace_strategy=NodeWorkspaceStrategy.WORKTREE,
        )
        assert node.workspace_strategy == NodeWorkspaceStrategy.WORKTREE

    def test_dag_node_serialization_roundtrip(self):
        node = DAGNode(
            id="impl",
            agent_type="generator",
            task_description="impl",
            workspace_strategy=NodeWorkspaceStrategy.COPY,
        )
        data = node.model_dump()
        restored = DAGNode(**data)
        assert restored.workspace_strategy == NodeWorkspaceStrategy.COPY


class TestBackendManagerSetupNode:
    """Tests for BackendManager.setup_node()."""

    def test_shared_strategy_returns_run_workdir(self, tmp_path):
        """SHARED strategy returns the run's existing work_dir."""
        manager = BackendManager(
            workspace="local",
            base_path=str(tmp_path / "backends"),
        )
        # Setup a run first
        with patch.object(manager, "_get_workspace_backend") as mock_get:
            mock_backend = MagicMock()
            mock_backend.is_available.return_value = True
            mock_backend.setup.return_value = tmp_path / "run_workdir"
            mock_backend.get_work_dir.return_value = tmp_path / "run_workdir"
            mock_get.return_value = mock_backend
            manager.setup("job1", "run1")

        ws = manager.setup_node("job1", "run1", "node1", strategy="shared")
        assert ws.node_id == "node1"
        assert ws.strategy == NodeWorkspaceStrategy.SHARED
        assert ws.workspace_path == str(tmp_path / "run_workdir")

    def test_shared_strategy_no_active_run(self, tmp_path):
        """SHARED strategy with no active run returns empty paths."""
        manager = BackendManager(
            workspace="local",
            base_path=str(tmp_path / "backends"),
        )
        ws = manager.setup_node("job1", "run1", "node1", strategy="shared")
        assert ws.strategy == NodeWorkspaceStrategy.SHARED
        assert ws.workspace_path == ""

    def test_copy_strategy_creates_isolated_workspace(self, tmp_path):
        """COPY strategy creates a separate directory."""
        manager = BackendManager(
            workspace="local",
            base_path=str(tmp_path / "backends"),
        )
        run_workdir = tmp_path / "run_workdir"
        run_workdir.mkdir()
        (run_workdir / "parser.py").write_text("x = 1")

        # Setup mock run
        with patch.object(manager, "_get_workspace_backend") as mock_get:
            mock_backend = MagicMock()
            mock_backend.is_available.return_value = True
            mock_backend.setup.return_value = run_workdir
            mock_backend.get_work_dir.return_value = run_workdir
            mock_get.return_value = mock_backend
            manager.setup("job1", "run1")

        ws = manager.setup_node("job1", "run1", "node1", strategy="copy")
        assert ws.strategy == NodeWorkspaceStrategy.COPY
        assert ws.workspace_path != str(run_workdir)
        assert Path(ws.workspace_path).exists()
        # Verify the copy has the same files
        assert (Path(ws.workspace_path) / "parser.py").read_text() == "x = 1"

    def test_cleanup_node_removes_copy_workspace(self, tmp_path):
        """cleanup_node removes the copied workspace."""
        manager = BackendManager(
            workspace="local",
            base_path=str(tmp_path / "backends"),
        )
        run_workdir = tmp_path / "run_workdir"
        run_workdir.mkdir()
        (run_workdir / "parser.py").write_text("x = 1")

        with patch.object(manager, "_get_workspace_backend") as mock_get:
            mock_backend = MagicMock()
            mock_backend.is_available.return_value = True
            mock_backend.setup.return_value = run_workdir
            mock_backend.get_work_dir.return_value = run_workdir
            mock_get.return_value = mock_backend
            manager.setup("job1", "run1")

        ws = manager.setup_node("job1", "run1", "node1", strategy="copy")
        assert Path(ws.workspace_path).exists()

        manager.cleanup_node("job1", "run1", "node1")
        assert not Path(ws.workspace_path).exists()

    def test_cleanup_node_shared_is_noop(self, tmp_path):
        """cleanup_node for SHARED strategy does nothing."""
        manager = BackendManager(
            workspace="local",
            base_path=str(tmp_path / "backends"),
        )
        run_workdir = tmp_path / "run_workdir"
        run_workdir.mkdir()

        with patch.object(manager, "_get_workspace_backend") as mock_get:
            mock_backend = MagicMock()
            mock_backend.is_available.return_value = True
            mock_backend.setup.return_value = run_workdir
            mock_backend.get_work_dir.return_value = run_workdir
            mock_get.return_value = mock_backend
            manager.setup("job1", "run1")

        manager.setup_node("job1", "run1", "node1", strategy="shared")  # noqa: F841
        manager.cleanup_node("job1", "run1", "node1")
        # Shared workdir still exists
        assert run_workdir.exists()

    def test_multiple_nodes_get_separate_workspaces(self, tmp_path):
        """Multiple nodes with COPY strategy get separate workspaces."""
        manager = BackendManager(
            workspace="local",
            base_path=str(tmp_path / "backends"),
        )
        run_workdir = tmp_path / "run_workdir"
        run_workdir.mkdir()
        (run_workdir / "base.py").write_text("x = 1")

        with patch.object(manager, "_get_workspace_backend") as mock_get:
            mock_backend = MagicMock()
            mock_backend.is_available.return_value = True
            mock_backend.setup.return_value = run_workdir
            mock_backend.get_work_dir.return_value = run_workdir
            mock_get.return_value = mock_backend
            manager.setup("job1", "run1")

        ws_a = manager.setup_node("job1", "run1", "node_a", strategy="copy")
        ws_b = manager.setup_node("job1", "run1", "node_b", strategy="copy")
        assert ws_a.workspace_path != ws_b.workspace_path

    def test_worktree_failure_falls_back_to_shared(self, tmp_path):
        """When git worktree add fails, falls back to SHARED strategy."""
        manager = BackendManager(
            workspace="local",
            base_path=str(tmp_path / "backends"),
            repo_root=str(tmp_path),
        )
        run_workdir = tmp_path / "run_workdir"
        run_workdir.mkdir()

        with patch.object(manager, "_get_workspace_backend") as mock_get:
            mock_backend = MagicMock()
            mock_backend.is_available.return_value = True
            mock_backend.setup.return_value = run_workdir
            mock_backend.get_work_dir.return_value = run_workdir
            mock_get.return_value = mock_backend
            manager.setup("job1", "run1")

        # Mock subprocess.run so git worktree add fails
        with patch("backend.lifecycle.run_with_progress") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="abc123"),  # git rev-parse
                MagicMock(returncode=1, stderr="worktree add failed"),  # git worktree add
            ]
            ws = manager.setup_node("job1", "run1", "node1", strategy="worktree")

        assert ws.strategy == NodeWorkspaceStrategy.SHARED
        assert ws.workspace_path == str(run_workdir)

    def test_worktree_without_repo_root_falls_back_to_copy(self, tmp_path):
        """WORKTREE without repo_root falls back to COPY strategy."""
        manager = BackendManager(
            workspace="local",
            base_path=str(tmp_path / "backends"),
            repo_root=None,
        )
        run_workdir = tmp_path / "run_workdir"
        run_workdir.mkdir()
        (run_workdir / "base.py").write_text("x = 1")

        with patch.object(manager, "_get_workspace_backend") as mock_get:
            mock_backend = MagicMock()
            mock_backend.is_available.return_value = True
            mock_backend.setup.return_value = run_workdir
            mock_backend.get_work_dir.return_value = run_workdir
            mock_get.return_value = mock_backend
            manager.setup("job1", "run1")

        ws = manager.setup_node("job1", "run1", "node1", strategy="worktree")
        # Should fall back to COPY (not WORKTREE) since no repo_root
        assert ws.strategy == NodeWorkspaceStrategy.COPY
        assert (Path(ws.workspace_path) / "base.py").read_text() == "x = 1"
