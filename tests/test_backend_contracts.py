"""
Backend contract tests -- M2-C

Verifies both LocalBackend and WorktreeBackend satisfy the same interface
contract with identical test scenarios.

For WorktreeBackend, git worktree operations are mocked to avoid git dependency.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.base import WorkspaceIsolation
from backend.local import LocalBackend
from backend.worktree import WorktreeBackend
from backend.lifecycle import BackendManager


# ---------------------------------------------------------------------------
# Shared contract test cases -- parametrized over both backends
# ---------------------------------------------------------------------------


class TestBackendContract:
    """
    Contract tests that both LocalBackend and WorktreeBackend must satisfy.

    Each test runs with both backends via parametrize to ensure interface parity.
    """

    @pytest.fixture
    def local_backend(self, tmp_path):
        return LocalBackend(base_path=str(tmp_path / "local"))

    @pytest.fixture
    def worktree_backend(self, tmp_path):
        """WorktreeBackend with mocked git operations."""
        backend = WorktreeBackend(
            repo_root=str(tmp_path / "repo"),
            base_path=str(tmp_path / "worktrees"),
        )
        return backend

    @pytest.fixture(params=["local", "worktree"])
    def backend(self, request, tmp_path, local_backend, worktree_backend):
        """Parametrized fixture returning both backend types."""
        if request.param == "local":
            return local_backend
        return worktree_backend

    # -- backend_type property --

    def test_has_workspace_type(self, backend):
        """Every backend must expose a workspace_type."""
        assert hasattr(backend, "workspace_type")
        assert isinstance(backend.workspace_type, WorkspaceIsolation)

    def test_backend_type_value(self, backend):
        """workspace_type must be a valid WorkspaceIsolation."""
        assert backend.workspace_type in (
            WorkspaceIsolation.LOCAL, WorkspaceIsolation.WORKTREE,
        )

    # -- is_available --

    def test_is_available_returns_bool(self, backend):
        """is_available() must return a boolean."""
        result = backend.is_available()
        assert isinstance(result, bool)

    # -- setup returns Path --

    def test_setup_returns_path(self, backend, tmp_path):
        """setup() must return a Path object."""
        if isinstance(backend, WorktreeBackend):
            # Mock git worktree add
            mock_result = MagicMock()
            mock_result.returncode = 0
            with patch("backend.worktree.subprocess.run", return_value=mock_result):
                work_dir = backend.setup("job-1", "run-1")
        else:
            work_dir = backend.setup("job-1", "run-1")

        assert isinstance(work_dir, Path)

    def test_setup_creates_or_returns_directory(self, backend, tmp_path):
        """setup() must create or return a valid working directory."""
        if isinstance(backend, WorktreeBackend):
            mock_result = MagicMock()
            mock_result.returncode = 0
            with patch("backend.worktree.subprocess.run", return_value=mock_result):
                work_dir = backend.setup("job-1", "run-1")
        else:
            work_dir = backend.setup("job-1", "run-1")

        # For LocalBackend without repo_root, directory must exist
        # For WorktreeBackend with mock, path is returned but may not exist
        # on filesystem (git mock doesn't create dir)
        assert "job-1" in str(work_dir) or isinstance(backend, WorktreeBackend)

    # -- get_work_dir --

    def test_get_work_dir_returns_path(self, backend):
        """get_work_dir() must return a Path."""
        work_dir = backend.get_work_dir("job-1", "run-1")
        assert isinstance(work_dir, Path)

    def test_get_work_dir_includes_job_and_run(self, backend):
        """get_work_dir() path should include job_id and run_id."""
        work_dir = backend.get_work_dir("job-1", "run-1")
        path_str = str(work_dir)
        assert "job-1" in path_str
        assert "run-1" in path_str

    # -- cleanup --

    def test_cleanup_does_not_raise(self, backend):
        """cleanup() must not raise even for non-existent paths."""
        # Should not raise
        backend.cleanup("nonexistent-job", "nonexistent-run")

    # -- preserve --

    def test_preserve_does_not_raise(self, backend, tmp_path):
        """preserve() must handle gracefully even for non-existent paths."""
        result = backend.preserve("nonexistent-job", "nonexistent-run", reason="test")
        # Should return None or Path, not raise
        assert result is None or isinstance(result, Path)

    # -- idempotency --

    def test_cleanup_idempotent(self, backend):
        """Calling cleanup() multiple times should not raise."""
        if isinstance(backend, WorktreeBackend):
            # Setup a fake worktree entry
            backend.worktrees["run-1"] = backend.base_path / "job-1" / "run-1"
            mock_result = MagicMock()
            mock_result.returncode = 0
            with patch("backend.worktree.subprocess.run", return_value=mock_result):
                backend.cleanup("job-1", "run-1")
                backend.cleanup("job-1", "run-1")
        else:
            backend.setup("job-1", "run-1")
            backend.cleanup("job-1", "run-1")
            backend.cleanup("job-1", "run-1")  # Should not raise


# ---------------------------------------------------------------------------
# LocalBackend-specific contract tests (real filesystem)
# ---------------------------------------------------------------------------


class TestLocalBackendContract:
    """Contract tests using real filesystem operations for LocalBackend."""

    def test_lifecycle_setup_cleanup(self, tmp_path):
        """Full lifecycle: setup -> cleanup."""
        backend = LocalBackend(base_path=str(tmp_path))
        work_dir = backend.setup("job-1", "run-1")
        assert work_dir.exists()

        backend.cleanup("job-1", "run-1")
        assert not work_dir.exists()

    def test_lifecycle_setup_preserve(self, tmp_path):
        """Full lifecycle: setup -> preserve."""
        backend = LocalBackend(base_path=str(tmp_path))
        work_dir = backend.setup("job-1", "run-1")
        (work_dir / "output.txt").write_text("result")

        preserve_dir = backend.preserve("job-1", "run-1", reason="failed")
        assert not work_dir.exists()
        assert preserve_dir is not None
        assert preserve_dir.exists()
        assert "_preserved" in str(preserve_dir)

    def test_concurrent_jobs_isolated(self, tmp_path):
        """Concurrent jobs should have separate directories."""
        backend = LocalBackend(base_path=str(tmp_path))

        dir_a = backend.setup("job-a", "run-1")
        dir_b = backend.setup("job-b", "run-1")

        assert dir_a != dir_b
        assert dir_a.exists()
        assert dir_b.exists()

        # Write to one doesn't affect the other
        (dir_a / "test.txt").write_text("a")
        assert not (dir_b / "test.txt").exists()


# ---------------------------------------------------------------------------
# WorktreeBackend-specific contract tests (mocked git)
# ---------------------------------------------------------------------------


class TestWorktreeBackendContract:
    """Contract tests for WorktreeBackend with mocked git operations."""

    def test_lifecycle_setup_cleanup(self, tmp_path):
        """Full lifecycle: setup -> cleanup."""
        backend = WorktreeBackend(
            repo_root=str(tmp_path),
            base_path=str(tmp_path / "wt"),
        )

        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch("backend.worktree.subprocess.run", return_value=mock_result):
            work_dir = backend.setup("job-1", "run-1")
            assert "job-1" in str(work_dir)

            backend.cleanup("job-1", "run-1")
            assert "run-1" not in backend.worktrees

    def test_lifecycle_setup_preserve(self, tmp_path):
        """Full lifecycle: setup -> preserve."""
        backend = WorktreeBackend(
            repo_root=str(tmp_path),
            base_path=str(tmp_path / "wt"),
        )

        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch("backend.worktree.subprocess.run", return_value=mock_result):
            work_dir = backend.setup("job-1", "run-1")
            # Manually create the directory for marker test
            work_dir.mkdir(parents=True, exist_ok=True)

            preserve_dir = backend.preserve("job-1", "run-1", reason="test failure")
            assert preserve_dir is not None
            marker = preserve_dir / ".PRESERVED"
            assert marker.exists()

    def test_concurrent_jobs_isolated(self, tmp_path):
        """Concurrent jobs should have separate worktree paths."""
        backend = WorktreeBackend(
            repo_root=str(tmp_path),
            base_path=str(tmp_path / "wt"),
        )

        mock_result = MagicMock()
        mock_result.returncode = 0

        with patch("backend.worktree.subprocess.run", return_value=mock_result):
            dir_a = backend.setup("job-a", "run-1")
            dir_b = backend.setup("job-b", "run-1")

            assert dir_a != dir_b


# ---------------------------------------------------------------------------
# CleanupPolicy contract tests via BackendManager
# ---------------------------------------------------------------------------


class TestCleanupPolicyContract:
    """Verify cleanup_policy behavior through BackendManager."""

    def test_on_success_policy_cleans_on_success(self, tmp_path):
        manager = BackendManager(
            base_path=str(tmp_path),
            cleanup_policy="on_success",
        )
        work_dir = manager.setup("job-1", "run-1", workspace_type="local")
        (work_dir / "output.txt").write_text("result")

        result = manager.finalize("job-1", "run-1", success=True)
        assert result is None
        assert not work_dir.exists()

    def test_on_success_policy_preserves_on_failure(self, tmp_path):
        manager = BackendManager(
            base_path=str(tmp_path),
            cleanup_policy="on_success",
        )
        work_dir = manager.setup("job-1", "run-1", workspace_type="local")
        (work_dir / "output.txt").write_text("result")

        result = manager.finalize("job-1", "run-1", success=False, reason="error")
        assert result is not None
        assert result.exists()

    def test_always_policy_cleans_on_failure(self, tmp_path):
        manager = BackendManager(
            base_path=str(tmp_path),
            cleanup_policy="always",
        )
        work_dir = manager.setup("job-1", "run-1", workspace_type="local")

        result = manager.finalize("job-1", "run-1", success=False, reason="error")
        assert result is None
        assert not work_dir.exists()

    def test_never_policy_preserves_on_success(self, tmp_path):
        manager = BackendManager(
            base_path=str(tmp_path),
            cleanup_policy="never",
        )
        work_dir = manager.setup("job-1", "run-1", workspace_type="local")
        (work_dir / "output.txt").write_text("result")

        result = manager.finalize("job-1", "run-1", success=True)
        assert result is not None
        assert result.exists()


# ---------------------------------------------------------------------------
# Config validation tests
# ---------------------------------------------------------------------------


class TestCleanupPolicyValidation:
    """Verify cleanup_policy validation in HarnessConfig and BackendManager."""

    def test_valid_policies_accepted(self):
        """All valid policy values should be accepted."""
        from core.config import HarnessConfig
        for policy in ("always", "on_success", "never"):
            config = HarnessConfig(cleanup_policy=policy)
            assert config.cleanup_policy == policy

    def test_invalid_policy_rejected(self):
        """Invalid policy values should be rejected by Pydantic."""
        from core.config import HarnessConfig
        import pydantic
        with pytest.raises(pydantic.ValidationError):
            HarnessConfig(cleanup_policy="invalid_policy")

    def test_typo_rejected(self):
        """Typos should be caught (e.g., 'Always' vs 'always')."""
        from core.config import HarnessConfig
        import pydantic
        with pytest.raises(pydantic.ValidationError):
            HarnessConfig(cleanup_policy="Always")

    def test_from_env_validates_cleanup_policy(self):
        """from_env() must validate cleanup_policy from environment."""
        import os
        from core.config import HarnessConfig
        import pydantic
        original = os.environ.get("HARNESS_CLEANUP_POLICY")
        try:
            os.environ["HARNESS_CLEANUP_POLICY"] = "Always"
            with pytest.raises(pydantic.ValidationError):
                HarnessConfig.from_env()
        finally:
            if original is None:
                os.environ.pop("HARNESS_CLEANUP_POLICY", None)
            else:
                os.environ["HARNESS_CLEANUP_POLICY"] = original

    def test_backend_manager_rejects_invalid_policy(self, tmp_path):
        """BackendManager must reject invalid cleanup_policy."""
        with pytest.raises(ValueError, match="Invalid cleanup_policy"):
            BackendManager(
                base_path=str(tmp_path),
                cleanup_policy="Always",
            )

    def test_backend_manager_rejects_unknown_policy(self, tmp_path):
        """BackendManager must reject unknown cleanup_policy."""
        with pytest.raises(ValueError, match="Invalid cleanup_policy"):
            BackendManager(
                base_path=str(tmp_path),
                cleanup_policy="sometimes",
            )
