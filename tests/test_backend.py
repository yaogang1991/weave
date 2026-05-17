"""
Unit tests for execution backends.

Covers:
- LocalBackend: setup, cleanup, preserve
- WorktreeBackend: availability, setup (with git), cleanup
- BackendManager: workspace/sandbox composition, risk mapping, lifecycle
- DockerBackend stub: returns not available
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure project root is on sys.path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.base import WorkspaceIsolation, ExecutionSandbox  # noqa: E402
from backend.local import LocalBackend  # noqa: E402
from backend.worktree import WorktreeBackend  # noqa: E402
from backend.lifecycle import BackendManager  # noqa: E402
from backend.docker_stub import DockerBackend  # noqa: E402


# ============================================================================
# LocalBackend tests
# ============================================================================


class TestLocalBackend:
    """Tests for LocalBackend."""

    def test_setup_creates_directory(self, tmp_path):
        """setup() should create the working directory."""
        backend = LocalBackend(base_path=str(tmp_path / "backends"))
        job_id = "test-job"
        run_id = "test-run-1"

        work_dir = backend.setup(job_id, run_id)

        assert work_dir.exists()
        assert work_dir.is_dir()
        assert str(work_dir) == str(tmp_path / "backends" / job_id / run_id)

    def test_setup_returns_existing_directory(self, tmp_path):
        """setup() should return existing directory without error."""
        backend = LocalBackend(base_path=str(tmp_path / "backends"))
        job_id = "test-job"
        run_id = "test-run-1"

        work_dir1 = backend.setup(job_id, run_id)
        work_dir2 = backend.setup(job_id, run_id)

        assert work_dir1 == work_dir2
        assert work_dir1.exists()

    def test_get_work_dir_returns_path(self, tmp_path):
        """get_work_dir() should return path without creating."""
        backend = LocalBackend(base_path=str(tmp_path / "backends"))
        job_id = "test-job"
        run_id = "test-run-1"

        work_dir = backend.get_work_dir(job_id, run_id)

        assert not work_dir.exists()
        assert "test-job" in str(work_dir)
        assert "test-run-1" in str(work_dir)

    def test_cleanup_removes_directory(self, tmp_path):
        """cleanup() should delete the working directory."""
        backend = LocalBackend(base_path=str(tmp_path / "backends"))
        job_id = "test-job"
        run_id = "test-run-1"

        work_dir = backend.setup(job_id, run_id)
        assert work_dir.exists()

        backend.cleanup(job_id, run_id)
        assert not work_dir.exists()

    def test_cleanup_nonexistent_directory(self, tmp_path):
        """cleanup() should not raise for non-existent directory."""
        backend = LocalBackend(base_path=str(tmp_path / "backends"))
        backend.cleanup("nonexistent", "nonexistent")

    def test_preserve_moves_directory(self, tmp_path):
        """preserve() should move work dir to preserve dir."""
        backend = LocalBackend(base_path=str(tmp_path / "backends"))
        job_id = "test-job"
        run_id = "test-run-1"

        work_dir = backend.setup(job_id, run_id)
        (work_dir / "test_file.txt").write_text("test content")

        preserve_dir = backend.preserve(job_id, run_id, reason="test failure")

        assert not work_dir.exists()
        assert preserve_dir.exists()
        assert "_preserved" in str(preserve_dir)

    def test_is_available_always_true(self):
        """Local backend should always be available."""
        backend = LocalBackend()
        assert backend.is_available() is True

    def test_workspace_type(self):
        """LocalBackend should have correct workspace_type."""
        assert LocalBackend.workspace_type == WorkspaceIsolation.LOCAL


# ============================================================================
# WorktreeBackend tests
# ============================================================================


class TestWorktreeBackend:
    """Tests for WorktreeBackend."""

    def test_workspace_type(self):
        """WorktreeBackend should have correct workspace_type."""
        assert WorktreeBackend.workspace_type == WorkspaceIsolation.WORKTREE

    def test_init_creates_base_path(self, tmp_path):
        """__init__ should create base_path if not exists."""
        base = tmp_path / "worktrees"
        assert not base.exists()

        WorktreeBackend(base_path=str(base))  # noqa: F841

        assert base.exists()
        assert base.is_dir()

    def test_is_available_detects_git(self, tmp_path):
        """is_available() should detect git availability."""
        backend = WorktreeBackend(repo_root=str(tmp_path))
        result = backend.is_available()
        assert isinstance(result, bool)

    def test_is_available_false_when_no_git(self, tmp_path):
        """is_available() should return False when git is not available."""
        backend = WorktreeBackend(repo_root=str(tmp_path))
        with patch("backend.worktree.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("git not found")
            assert backend.is_available() is False

    def test_get_work_dir_before_setup(self, tmp_path):
        """get_work_dir() should return expected path before setup."""
        backend = WorktreeBackend(base_path=str(tmp_path))
        work_dir = backend.get_work_dir("job-1", "run-1")
        assert str(work_dir) == str(tmp_path / "job-1" / "run-1")

    def test_preserve_writes_marker(self, tmp_path):
        """preserve() should write .PRESERVED marker file."""
        backend = WorktreeBackend(base_path=str(tmp_path))
        worktree_path = tmp_path / "job-1" / "run-1"
        worktree_path.mkdir(parents=True)
        backend.worktrees["run-1"] = worktree_path

        result = backend.preserve("job-1", "run-1", reason="test failure")

        assert result == worktree_path
        marker = worktree_path / ".PRESERVED"
        assert marker.exists()
        content = marker.read_text()
        assert "Preserved at:" in content
        assert "Reason: test failure" in content


# ============================================================================
# BackendManager tests (new orthogonal architecture)
# ============================================================================


class TestBackendManager:
    """Tests for BackendManager with orthogonal workspace/sandbox."""

    def test_default_composition(self, tmp_path):
        """Default should be LOCAL workspace + LOCAL sandbox."""
        manager = BackendManager(base_path=str(tmp_path))
        assert manager.workspace_type == WorkspaceIsolation.LOCAL
        assert manager.sandbox_type == ExecutionSandbox.LOCAL

    def test_worktree_composition(self, tmp_path):
        """Can specify WORKTREE workspace."""
        manager = BackendManager(workspace="worktree", base_path=str(tmp_path))
        assert manager.workspace_type == WorkspaceIsolation.WORKTREE

    def test_setup_local(self, tmp_path):
        """setup() with local workspace should create directory."""
        manager = BackendManager(base_path=str(tmp_path))
        work_dir = manager.setup("job-1", "run-1", workspace_type="local")
        assert work_dir.exists()

    def test_risk_level_mapping(self, tmp_path):
        """Risk level should map to appropriate workspace isolation."""
        manager = BackendManager(base_path=str(tmp_path))

        ws = manager._resolve_workspace_type(None, "low")
        assert ws == WorkspaceIsolation.LOCAL

        ws = manager._resolve_workspace_type(None, "high")
        assert ws == WorkspaceIsolation.WORKTREE

        ws = manager._resolve_workspace_type(None, "critical")
        assert ws == WorkspaceIsolation.WORKTREE

    def test_explicit_overrides_risk(self, tmp_path):
        """Explicit workspace_type should override risk_level."""
        manager = BackendManager(base_path=str(tmp_path))

        ws = manager._resolve_workspace_type("local", "critical")
        assert ws == WorkspaceIsolation.LOCAL

    def test_fallback_to_default(self, tmp_path):
        """No explicit or risk level should use default."""
        manager = BackendManager(workspace="worktree", base_path=str(tmp_path))

        ws = manager._resolve_workspace_type(None, None)
        assert ws == WorkspaceIsolation.WORKTREE

    def test_unknown_risk_uses_default(self, tmp_path):
        """Unknown risk level should fall back to default."""
        manager = BackendManager(base_path=str(tmp_path))

        ws = manager._resolve_workspace_type(None, "unknown_risk")
        assert ws == WorkspaceIsolation.LOCAL

    def test_custom_risk_map(self, tmp_path):
        """Custom workspace_by_risk should be respected."""
        manager = BackendManager(
            base_path=str(tmp_path),
            workspace_by_risk={"low": "local", "medium": "worktree"},
        )

        assert manager._resolve_workspace_type(None, "low") == WorkspaceIsolation.LOCAL
        assert manager._resolve_workspace_type(None, "medium") == WorkspaceIsolation.WORKTREE

    def test_cleanup_removes_active_run(self, tmp_path):
        """cleanup() should remove run from active_runs."""
        manager = BackendManager(base_path=str(tmp_path))
        manager.setup("job-1", "run-1", workspace_type="local")
        assert "run-1" in manager._active_runs

        manager.cleanup("job-1", "run-1")
        assert "run-1" not in manager._active_runs

    def test_get_active_runs(self, tmp_path):
        """get_active_runs() should return active runs."""
        manager = BackendManager(base_path=str(tmp_path))
        manager.setup("job-1", "run-1", workspace_type="local")
        manager.setup("job-1", "run-2", workspace_type="local")

        active = manager.get_active_runs()
        assert len(active) == 2
        assert active["run-1"] == "local"
        assert active["run-2"] == "local"

    def test_sandbox_provider_accessible(self):
        """sandbox property should return the sandbox provider."""
        manager = BackendManager()
        from backend.sandbox import LocalSandbox
        assert isinstance(manager.sandbox, LocalSandbox)


# ============================================================================
# BackendManager fallback tests
# ============================================================================


class TestBackendManagerFallback:
    """Tests for BackendManager fallback behavior."""

    def test_worktree_unavailable_fallback_to_local(self, tmp_path):
        """When worktree is unavailable, should fall back to local."""
        manager = BackendManager(base_path=str(tmp_path))

        with patch.object(
            WorktreeBackend, "is_available", return_value=False
        ):
            work_dir = manager.setup("job-1", "run-1", workspace_type="worktree")

        assert work_dir.exists()
        active = manager.get_active_runs()
        assert "run-1" in active
        assert active["run-1"] == "local"

    def test_backend_singleton_reuse(self, tmp_path):
        """Same workspace type should be reused (singleton pattern)."""
        manager = BackendManager(base_path=str(tmp_path))
        manager.setup("job-1", "run-1", workspace_type="local")
        manager.setup("job-1", "run-2", workspace_type="local")

        assert len(manager._backends) == 1
        assert "local" in manager._backends


# ============================================================================
# DockerBackend stub tests
# ============================================================================


class TestDockerBackend:
    """Tests for DockerBackend stub."""

    def test_is_available_returns_false(self):
        """DockerBackend should not be available."""
        backend = DockerBackend()
        assert backend.is_available() is False

    def test_setup_raises(self):
        """setup() should raise NotImplementedError."""
        backend = DockerBackend()
        with pytest.raises(NotImplementedError) as exc_info:
            backend.setup("job-1", "run-1")
        assert "planned for M3" in str(exc_info.value)


# ============================================================================
# Enum tests
# ============================================================================


class TestEnums:
    """Tests for WorkspaceIsolation and ExecutionSandbox enums."""

    def test_workspace_isolation_values(self):
        assert WorkspaceIsolation.LOCAL == "local"
        assert WorkspaceIsolation.WORKTREE == "worktree"

    def test_workspace_isolation_from_string(self):
        assert WorkspaceIsolation("local") == WorkspaceIsolation.LOCAL
        assert WorkspaceIsolation("worktree") == WorkspaceIsolation.WORKTREE

    def test_execution_sandbox_values(self):
        assert ExecutionSandbox.LOCAL == "local"
        assert ExecutionSandbox.DOCKER == "docker"

    def test_execution_sandbox_from_string(self):
        assert ExecutionSandbox("local") == ExecutionSandbox.LOCAL
        assert ExecutionSandbox("docker") == ExecutionSandbox.DOCKER
