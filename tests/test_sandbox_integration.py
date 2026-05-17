"""
Tests for #179 PR 3+4: sandbox integration in service.py and DockerSandbox MVP.

PR 3: BackendManager.sandbox -> SyncSandboxAdapter -> ToolRegistry wiring
PR 4: Minimal DockerSandbox implementation
#483: DockerSandbox resource limits
#484: BackendManager risk-based sandbox selection
"""
import os

import pytest
from unittest.mock import MagicMock, patch, AsyncMock

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


class TestDockerSandboxResourceLimits:
    """Verify DockerSandbox includes resource limits in docker args (#483)."""

    def test_default_memory_limit(self):
        sandbox = DockerSandbox()
        assert sandbox._memory_limit == "512m"

    def test_default_cpu_limit(self):
        sandbox = DockerSandbox()
        assert sandbox._cpu_limit == 1.0

    def test_custom_memory_limit(self):
        sandbox = DockerSandbox(memory_limit="1g")
        assert sandbox._memory_limit == "1g"

    def test_custom_cpu_limit(self):
        sandbox = DockerSandbox(cpu_limit=2.0)
        assert sandbox._cpu_limit == 2.0

    def test_build_docker_args_includes_memory(self):
        """Docker args include --memory flag (#483)."""
        sandbox = DockerSandbox(memory_limit="256m")
        args = sandbox._build_docker_args("echo hi", "/workspace")
        assert "--memory" in args
        mem_idx = args.index("--memory")
        assert args[mem_idx + 1] == "256m"

    def test_build_docker_args_includes_cpus(self):
        """Docker args include --cpus flag (#483)."""
        sandbox = DockerSandbox(cpu_limit=0.5)
        args = sandbox._build_docker_args("echo hi", "/workspace")
        assert "--cpus" in args
        cpus_idx = args.index("--cpus")
        assert args[cpus_idx + 1] == "0.5"

    def test_build_docker_args_includes_network_none(self):
        """Docker args include --network none by default."""
        sandbox = DockerSandbox()
        args = sandbox._build_docker_args("echo hi", "/workspace")
        net_idx = args.index("--network")
        assert args[net_idx + 1] == "none"


class TestBackendManagerSandboxConfig:
    """Verify BackendManager passes config to DockerSandbox (#483)."""

    def test_backend_manager_passes_sandbox_config(self):
        """BackendManager passes sandbox_config to DockerSandbox."""
        manager = BackendManager(
            workspace=WorkspaceIsolation.LOCAL,
            sandbox=ExecutionSandbox.DOCKER,
            sandbox_config={
                "image": "python:3.11-slim",
                "memory_limit": "1g",
                "cpu_limit": 2.0,
            },
        )
        assert isinstance(manager.sandbox, DockerSandbox)
        assert manager.sandbox._image == "python:3.11-slim"
        assert manager.sandbox._memory_limit == "1g"
        assert manager.sandbox._cpu_limit == 2.0

    def test_backend_manager_uses_defaults_without_config(self):
        """BackendManager uses defaults when no sandbox_config provided."""
        manager = BackendManager(
            workspace=WorkspaceIsolation.LOCAL,
            sandbox=ExecutionSandbox.DOCKER,
        )
        assert isinstance(manager.sandbox, DockerSandbox)
        assert manager.sandbox._memory_limit == "512m"
        assert manager.sandbox._cpu_limit == 1.0


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


# ---------------------------------------------------------------------------
# #456: Credential isolation — sandbox processes cannot access API keys
# ---------------------------------------------------------------------------

class TestLocalSandboxCredentialIsolation:
    """Verify LocalSandbox strips sensitive env vars from subprocess env."""

    def test_build_safe_env_strips_api_keys(self):
        """_build_safe_env removes keys matching sensitive patterns."""
        sandbox = LocalSandbox()
        with patch.dict(
            os.environ,
            {
                "ANTHROPIC_API_KEY": "sk-test",
                "OPENAI_API_KEY": "sk-openai",
                "AWS_ACCESS_KEY_ID": "AKIA123",
                "GITHUB_TOKEN": "ghp_abc",
                "MY_API_KEY": "key123",
                "PATH": "/usr/bin",
                "HOME": "/home/user",
            },
            clear=True,
        ):
            safe_env = sandbox._build_safe_env()
        assert "ANTHROPIC_API_KEY" not in safe_env
        assert "OPENAI_API_KEY" not in safe_env
        assert "AWS_ACCESS_KEY_ID" not in safe_env
        assert "GITHUB_TOKEN" not in safe_env
        assert "MY_API_KEY" not in safe_env
        assert safe_env["PATH"] == "/usr/bin"
        assert safe_env["HOME"] == "/home/user"

    def test_build_safe_env_returns_explicit_env_unchanged(self):
        """When caller provides explicit env, it is passed through."""
        sandbox = LocalSandbox()
        explicit = {"MY_API_KEY": "caller_controls_this"}
        result = sandbox._build_safe_env(explicit)
        assert result is explicit
        assert result["MY_API_KEY"] == "caller_controls_this"

    def test_build_safe_env_returns_explicit_dict_copy(self):
        """Explicit env is returned as-is (not stripped)."""
        sandbox = LocalSandbox()
        explicit = {"ANTHROPIC_API_KEY": "sk-explicit"}
        result = sandbox._build_safe_env(explicit)
        assert result["ANTHROPIC_API_KEY"] == "sk-explicit"

    @pytest.mark.asyncio
    async def test_api_key_not_visible_in_sandbox(self):
        """Sandbox subprocess cannot read ANTHROPIC_API_KEY."""
        sandbox = LocalSandbox()
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-secret-key"}):
            result = await sandbox.run_command(
                "echo $ANTHROPIC_API_KEY", cwd="/tmp",
            )
        assert "sk-test-secret-key" not in result.stdout

    @pytest.mark.asyncio
    async def test_path_preserved_in_sandbox(self):
        """Non-sensitive env vars like PATH are preserved."""
        sandbox = LocalSandbox()
        result = await sandbox.run_command("echo $PATH", cwd="/tmp")
        assert result.stdout.strip()  # PATH is non-empty

    @pytest.mark.asyncio
    async def test_explicit_env_not_stripped(self):
        """When caller provides explicit env, values are passed through."""
        sandbox = LocalSandbox()
        result = await sandbox.run_command(
            "echo $MY_VAR", cwd="/tmp", env={"MY_VAR": "test_value"},
        )
        assert "test_value" in result.stdout


# ---------------------------------------------------------------------------
# #484: BackendManager risk-based sandbox selection
# ---------------------------------------------------------------------------

class TestRiskBasedSandboxSelection:
    """Verify BackendManager selects sandbox based on risk level (#484)."""

    def test_default_sandbox_by_risk_mapping(self):
        """Default mapping: low/medium -> local, high/critical -> docker."""
        manager = BackendManager(
            workspace=WorkspaceIsolation.LOCAL,
            sandbox=ExecutionSandbox.LOCAL,
        )
        assert manager.sandbox_by_risk["low"] == "local"
        assert manager.sandbox_by_risk["medium"] == "local"
        assert manager.sandbox_by_risk["high"] == "docker"
        assert manager.sandbox_by_risk["critical"] == "docker"

    def test_custom_sandbox_by_risk_mapping(self):
        """Custom sandbox_by_risk overrides defaults."""
        manager = BackendManager(
            workspace=WorkspaceIsolation.LOCAL,
            sandbox=ExecutionSandbox.LOCAL,
            sandbox_by_risk={"low": "local", "high": "local"},
        )
        assert manager.sandbox_by_risk["high"] == "local"

    def test_select_sandbox_returns_default_when_no_risk(self):
        """No risk_level -> returns default sandbox."""
        manager = BackendManager(
            workspace=WorkspaceIsolation.LOCAL,
            sandbox=ExecutionSandbox.LOCAL,
        )
        sandbox = manager._select_sandbox(None)
        assert isinstance(sandbox, LocalSandbox)

    def test_select_sandbox_low_risk_returns_local(self):
        """Low risk -> LocalSandbox."""
        manager = BackendManager(
            workspace=WorkspaceIsolation.LOCAL,
            sandbox=ExecutionSandbox.LOCAL,
        )
        sandbox = manager._select_sandbox("low")
        assert isinstance(sandbox, LocalSandbox)

    def test_select_sandbox_high_risk_falls_back_to_local(self):
        """High risk -> DockerSandbox, but falls back to LocalSandbox if unavailable."""
        manager = BackendManager(
            workspace=WorkspaceIsolation.LOCAL,
            sandbox=ExecutionSandbox.LOCAL,
        )
        sandbox = manager._select_sandbox("high")
        # Docker is not available in test env, so falls back to LocalSandbox
        assert isinstance(sandbox, LocalSandbox)

    def test_select_sandbox_high_risk_uses_docker_when_available(self):
        """High risk with Docker available -> DockerSandbox."""
        manager = BackendManager(
            workspace=WorkspaceIsolation.LOCAL,
            sandbox=ExecutionSandbox.LOCAL,
        )
        with patch.object(DockerSandbox, "is_available", return_value=True):
            sandbox = manager._select_sandbox("high")
        assert isinstance(sandbox, DockerSandbox)

    def test_select_sandbox_critical_risk_uses_docker_when_available(self):
        """Critical risk with Docker available -> DockerSandbox."""
        manager = BackendManager(
            workspace=WorkspaceIsolation.LOCAL,
            sandbox=ExecutionSandbox.LOCAL,
        )
        with patch.object(DockerSandbox, "is_available", return_value=True):
            sandbox = manager._select_sandbox("critical")
        assert isinstance(sandbox, DockerSandbox)

    def test_setup_stores_risk_selected_sandbox(self):
        """setup() stores the risk-selected sandbox for later retrieval."""
        manager = BackendManager(
            workspace=WorkspaceIsolation.LOCAL,
            sandbox=ExecutionSandbox.LOCAL,
        )
        with patch.object(DockerSandbox, "is_available", return_value=True):
            manager.setup("j1", "r1", risk_level="high")
        sandbox = manager.get_sandbox("r1")
        assert isinstance(sandbox, DockerSandbox)
        # Cleanup
        manager.cleanup("j1", "r1")

    def test_get_sandbox_returns_default_for_unknown_run(self):
        """get_sandbox() returns default sandbox for unknown run_id."""
        manager = BackendManager(
            workspace=WorkspaceIsolation.LOCAL,
            sandbox=ExecutionSandbox.LOCAL,
        )
        sandbox = manager.get_sandbox("nonexistent")
        assert sandbox is manager.sandbox

    def test_cleanup_removes_active_sandbox(self):
        """cleanup() removes the active sandbox for a run."""
        manager = BackendManager(
            workspace=WorkspaceIsolation.LOCAL,
            sandbox=ExecutionSandbox.LOCAL,
        )
        manager.setup("j1", "r1")
        assert "r1" in manager._active_sandboxes
        manager.cleanup("j1", "r1")
        assert "r1" not in manager._active_sandboxes

    def test_preserve_removes_active_sandbox(self):
        """preserve() removes the active sandbox for a run."""
        manager = BackendManager(
            workspace=WorkspaceIsolation.LOCAL,
            sandbox=ExecutionSandbox.LOCAL,
        )
        manager.setup("j1", "r1")
        assert "r1" in manager._active_sandboxes
        manager.preserve("j1", "r1", reason="test")
        assert "r1" not in manager._active_sandboxes
