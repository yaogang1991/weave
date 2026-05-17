"""Tests for backend architecture refactor (orthogonal dimensions)."""

import asyncio  # noqa: F401
import pytest

from backend.base import WorkspaceIsolation, ExecutionSandbox
from backend.local import LocalBackend
from backend.worktree import WorktreeBackend
from backend.sandbox import LocalSandbox, DockerSandbox
from backend.lifecycle import BackendManager, HookError


class TestWorkspaceIsolation:
    def test_values(self):
        assert WorkspaceIsolation.LOCAL == "local"
        assert WorkspaceIsolation.WORKTREE == "worktree"

    def test_from_string(self):
        assert WorkspaceIsolation("local") == WorkspaceIsolation.LOCAL
        assert WorkspaceIsolation("worktree") == WorkspaceIsolation.WORKTREE


class TestExecutionSandbox:
    def test_values(self):
        assert ExecutionSandbox.LOCAL == "local"
        assert ExecutionSandbox.DOCKER == "docker"


class TestLocalBackend:
    def test_workspace_type(self):
        backend = LocalBackend()
        assert backend.workspace_type == WorkspaceIsolation.LOCAL

    def test_is_available(self):
        backend = LocalBackend()
        assert backend.is_available() is True


class TestWorktreeBackend:
    def test_workspace_type(self):
        backend = WorktreeBackend(repo_root="/tmp")
        assert backend.workspace_type == WorkspaceIsolation.WORKTREE


class TestLocalSandbox:
    def test_sandbox_type(self):
        sandbox = LocalSandbox()
        assert sandbox.sandbox_type == ExecutionSandbox.LOCAL
        assert sandbox.is_available() is True

    @pytest.mark.asyncio
    async def test_run_command_success(self, tmp_path):
        sandbox = LocalSandbox()
        result = await sandbox.run_command("echo hello", str(tmp_path))
        assert result.success is True
        assert "hello" in result.stdout

    @pytest.mark.asyncio
    async def test_run_command_failure(self, tmp_path):
        sandbox = LocalSandbox()
        result = await sandbox.run_command("exit 1", str(tmp_path))
        assert result.success is False
        assert result.exit_code == 1

    @pytest.mark.asyncio
    async def test_run_command_timeout(self, tmp_path):
        sandbox = LocalSandbox()
        result = await sandbox.run_command("sleep 10", str(tmp_path), timeout=1)
        assert result.success is False
        assert "timed out" in result.stderr


class TestDockerSandbox:
    def test_not_implemented(self):
        sandbox = DockerSandbox()
        assert sandbox.is_available() is False

    @pytest.mark.asyncio
    async def test_run_command_raises(self):
        sandbox = DockerSandbox()
        with pytest.raises(NotImplementedError):
            await sandbox.run_command("echo hello", "/tmp")


class TestBackendManager:
    def test_default_composition(self):
        manager = BackendManager(
            workspace="local",
            sandbox="local",
        )
        assert manager.workspace_type == WorkspaceIsolation.LOCAL
        assert manager.sandbox_type == ExecutionSandbox.LOCAL

    def test_worktree_local_composition(self):
        manager = BackendManager(
            workspace="worktree",
            sandbox="local",
        )
        assert manager.workspace_type == WorkspaceIsolation.WORKTREE
        assert manager.sandbox_type == ExecutionSandbox.LOCAL

    def test_sandbox_provider_accessible(self):
        manager = BackendManager()
        assert isinstance(manager.sandbox, LocalSandbox)

    def test_setup_local(self, tmp_path):
        manager = BackendManager(
            workspace="local",
            base_path=str(tmp_path / "backends"),
        )
        work_dir = manager.setup("job1", "run1")
        assert work_dir.exists()

    def test_cleanup(self, tmp_path):
        manager = BackendManager(
            workspace="local",
            base_path=str(tmp_path / "backends"),
        )
        manager.setup("job1", "run1")
        manager.cleanup("job1", "run1")

    def test_resolve_by_risk(self):
        manager = BackendManager(
            workspace="local",
            workspace_by_risk={"high": "worktree", "critical": "worktree"},
        )
        ws = manager._resolve_workspace_type(None, "high")
        assert ws == WorkspaceIsolation.WORKTREE

        ws = manager._resolve_workspace_type(None, "low")
        assert ws == WorkspaceIsolation.LOCAL

    def test_explicit_overrides_risk(self):
        manager = BackendManager(
            workspace="local",
            workspace_by_risk={"high": "worktree"},
        )
        ws = manager._resolve_workspace_type("local", "high")
        assert ws == WorkspaceIsolation.LOCAL  # explicit wins

    def test_get_active_runs(self, tmp_path):
        manager = BackendManager(
            workspace="local",
            base_path=str(tmp_path / "backends"),
        )
        manager.setup("job1", "run1")
        active = manager.get_active_runs()
        assert "run1" in active
        assert active["run1"] == "local"

    @pytest.mark.asyncio
    async def test_execute_hook_success(self, tmp_path):
        manager = BackendManager(workspace="local")
        (tmp_path / "test.txt").touch()
        result = await manager.execute_hook("after_create", "echo done", tmp_path)
        assert result.success is True
        assert "done" in result.output

    @pytest.mark.asyncio
    async def test_execute_hook_fatal_failure(self, tmp_path):
        manager = BackendManager(workspace="local")
        with pytest.raises(HookError) as exc_info:
            await manager.execute_hook("before_run", "exit 1", tmp_path)
        assert exc_info.value.hook_name == "before_run"

    @pytest.mark.asyncio
    async def test_execute_hook_nonfatal_failure(self, tmp_path):
        manager = BackendManager(workspace="local")
        result = await manager.execute_hook("after_run", "exit 1", tmp_path)
        assert result.success is False  # no exception raised

    @pytest.mark.asyncio
    async def test_execute_hook_timeout_fatal(self, tmp_path):
        manager = BackendManager(workspace="local")
        with pytest.raises(HookError):
            await manager.execute_hook("after_create", "sleep 10", tmp_path, timeout=1)

    @pytest.mark.asyncio
    async def test_execute_hook_timeout_nonfatal(self, tmp_path):
        manager = BackendManager(workspace="local")
        result = await manager.execute_hook("before_remove", "sleep 10", tmp_path, timeout=1)
        assert result.success is False
        assert "timed out" in result.error
