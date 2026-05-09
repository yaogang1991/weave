"""
Sandbox providers — control where agent processes run.

This is the second orthogonal dimension of isolation (workspace isolation
is the first). Sandbox providers determine the execution environment:
host process, Docker container, or future VM.

Note: Hooks are NOT executed through SandboxProvider. Hooks run as
subprocess on the host (matching Symphony's design), because they are
operational scripts (npm install, pytest) that need access to the host.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field

from backend.base import ExecutionSandbox


@dataclass
class CommandResult:
    """Result of a sandbox command execution."""
    success: bool
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0


class SandboxProvider(abc.ABC):
    """Abstract base class for execution sandboxes."""

    sandbox_type: ExecutionSandbox

    @abc.abstractmethod
    async def run_command(
        self,
        command: str,
        cwd: str,
        timeout: int = 120,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        """Execute a command in the sandbox environment.

        Args:
            command: Shell command to execute.
            cwd: Working directory.
            timeout: Maximum execution time in seconds.
            env: Optional environment variables.

        Returns:
            CommandResult with success status and output.
        """
        ...

    @abc.abstractmethod
    def is_available(self) -> bool:
        """Check if this sandbox provider is available."""
        ...


class LocalSandbox(SandboxProvider):
    """Execute commands directly on the host (current default behavior)."""

    sandbox_type = ExecutionSandbox.LOCAL

    async def run_command(
        self,
        command: str,
        cwd: str,
        timeout: int = 120,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        """Execute command as a subprocess on the host."""
        import asyncio
        import time

        start = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
            elapsed = int((time.monotonic() - start) * 1000)

            return CommandResult(
                success=proc.returncode == 0,
                exit_code=proc.returncode or 0,
                stdout=stdout.decode(errors="replace"),
                stderr=stderr.decode(errors="replace"),
                duration_ms=elapsed,
            )
        except asyncio.TimeoutError:
            proc.kill()  # type: ignore[union-attr]
            # Await process cleanup to reap child and drain pipes
            try:
                await proc.communicate()
            except (ProcessLookupError, OSError):
                pass
            return CommandResult(
                success=False,
                exit_code=-1,
                stderr=f"Command timed out after {timeout}s",
                duration_ms=int((time.monotonic() - start) * 1000),
            )

    def is_available(self) -> bool:
        return True


class DockerSandbox(SandboxProvider):
    """Execute commands in a Docker container (M3+ stub).

    This is a placeholder for future implementation.
    Docker sandbox provides full process/network/filesystem isolation.
    """

    sandbox_type = ExecutionSandbox.DOCKER

    async def run_command(
        self,
        command: str,
        cwd: str,
        timeout: int = 120,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        raise NotImplementedError(
            "DockerSandbox not yet implemented (planned for M3)"
        )

    def is_available(self) -> bool:
        return False
