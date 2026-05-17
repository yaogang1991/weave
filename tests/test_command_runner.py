"""
Tests for #179 PR 1: ToolCommandRunner interface and SyncSandboxAdapter.

Verifies the sync command runner protocol, adapter behavior, and
ToolRegistry integration with sandbox runners.
"""
from unittest.mock import MagicMock

from tools.command_runner import (
    ToolCommandResult,
    SyncSandboxAdapter,
)
from tools.registry import ToolRegistry
from core.models import ToolResult


class TestToolCommandResult:
    def test_result_fields(self):
        r = ToolCommandResult(returncode=0, stdout="hello", stderr="")
        assert r.returncode == 0
        assert r.stdout == "hello"
        assert not r.timed_out

    def test_timeout_result(self):
        r = ToolCommandResult(
            returncode=-1, stderr="timed out", timed_out=True,
        )
        assert r.timed_out
        assert r.returncode == -1


class TestSyncSandboxAdapter:
    def test_adapter_delegates_to_sandbox(self):
        from backend.sandbox import CommandResult

        mock_sandbox = MagicMock()
        mock_sandbox.run_command = MagicMock(
            return_value=CommandResult(
                success=True,
                exit_code=0,
                stdout="hello world",
                stderr="",
            ),
        )
        adapter = SyncSandboxAdapter(mock_sandbox)
        result = adapter.run_command("echo hello", cwd="/tmp")

        assert result.returncode == 0
        assert result.stdout == "hello world"
        assert not result.timed_out
        mock_sandbox.run_command.assert_called_once()

    def test_adapter_converts_failure(self):
        from backend.sandbox import CommandResult

        mock_sandbox = MagicMock()
        mock_sandbox.run_command = MagicMock(
            return_value=CommandResult(
                success=False,
                exit_code=1,
                stdout="",
                stderr="command not found",
            ),
        )
        adapter = SyncSandboxAdapter(mock_sandbox)
        result = adapter.run_command("bad_cmd", cwd="/tmp")

        assert result.returncode == 1
        assert result.stderr == "command not found"

    def test_adapter_handles_timeout(self):
        from backend.sandbox import CommandResult

        mock_sandbox = MagicMock()

        def slow_command(*args, **kwargs):
            import asyncio

            async def _slow():
                await asyncio.sleep(10)
                return CommandResult(success=True, exit_code=0)
            return _slow()

        mock_sandbox.run_command = slow_command
        adapter = SyncSandboxAdapter(mock_sandbox)
        result = adapter.run_command("slow_cmd", cwd="/tmp", timeout=1)

        assert result.timed_out
        assert result.returncode == -1

    def test_adapter_passes_env(self):
        from backend.sandbox import CommandResult

        mock_sandbox = MagicMock()
        mock_sandbox.run_command = MagicMock(
            return_value=CommandResult(success=True, exit_code=0),
        )
        adapter = SyncSandboxAdapter(mock_sandbox)
        adapter.run_command("echo $FOO", cwd="/tmp", env={"FOO": "bar"})

        # sandbox.run_command(command, cwd, timeout, env)
        call_args = mock_sandbox.run_command.call_args
        assert call_args[0][0] == "echo $FOO"
        assert call_args[0][1] == "/tmp"
        assert call_args[0][3] == {"FOO": "bar"}


class TestToolRegistryWithSandboxRunner:
    def test_uses_sandbox_runner_when_set(self):
        mock_runner = MagicMock()
        mock_runner.run_command = MagicMock(
            return_value=ToolCommandResult(
                returncode=0, stdout="hello from sandbox", stderr="",
            ),
        )
        registry = ToolRegistry(sandbox_runner=mock_runner)
        result = registry.execute("bash", {"command": "echo hello"})

        assert isinstance(result, ToolResult)
        assert result.success
        assert "hello from sandbox" in result.output
        mock_runner.run_command.assert_called_once()

    def test_subprocess_when_no_runner(self):
        registry = ToolRegistry()
        result = registry.execute("bash", {"command": "echo test123"})

        assert isinstance(result, ToolResult)
        assert result.success
        assert "test123" in result.output

    def test_sandbox_timeout_returns_tool_error(self):
        mock_runner = MagicMock()
        mock_runner.run_command = MagicMock(
            return_value=ToolCommandResult(
                returncode=-1, stderr="Command timed out after 5s", timed_out=True,
            ),
        )
        registry = ToolRegistry(sandbox_runner=mock_runner)
        result = registry.execute("bash", {"command": "slow_cmd", "timeout": 5})

        assert not result.success
        assert "timed out" in result.error.lower()

    def test_sandbox_failure_returns_tool_error(self):
        mock_runner = MagicMock()
        mock_runner.run_command = MagicMock(
            return_value=ToolCommandResult(
                returncode=1, stdout="", stderr="permission denied",
            ),
        )
        registry = ToolRegistry(sandbox_runner=mock_runner)
        result = registry.execute("bash", {"command": "forbidden"})

        assert not result.success
        assert "permission denied" in result.error

    def test_blocked_command_still_blocked_with_runner(self):
        mock_runner = MagicMock()
        mock_runner.run_command = MagicMock(
            return_value=ToolCommandResult(returncode=0, stdout=""),
        )
        registry = ToolRegistry(sandbox_runner=mock_runner)
        result = registry.execute("bash", {"command": "rm -rf /"})

        assert not result.success
        assert "Blocked" in result.error
        mock_runner.run_command.assert_not_called()
