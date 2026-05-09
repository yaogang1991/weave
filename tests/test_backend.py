"""
Unit tests for execution backends.

Covers:
- LocalBackend: setup, cleanup, preserve
- WorktreeBackend: availability, setup (with git), cleanup
- BackendManager: backend selection, risk mapping, lifecycle
- DockerBackend stub: returns not available
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root is on sys.path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.base import BackendType, ExecutionBackend
from backend.local import LocalBackend
from backend.worktree import WorktreeBackend
from backend.lifecycle import BackendManager
from backend.docker_stub import DockerBackend


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

        # First call creates
        work_dir1 = backend.setup(job_id, run_id)
        # Second call returns existing
        work_dir2 = backend.setup(job_id, run_id)

        assert work_dir1 == work_dir2
        assert work_dir1.exists()

    def test_get_work_dir_returns_path(self, tmp_path):
        """get_work_dir() should return path without creating."""
        backend = LocalBackend(base_path=str(tmp_path / "backends"))
        job_id = "test-job"
        run_id = "test-run-1"

        work_dir = backend.get_work_dir(job_id, run_id)

        assert not work_dir.exists()  # Not created yet
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

        # Should not raise
        backend.cleanup("nonexistent", "nonexistent")

    def test_preserve_moves_directory(self, tmp_path):
        """preserve() should move work dir to preserve dir."""
        backend = LocalBackend(base_path=str(tmp_path / "backends"))
        job_id = "test-job"
        run_id = "test-run-1"

        work_dir = backend.setup(job_id, run_id)
        # Create a file in the directory to verify move
        (work_dir / "test_file.txt").write_text("test content")

        preserve_dir = backend.preserve(job_id, run_id, reason="test failure")

        assert not work_dir.exists()
        assert preserve_dir.exists()
        assert "_preserved" in str(preserve_dir)

    def test_is_available_always_true(self):
        """Local backend should always be available."""
        backend = LocalBackend()
        assert backend.is_available() is True

    def test_backend_type(self):
        """LocalBackend should have correct backend_type."""
        assert LocalBackend.backend_type == BackendType.LOCAL


# ============================================================================
# WorktreeBackend tests
# ============================================================================


class TestWorktreeBackend:
    """Tests for WorktreeBackend."""

    def test_backend_type(self):
        """WorktreeBackend should have correct backend_type."""
        assert WorktreeBackend.backend_type == BackendType.WORKTREE

    def test_init_creates_base_path(self, tmp_path):
        """__init__ should create base_path if not exists."""
        base = tmp_path / "worktrees"
        assert not base.exists()

        backend = WorktreeBackend(base_path=str(base))

        assert base.exists()
        assert base.is_dir()

    def test_is_available_detects_git(self, tmp_path):
        """is_available() should detect git availability."""
        backend = WorktreeBackend(repo_root=str(tmp_path))
        # In a non-git directory, should return False
        result = backend.is_available()
        # This depends on the test environment
        assert isinstance(result, bool)

    def test_is_available_false_when_no_git(self, tmp_path):
        """is_available() should return False when git is not available."""
        backend = WorktreeBackend(repo_root=str(tmp_path))
        # Patch subprocess.run to simulate git not found
        with patch("backend.worktree.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("git not found")
            assert backend.is_available() is False

    def test_is_available_false_on_timeout(self, tmp_path):
        """is_available() should return False on timeout."""
        backend = WorktreeBackend(repo_root=str(tmp_path))
        with patch("backend.worktree.subprocess.run") as mock_run:
            from subprocess import TimeoutExpired

            mock_run.side_effect = TimeoutExpired("git", 5)
            assert backend.is_available() is False

    def test_get_work_dir_before_setup(self, tmp_path):
        """get_work_dir() should return expected path before setup."""
        backend = WorktreeBackend(base_path=str(tmp_path))
        job_id = "job-1"
        run_id = "run-1"

        work_dir = backend.get_work_dir(job_id, run_id)

        assert str(work_dir) == str(tmp_path / "job-1" / "run-1")

    def test_get_work_dir_after_setup(self, tmp_path):
        """get_work_dir() should return cached path after setup."""
        backend = WorktreeBackend(base_path=str(tmp_path))
        job_id = "job-1"
        run_id = "run-1"

        # Manually set worktree tracking
        expected_path = tmp_path / "cached" / "path"
        backend.worktrees[run_id] = expected_path

        work_dir = backend.get_work_dir(job_id, run_id)

        assert work_dir == expected_path

    def test_list_active_worktrees_parsing(self, tmp_path):
        """list_active_worktrees() should parse porcelain output."""
        # Initialize a git repo for this test
        import subprocess

        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(repo),
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(repo),
            capture_output=True,
        )
        # Create initial commit
        (repo / "file.txt").write_text("content")
        subprocess.run(
            ["git", "add", "."], cwd=str(repo), capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=str(repo),
            capture_output=True,
        )

        backend = WorktreeBackend(repo_root=str(repo))
        worktrees = backend.list_active_worktrees()

        # Should have at least the main worktree
        assert len(worktrees) >= 1
        assert all("path" in wt for wt in worktrees)

    def test_preserve_writes_marker(self, tmp_path):
        """preserve() should write .PRESERVED marker file."""
        backend = WorktreeBackend(base_path=str(tmp_path))
        job_id = "job-1"
        run_id = "run-1"

        # Create a fake worktree directory
        worktree_path = tmp_path / job_id / run_id
        worktree_path.mkdir(parents=True)
        backend.worktrees[run_id] = worktree_path

        result = backend.preserve(job_id, run_id, reason="test failure")

        assert result == worktree_path
        marker = worktree_path / ".PRESERVED"
        assert marker.exists()
        content = marker.read_text()
        assert "Preserved at:" in content
        assert "Reason: test failure" in content

    def test_preserve_removes_from_active(self, tmp_path):
        """preserve() should remove run_id from active worktrees."""
        backend = WorktreeBackend(base_path=str(tmp_path))
        job_id = "job-1"
        run_id = "run-1"

        worktree_path = tmp_path / job_id / run_id
        worktree_path.mkdir(parents=True)
        backend.worktrees[run_id] = worktree_path

        backend.preserve(job_id, run_id)

        assert run_id not in backend.worktrees


# ============================================================================
# BackendManager tests
# ============================================================================


class TestBackendManager:
    """Tests for BackendManager."""

    def test_default_backend_selection(self, tmp_path):
        """Default backend should be local."""
        manager = BackendManager(
            default_backend="local", base_path=str(tmp_path)
        )
        assert manager.default_backend_type == BackendType.LOCAL

    def test_worktree_default_backend(self, tmp_path):
        """Default backend can be set to worktree."""
        manager = BackendManager(
            default_backend="worktree", base_path=str(tmp_path)
        )
        assert manager.default_backend_type == BackendType.WORKTREE

    def test_setup_with_explicit_backend(self, tmp_path):
        """setup() should use explicit backend_type."""
        manager = BackendManager(base_path=str(tmp_path))
        job_id = "job-1"
        run_id = "run-1"

        work_dir = manager.setup(
            job_id, run_id, backend_type="local"
        )

        assert work_dir.exists()
        assert str(work_dir) == str(
            tmp_path / "job-1" / "run-1"
        )

    def test_risk_level_mapping_low(self, tmp_path):
        """Low risk should map to local backend."""
        manager = BackendManager(base_path=str(tmp_path))

        btype = manager._resolve_backend_type(None, "low")
        assert btype == BackendType.LOCAL

    def test_risk_level_mapping_high(self, tmp_path):
        """High risk should map to worktree backend."""
        manager = BackendManager(base_path=str(tmp_path))

        btype = manager._resolve_backend_type(None, "high")
        assert btype == BackendType.WORKTREE

    def test_risk_level_mapping_critical(self, tmp_path):
        """Critical risk should map to worktree backend."""
        manager = BackendManager(base_path=str(tmp_path))

        btype = manager._resolve_backend_type(None, "critical")
        assert btype == BackendType.WORKTREE

    def test_explicit_overrides_risk(self, tmp_path):
        """Explicit backend_type should override risk_level."""
        manager = BackendManager(base_path=str(tmp_path))

        btype = manager._resolve_backend_type("local", "critical")
        assert btype == BackendType.LOCAL

    def test_explicit_overrides_default(self, tmp_path):
        """Explicit backend_type should override default."""
        manager = BackendManager(
            default_backend="local", base_path=str(tmp_path)
        )

        btype = manager._resolve_backend_type("worktree", None)
        assert btype == BackendType.WORKTREE

    def test_fallback_to_default(self, tmp_path):
        """No explicit or risk level should use default."""
        manager = BackendManager(
            default_backend="worktree", base_path=str(tmp_path)
        )

        btype = manager._resolve_backend_type(None, None)
        assert btype == BackendType.WORKTREE

    def test_unknown_risk_level_uses_default(self, tmp_path):
        """Unknown risk level should fall back to default."""
        manager = BackendManager(
            default_backend="local", base_path=str(tmp_path)
        )

        btype = manager._resolve_backend_type(None, "unknown_risk")
        assert btype == BackendType.LOCAL

    def test_custom_risk_backend_map(self, tmp_path):
        """Custom risk_backend_map should be respected."""
        manager = BackendManager(
            base_path=str(tmp_path),
            risk_backend_map={
                "low": "local",
                "medium": "worktree",
                "high": "worktree",
            },
        )

        assert manager._resolve_backend_type(None, "low") == BackendType.LOCAL
        assert (
            manager._resolve_backend_type(None, "medium")
            == BackendType.WORKTREE
        )

    def test_cleanup_removes_active_run(self, tmp_path):
        """cleanup() should remove run from active_runs."""
        manager = BackendManager(base_path=str(tmp_path))
        job_id = "job-1"
        run_id = "run-1"

        manager.setup(job_id, run_id, backend_type="local")
        assert run_id in manager._active_runs

        manager.cleanup(job_id, run_id)
        assert run_id not in manager._active_runs

    def test_preserve_removes_active_run(self, tmp_path):
        """preserve() should remove run from active_runs."""
        manager = BackendManager(base_path=str(tmp_path))
        job_id = "job-1"
        run_id = "run-1"

        manager.setup(job_id, run_id, backend_type="local")
        assert run_id in manager._active_runs

        manager.preserve(job_id, run_id, reason="test")
        assert run_id not in manager._active_runs

    def test_get_active_runs(self, tmp_path):
        """get_active_runs() should return active runs."""
        manager = BackendManager(base_path=str(tmp_path))

        manager.setup("job-1", "run-1", backend_type="local")
        manager.setup("job-1", "run-2", backend_type="local")

        active = manager.get_active_runs()

        assert len(active) == 2
        assert active["run-1"] == "local"
        assert active["run-2"] == "local"

    def test_get_work_dir_returns_path(self, tmp_path):
        """get_work_dir() should return path for active run."""
        manager = BackendManager(base_path=str(tmp_path))
        job_id = "job-1"
        run_id = "run-1"

        manager.setup(job_id, run_id, backend_type="local")
        work_dir = manager.get_work_dir(job_id, run_id)

        assert work_dir is not None
        assert work_dir.exists()

    def test_get_work_dir_inactive_run(self, tmp_path):
        """get_work_dir() should return None for inactive run."""
        manager = BackendManager(base_path=str(tmp_path))

        work_dir = manager.get_work_dir("job-1", "nonexistent")

        assert work_dir is None

    def test_preserve_returns_none_for_inactive(self, tmp_path):
        """preserve() should return None for inactive run."""
        manager = BackendManager(base_path=str(tmp_path))

        result = manager.preserve("job-1", "nonexistent")

        assert result is None

    def test_cleanup_silent_for_inactive(self, tmp_path):
        """cleanup() should not raise for inactive run."""
        manager = BackendManager(base_path=str(tmp_path))

        # Should not raise
        manager.cleanup("job-1", "nonexistent")


# ============================================================================
# BackendManager fallback tests
# ============================================================================


class TestBackendManagerFallback:
    """Tests for BackendManager fallback behavior."""

    def test_worktree_unavailable_fallback_to_local(self, tmp_path):
        """When worktree is unavailable, should fall back to local."""
        manager = BackendManager(base_path=str(tmp_path))

        # Patch WorktreeBackend.is_available to return False
        with patch.object(
            WorktreeBackend, "is_available", return_value=False
        ):
            work_dir = manager.setup(
                "job-1",
                "run-1",
                backend_type="worktree",
            )

        # Should have fallen back to local backend
        assert work_dir.exists()
        # Verify it used local by checking the active run
        active = manager.get_active_runs()
        assert "run-1" in active
        assert active["run-1"] == "local"

    def test_backend_singleton_reuse(self, tmp_path):
        """Same backend type should be reused (singleton pattern)."""
        manager = BackendManager(base_path=str(tmp_path))

        manager.setup("job-1", "run-1", backend_type="local")
        manager.setup("job-1", "run-2", backend_type="local")

        # Should only have one LocalBackend instance
        assert len(manager._backends) == 1
        assert "local" in manager._backends


# ============================================================================
# DockerBackend stub tests
# ============================================================================


class TestDockerBackend:
    """Tests for DockerBackend stub."""

    def test_backend_type(self):
        """DockerBackend should have correct backend_type."""
        assert DockerBackend.backend_type == BackendType.DOCKER

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

    def test_cleanup_raises(self):
        """cleanup() should raise NotImplementedError."""
        backend = DockerBackend()
        with pytest.raises(NotImplementedError) as exc_info:
            backend.cleanup("job-1", "run-1")
        assert "planned for M3" in str(exc_info.value)

    def test_preserve_raises(self):
        """preserve() should raise NotImplementedError."""
        backend = DockerBackend()
        with pytest.raises(NotImplementedError) as exc_info:
            backend.preserve("job-1", "run-1")
        assert "planned for M3" in str(exc_info.value)

    def test_get_work_dir_raises(self):
        """get_work_dir() should raise NotImplementedError."""
        backend = DockerBackend()
        with pytest.raises(NotImplementedError) as exc_info:
            backend.get_work_dir("job-1", "run-1")
        assert "planned for M3" in str(exc_info.value)


# ============================================================================
# ExecutionBackend interface tests
# ============================================================================


class TestExecutionBackendInterface:
    """Tests for the abstract base class behavior."""

    def test_cannot_instantiate_abstract(self):
        """ExecutionBackend should not be instantiable directly."""
        with pytest.raises(TypeError):
            ExecutionBackend()

    def test_backend_type_enum_values(self):
        """BackendType enum should have expected values."""
        assert BackendType.LOCAL == "local"
        assert BackendType.WORKTREE == "worktree"
        assert BackendType.DOCKER == "docker"

    def test_backend_type_from_string(self):
        """BackendType should be constructable from string."""
        assert BackendType("local") == BackendType.LOCAL
        assert BackendType("worktree") == BackendType.WORKTREE
        assert BackendType("docker") == BackendType.DOCKER
