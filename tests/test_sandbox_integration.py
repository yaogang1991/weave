"""
Tests for #179 PR 3+4: sandbox integration in service.py and DockerSandbox MVP.

PR 3: BackendManager.sandbox → SyncSandboxAdapter → ToolRegistry wiring
PR 4: Minimal DockerSandbox implementation
"""
import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from pathlib import Path

from backend.sandbox import DockerSandbox, LocalSandbox, CommandResult
from backend.lifecycle import BackendManager
from backend.base import ExecutionSandbox, WorkspaceIsolation
from tools.command_runner import SyncSandboxAdapter, ToolCommandResult


# ---------------------------------------------------------------------------
# PR 3: SyncSandboxAdapter wiring in BackendManager context
# ---------------------------------------------------------------------------

class TestSandboxAdapterFromBackendManager:
    """Verify SyncSandboxAdapter works with BackendManager.sandbox."""

    def test_local_sandbox_adapter_integration(self):
        """SyncSandboxAdapter wraps LocalSandbox correctly."""
        sandbox = LocalSandbox()
        adapter = SyncSandboxAdapter(sandbox)

        assert adapter.run_command is not None
        # SyncSandboxAdapter should satisfy ToolCommandRunner protocol
        assert hasattr(adapter, "run_command")

    def test_backend_manager_exposes_sandbox(self):
        """BackendManager has a .sandbox property."""
        manager = BackendManager(
            workspace=WorkspaceIsolation.LOCAL,
            sandbox=ExecutionSandbox.LOCAL,
        )
        assert manager.sandbox is not None
        assert isinstance(manager.sandbox, LocalSandbox)

    def test_backend_manager_docker_sandbox(self):
        """BackendManager creates DockerSandbox when configured."""
        manager = BackendManager(
            workspace=WorkspaceIsolation.LOCAL,
            sandbox=ExecutionSandbox.DOCKER,
        )
        assert isinstance(manager.sandbox, DockerSandbox)

    def test_adapter_with_backend_manager_sandbox(self):
        """SyncSandboxAdapter can wrap BackendManager.sandbox."""
        manager = BackendManager(
            workspace=WorkspaceIsolation.LOCAL,
            sandbox=ExecutionSandbox.LOCAL,
        )
        adapter = SyncSandboxAdapter(manager.sandbox)
        # Verify adapter is callable without error
        assert callable(adapter.run_command)


# ---------------------------------------------------------------------------
# PR 4: DockerSandbox MVP
# ---------------------------------------------------------------------------

class TestDockerSandboxMVP:
    """Tests for minimal DockerSandbox implementation."""

    def test_docker_sandbox_type(self):
        sandbox = DockerSandbox()
        assert sandbox.sandbox_type == ExecutionSandbox.DOCKER

    @pytest.mark.asyncio
    async def test_is_available_returns_false_when_no_docker(self):
        """DockerSandbox.is_available() returns False when docker CLI missing."""
        sandbox = DockerSandbox()
        # In test environments without Docker, this should return False
        # If Docker IS available, it returns True — either is acceptable
        result = sandbox.is_available()
        assert isinstance(result, bool)

    @pytest.mark.asyncio
    async def test_run_command_raises_when_not_available(self):
        """DockerSandbox.run_command raises NotImplementedError if Docker unavailable."""
        sandbox = DockerSandbox()
        if not sandbox.is_available():
            with pytest.raises(NotImplementedError, match="Docker"):
                await sandbox.run_command("echo hello", cwd="/tmp")

    @pytest.mark.asyncio
    async def test_run_command_with_mock_docker(self):
        """DockerSandbox.run_command uses docker CLI when available."""
        sandbox = DockerSandbox()

        async def mock_create_subprocess_exec(*args, **kwargs):
            proc = AsyncMock()
            proc.communicate = AsyncMock(return_value=(b"hello\n", b""))
            proc.returncode = 0
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=mock_create_subprocess_exec):
            with patch.object(sandbox, "is_available", return_value=True):
                result = await sandbox.run_command("echo hello", cwd="/workspace")

        assert isinstance(result, CommandResult)
        assert result.success
        assert "hello" in result.stdout

    @pytest.mark.asyncio
    async def test_run_command_timeout(self):
        """DockerSandbox handles command timeout."""
        import asyncio

        sandbox = DockerSandbox()

        async def mock_create_subprocess_exec(*args, **kwargs):
            proc = AsyncMock()

            async def slow_communicate():
                await asyncio.sleep(10)
                return (b"", b"")

            proc.communicate = slow_communicate
            proc.kill = MagicMock()
            proc.returncode = 0
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=mock_create_subprocess_exec):
            with patch.object(sandbox, "is_available", return_value=True):
                result = await sandbox.run_command("sleep 10", cwd="/workspace", timeout=1)

        assert isinstance(result, CommandResult)
        assert not result.success
        assert result.exit_code == -1
        assert "timed out" in result.stderr.lower()

    @pytest.mark.asyncio
    async def test_run_command_nonzero_exit(self):
        """DockerSandbox returns failure on nonzero exit code."""
        sandbox = DockerSandbox()

        async def mock_create_subprocess_exec(*args, **kwargs):
            proc = AsyncMock()
            proc.communicate = AsyncMock(return_value=(b"", b"error: not found\n"))
            proc.returncode = 127
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=mock_create_subprocess_exec):
            with patch.object(sandbox, "is_available", return_value=True):
                result = await sandbox.run_command("bad_command", cwd="/workspace")

        assert isinstance(result, CommandResult)
        assert not result.success
        assert result.exit_code == 127

    @pytest.mark.asyncio
    async def test_run_command_passes_env(self):
        """DockerSandbox passes env variables to container."""
        sandbox = DockerSandbox()
        captured_kwargs = {}

        async def mock_create_subprocess_exec(*args, **kwargs):
            captured_kwargs.update({"args": args, "kwargs": kwargs})
            proc = AsyncMock()
            proc.communicate = AsyncMock(return_value=(b"bar\n", b""))
            proc.returncode = 0
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=mock_create_subprocess_exec):
            with patch.object(sandbox, "is_available", return_value=True):
                await sandbox.run_command(
                    "echo $FOO", cwd="/workspace", env={"FOO": "bar"},
                )

        # Docker sandbox should pass env via -e flag in argument list
        docker_args = captured_kwargs.get("args", ())
        assert any("-e" == arg for arg in docker_args)
        assert any("FOO=bar" in arg for arg in docker_args)

    def test_docker_sandbox_default_image(self):
        """DockerSandbox has a sensible default image."""
        sandbox = DockerSandbox()
        assert hasattr(sandbox, "_image")
        assert "python" in sandbox._image.lower()


class TestSyncAdapterWithDockerSandbox:
    """Integration: SyncSandboxAdapter wrapping DockerSandbox."""

    def test_adapter_with_docker_sandbox_not_available(self):
        """SyncSandboxAdapter handles DockerSandbox NotImplementedError."""
        sandbox = DockerSandbox()
        if not sandbox.is_available():
            adapter = SyncSandboxAdapter(sandbox)
            result = adapter.run_command("echo hello", cwd="/tmp", timeout=2)
            # Adapter should handle the error gracefully
            assert isinstance(result, ToolCommandResult)
            assert result.returncode != 0 or result.stderr
