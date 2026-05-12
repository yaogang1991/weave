"""
Tests for BackendLifecycleService (#177 PR 2).

Verifies that backend lifecycle (workspace setup, hooks, cleanup) is
properly encapsulated and testable without full job execution.
"""
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from pathlib import Path

from control_plane.backend_lifecycle import BackendLifecycleService


class TestLoadProjectHooks:
    """Static hook loading from .harness/config.yaml."""

    def test_loads_hooks_from_config(self, tmp_path):
        """Loads all hook types from config.yaml."""
        harness_dir = tmp_path / ".harness"
        harness_dir.mkdir()
        (harness_dir / "config.yaml").write_text(
            "hooks:\n  after_create: echo created\n  before_run: echo before\n"
            "  after_run: echo after\n  before_remove: echo remove\n",
            encoding="utf-8",
        )
        hooks = BackendLifecycleService.load_project_hooks(str(tmp_path))
        assert hooks["after_create"] == "echo created"
        assert hooks["before_run"] == "echo before"
        assert hooks["after_run"] == "echo after"
        assert hooks["before_remove"] == "echo remove"

    def test_returns_empty_when_no_project(self):
        hooks = BackendLifecycleService.load_project_hooks(None)
        assert hooks == {}

    def test_returns_empty_when_no_config(self, tmp_path):
        hooks = BackendLifecycleService.load_project_hooks(str(tmp_path))
        assert hooks == {}

    def test_returns_empty_on_bad_yaml(self, tmp_path):
        harness_dir = tmp_path / ".harness"
        harness_dir.mkdir()
        (harness_dir / "config.yaml").write_text("{{invalid yaml", encoding="utf-8")
        hooks = BackendLifecycleService.load_project_hooks(str(tmp_path))
        assert hooks == {}


class TestCreateBackendManager:
    """BackendManager creation from environment config."""

    @patch("control_plane.backend_lifecycle.HarnessConfig")
    def test_creates_manager_with_config(self, mock_config_cls):
        """Creates BackendManager using HarnessConfig."""
        mock_config = MagicMock()
        mock_config.sandbox.runtime = "local"
        mock_config.default_backend = "local"
        mock_config.risk_backend_map = {}
        mock_config.cleanup_policy = "always"
        mock_config_cls.from_env.return_value = mock_config

        with patch("control_plane.backend_lifecycle.BackendManager") as mock_bm_cls:
            bls = BackendLifecycleService()
            bls.create_backend_manager("/tmp/project")
            mock_bm_cls.assert_called_once()

    @patch("control_plane.backend_lifecycle.HarnessConfig")
    def test_falls_back_to_local_sandbox(self, mock_config_cls):
        """Unknown sandbox type falls back to 'local'."""
        mock_config = MagicMock()
        mock_config.sandbox.runtime = "unknown_runtime"
        mock_config.default_backend = "local"
        mock_config.risk_backend_map = {}
        mock_config.cleanup_policy = "always"
        mock_config_cls.from_env.return_value = mock_config

        with patch("control_plane.backend_lifecycle.BackendManager") as mock_bm_cls:
            bls = BackendLifecycleService()
            bls.create_backend_manager("/tmp/project")
            call_kwargs = mock_bm_cls.call_args[1]
            assert call_kwargs["sandbox"].value == "local"


class TestRunHook:
    """Lifecycle hook execution."""

    @pytest.mark.asyncio
    async def test_runs_hook_when_present(self):
        bls = BackendLifecycleService()
        mock_bm = MagicMock()
        mock_bm.execute_hook = AsyncMock()
        hooks = {"after_create": "echo hello"}

        await bls.run_hook(mock_bm, "after_create", hooks, "/tmp/work")
        mock_bm.execute_hook.assert_called_once_with(
            "after_create", "echo hello", "/tmp/work",
        )

    @pytest.mark.asyncio
    async def test_skips_hook_when_missing(self):
        bls = BackendLifecycleService()
        mock_bm = MagicMock()
        mock_bm.execute_hook = AsyncMock()
        hooks = {}

        await bls.run_hook(mock_bm, "after_create", hooks, "/tmp/work")
        mock_bm.execute_hook.assert_not_called()

    @pytest.mark.asyncio
    async def test_swallows_hook_error(self):
        bls = BackendLifecycleService()
        mock_bm = MagicMock()
        mock_bm.execute_hook = AsyncMock(side_effect=RuntimeError("hook failed"))
        hooks = {"after_create": "bad_command"}

        # Should not raise
        await bls.run_hook(mock_bm, "after_create", hooks, "/tmp/work")


class TestPreserveAndCleanup:
    """Workspace preserve and cleanup with error swallowing."""

    def test_preserve_delegates(self):
        bls = BackendLifecycleService()
        mock_bm = MagicMock()
        bls.preserve(mock_bm, "job1", "run1", reason="failed")
        mock_bm.preserve.assert_called_once_with("job1", "run1", reason="failed")

    def test_preserve_swallows_error(self):
        bls = BackendLifecycleService()
        mock_bm = MagicMock()
        mock_bm.preserve.side_effect = RuntimeError("disk full")
        # Should not raise
        bls.preserve(mock_bm, "job1", "run1")

    def test_cleanup_delegates(self):
        bls = BackendLifecycleService()
        mock_bm = MagicMock()
        bls.cleanup(mock_bm, "job1", "run1")
        mock_bm.cleanup.assert_called_once_with("job1", "run1")

    def test_cleanup_swallows_error(self):
        bls = BackendLifecycleService()
        mock_bm = MagicMock()
        mock_bm.cleanup.side_effect = RuntimeError("permission denied")
        # Should not raise
        bls.cleanup(mock_bm, "job1", "run1")


class TestSetupWorkspace:
    """Workspace setup delegation."""

    def test_setup_delegates(self):
        bls = BackendLifecycleService()
        mock_bm = MagicMock()
        mock_bm.setup.return_value = "/tmp/work/job1/run1"

        result = bls.setup_workspace(mock_bm, "job1", "run1", risk_level="high")
        assert result == "/tmp/work/job1/run1"
        mock_bm.setup.assert_called_once_with(
            job_id="job1", run_id="run1", risk_level="high",
        )
