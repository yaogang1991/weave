"""
Integration tests for execution backends.

Covers:
- Full lifecycle: setup -> execute -> cleanup
- Failure preservation: setup -> execute -> preserve
- Worktree isolation: two jobs do not overlap
- Backend fallback: worktree unavailable -> local
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure project root is on sys.path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.local import LocalBackend  # noqa: E402
from backend.worktree import WorktreeBackend  # noqa: E402
from backend.lifecycle import BackendManager  # noqa: E402


# ============================================================================
# Full lifecycle tests
# ============================================================================


class TestLocalBackendLifecycle:
    """Integration tests for LocalBackend full lifecycle."""

    def test_full_lifecycle_setup_cleanup(self, tmp_path):
        """Full lifecycle: setup -> work -> cleanup."""
        backend = LocalBackend(base_path=str(tmp_path))
        job_id = "job-1"
        run_id = "run-1"

        # Setup
        work_dir = backend.setup(job_id, run_id)
        assert work_dir.exists()

        # Simulate agent work
        (work_dir / "output.txt").write_text("task result")
        assert (work_dir / "output.txt").read_text() == "task result"

        # Cleanup
        backend.cleanup(job_id, run_id)
        assert not work_dir.exists()

    def test_full_lifecycle_setup_preserve(self, tmp_path):
        """Failure lifecycle: setup -> work -> preserve."""
        backend = LocalBackend(base_path=str(tmp_path))
        job_id = "job-1"
        run_id = "run-1"

        # Setup
        work_dir = backend.setup(job_id, run_id)
        (work_dir / "debug.log").write_text("error occurred")

        # Preserve on failure
        preserve_dir = backend.preserve(
            job_id, run_id, reason="execution failed"
        )

        # Original work_dir should be gone (moved to preserve)
        assert not work_dir.exists()
        # Preserved directory should exist with content
        assert preserve_dir.exists()

    def test_multiple_runs_same_job(self, tmp_path):
        """Multiple runs for the same job should have separate dirs."""
        backend = LocalBackend(base_path=str(tmp_path))
        job_id = "job-1"

        run1_dir = backend.setup(job_id, "run-1")
        run2_dir = backend.setup(job_id, "run-2")

        # Both should exist and be different
        assert run1_dir.exists()
        assert run2_dir.exists()
        assert run1_dir != run2_dir

        # Write different content to each
        (run1_dir / "file.txt").write_text("run1")
        (run2_dir / "file.txt").write_text("run2")

        assert (run1_dir / "file.txt").read_text() == "run1"
        assert (run2_dir / "file.txt").read_text() == "run2"

        # Cleanup run1 only
        backend.cleanup(job_id, "run-1")
        assert not run1_dir.exists()
        assert run2_dir.exists()

    def test_concurrent_job_isolation(self, tmp_path):
        """Different jobs should have isolated directories."""
        backend = LocalBackend(base_path=str(tmp_path))

        job_a_dir = backend.setup("job-A", "run-1")
        job_b_dir = backend.setup("job-B", "run-1")

        # Both should exist and be different
        assert job_a_dir.exists()
        assert job_b_dir.exists()
        assert job_a_dir != job_b_dir

        # Verify no path leakage
        assert "job-A" in str(job_a_dir)
        assert "job-B" in str(job_b_dir)


# ============================================================================
# WorktreeBackend integration tests
# ============================================================================


class TestWorktreeBackendLifecycle:
    """Integration tests for WorktreeBackend full lifecycle."""

    def _init_git_repo(self, path: Path) -> None:
        """Helper: Initialize a git repo for testing."""
        subprocess.run(
            ["git", "init"], cwd=str(path), capture_output=True
        )
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(path),
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(path),
            capture_output=True,
        )
        (path / "init.txt").write_text("init")
        subprocess.run(
            ["git", "add", "."], cwd=str(path), capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=str(path),
            capture_output=True,
        )

    def test_worktree_setup_and_cleanup(self, tmp_path):
        """Full worktree lifecycle: setup -> cleanup."""
        repo = tmp_path / "repo"
        repo.mkdir()
        self._init_git_repo(repo)

        backend = WorktreeBackend(
            repo_root=str(repo),
            base_path=str(tmp_path / "worktrees"),
        )

        # Skip if git worktree is not available in this environment
        if not backend.is_available():
            pytest.skip("git worktree not available")

        work_dir = backend.setup("job-1", "run-1")

        # Verify worktree was created
        assert work_dir.exists()
        # Should have the initial file
        assert (work_dir / "init.txt").exists()

        # Cleanup
        backend.cleanup("job-1", "run-1")
        assert not work_dir.exists()

    def test_worktree_isolation(self, tmp_path):
        """Two worktrees should be independent."""
        repo = tmp_path / "repo"
        repo.mkdir()
        self._init_git_repo(repo)

        backend = WorktreeBackend(
            repo_root=str(repo),
            base_path=str(tmp_path / "worktrees"),
        )

        if not backend.is_available():
            pytest.skip("git worktree not available")

        # Create two worktrees
        work_dir1 = backend.setup("job-1", "run-1")
        work_dir2 = backend.setup("job-1", "run-2")

        # They should be different paths
        assert work_dir1 != work_dir2
        assert work_dir1.exists()
        assert work_dir2.exists()

        # Modifying one should not affect the other
        (work_dir1 / "unique1.txt").write_text("from run 1")
        assert not (work_dir2 / "unique1.txt").exists()

        # Cleanup both
        backend.cleanup("job-1", "run-1")
        backend.cleanup("job-1", "run-2")
        assert not work_dir1.exists()
        assert not work_dir2.exists()

    def test_worktree_preserve_keeps_files(self, tmp_path):
        """Preserving worktree should keep files for debugging."""
        repo = tmp_path / "repo"
        repo.mkdir()
        self._init_git_repo(repo)

        backend = WorktreeBackend(
            repo_root=str(repo),
            base_path=str(tmp_path / "worktrees"),
        )

        if not backend.is_available():
            pytest.skip("git worktree not available")

        work_dir = backend.setup("job-1", "run-1")
        (work_dir / "debug.log").write_text("error trace")

        # Preserve on failure
        preserved = backend.preserve("job-1", "run-1", reason="test failure")

        # Preserved path should still exist
        assert preserved == work_dir
        assert preserved.exists()
        assert (preserved / "debug.log").exists()
        assert (preserved / ".PRESERVED").exists()

    def test_worktree_cleanup_after_preserve(self, tmp_path):
        """cleanup() after preserve() should handle gracefully."""
        repo = tmp_path / "repo"
        repo.mkdir()
        self._init_git_repo(repo)

        backend = WorktreeBackend(
            repo_root=str(repo),
            base_path=str(tmp_path / "worktrees"),
        )

        if not backend.is_available():
            pytest.skip("git worktree not available")

        backend.setup("job-1", "run-1")  # noqa: F841

        # Preserve first
        backend.preserve("job-1", "run-1")
        assert "run-1" not in backend.worktrees

        # Cleanup should handle gracefully (run_id already removed)
        backend.cleanup("job-1", "run-1")
        # Should not raise


# ============================================================================
# BackendManager integration tests
# ============================================================================


class TestBackendManagerLifecycle:
    """Integration tests for BackendManager full lifecycle."""

    def test_manager_full_lifecycle_local(self, tmp_path):
        """BackendManager with local backend: setup -> cleanup."""
        manager = BackendManager(
            workspace="local", base_path=str(tmp_path)
        )
        job_id = "job-1"
        run_id = "run-1"

        # Setup
        work_dir = manager.setup(job_id, run_id)
        assert work_dir.exists()
        (work_dir / "result.txt").write_text("success")

        # Cleanup
        manager.cleanup(job_id, run_id)
        assert not work_dir.exists()
        assert run_id not in manager.get_active_runs()

    def test_manager_preserve_on_failure(self, tmp_path):
        """BackendManager: setup -> preserve on failure."""
        manager = BackendManager(
            workspace="local", base_path=str(tmp_path)
        )
        job_id = "job-1"
        run_id = "run-1"

        # Setup
        work_dir = manager.setup(job_id, run_id)
        (work_dir / "debug.log").write_text("error trace")

        # Preserve
        preserve_dir = manager.preserve(
            job_id, run_id, reason="execution failed"
        )

        assert preserve_dir is not None
        assert run_id not in manager.get_active_runs()

    def test_manager_risk_based_selection_high(self, tmp_path):
        """High risk should use worktree backend (if available)."""
        manager = BackendManager(base_path=str(tmp_path))

        # Patch worktree availability to return True for this test
        with patch.object(
            WorktreeBackend, "is_available", return_value=True
        ):
            with patch.object(
                WorktreeBackend,
                "setup",
                return_value=tmp_path / "mock" / "worktree",
            ) as mock_setup:
                manager.setup(  # noqa: F841
                    "job-1", "run-1", risk_level="high"
                )
                mock_setup.assert_called_once()

    def test_manager_risk_based_selection_low(self, tmp_path):
        """Low risk should use local backend."""
        manager = BackendManager(base_path=str(tmp_path))

        work_dir = manager.setup(
            "job-1", "run-1", risk_level="low"
        )

        assert work_dir.exists()
        # Should be local backend
        assert manager.get_active_runs()["run-1"] == "local"

    def test_manager_fallback_worktree_to_local(self, tmp_path):
        """When worktree is unavailable, should fall back to local."""
        manager = BackendManager(
            workspace="worktree", base_path=str(tmp_path)
        )

        with patch.object(
            WorktreeBackend, "is_available", return_value=False
        ):
            work_dir = manager.setup(
                "job-1", "run-1", risk_level="high"
            )

        # Should have fallen back to local
        assert work_dir.exists()
        assert manager.get_active_runs()["run-1"] == "local"

    def test_manager_multiple_runs(self, tmp_path):
        """Multiple runs should be tracked independently."""
        manager = BackendManager(
            workspace="local", base_path=str(tmp_path)
        )

        manager.setup("job-1", "run-1")  # noqa: F841
        manager.setup("job-1", "run-2")  # noqa: F841
        manager.setup("job-2", "run-3")  # noqa: F841

        # All should be tracked
        active = manager.get_active_runs()
        assert len(active) == 3

        # Cleanup only run-1
        manager.cleanup("job-1", "run-1")
        assert len(manager.get_active_runs()) == 2
        assert "run-1" not in manager.get_active_runs()

        # Preserve run-2
        manager.preserve("job-1", "run-2")
        assert len(manager.get_active_runs()) == 1
        assert "run-2" not in manager.get_active_runs()

        # run-3 still active
        assert "run-3" in manager.get_active_runs()

    def test_manager_explicit_backend_override(self, tmp_path):
        """Explicit workspace_type should override default and risk."""
        manager = BackendManager(
            workspace="worktree", base_path=str(tmp_path)
        )

        # Explicitly request local despite default being worktree
        work_dir = manager.setup(
            "job-1",
            "run-1",
            workspace_type="local",
            risk_level="critical",
        )

        assert work_dir.exists()
        assert manager.get_active_runs()["run-1"] == "local"


# ============================================================================
# Worktree isolation tests
# ============================================================================


class TestWorktreeIsolation:
    """Tests verifying worktree isolation between jobs."""

    def _init_git_repo(self, path: Path) -> None:
        """Helper: Initialize a git repo for testing."""
        subprocess.run(
            ["git", "init"], cwd=str(path), capture_output=True
        )
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"],
            cwd=str(path),
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=str(path),
            capture_output=True,
        )
        (path / "init.txt").write_text("init")
        subprocess.run(
            ["git", "add", "."], cwd=str(path), capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "init"],
            cwd=str(path),
            capture_output=True,
        )

    def test_two_jobs_no_overlap(self, tmp_path):
        """Two jobs should have completely separate work directories."""
        repo = tmp_path / "repo"
        repo.mkdir()
        self._init_git_repo(repo)

        backend = WorktreeBackend(
            repo_root=str(repo),
            base_path=str(tmp_path / "worktrees"),
        )

        if not backend.is_available():
            pytest.skip("git worktree not available")

        job1_dir = backend.setup("job-1", "run-1")
        job2_dir = backend.setup("job-2", "run-1")

        # Verify no path overlap
        assert job1_dir != job2_dir
        # One should not be a subdirectory of the other
        assert not str(job1_dir).startswith(str(job2_dir))
        assert not str(job2_dir).startswith(str(job1_dir))

        backend.cleanup("job-1", "run-1")
        backend.cleanup("job-2", "run-1")

    def test_backend_manager_job_isolation(self, tmp_path):
        """BackendManager should maintain isolation between jobs."""
        manager = BackendManager(
            workspace="local", base_path=str(tmp_path)
        )

        dir1 = manager.setup("job-A", "run-1")
        dir2 = manager.setup("job-B", "run-1")

        # Verify paths are independent
        assert dir1 != dir2
        assert "job-A" in str(dir1)
        assert "job-B" in str(dir2)

        # Verify content isolation
        (dir1 / "file.txt").write_text("A")
        (dir2 / "file.txt").write_text("B")

        assert (dir1 / "file.txt").read_text() == "A"
        assert (dir2 / "file.txt").read_text() == "B"

        manager.cleanup("job-A", "run-1")
        manager.cleanup("job-B", "run-1")
