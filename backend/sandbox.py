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
import os
from dataclasses import dataclass

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
    """Execute commands directly on the host (current default behavior).

    When env is None (default), builds a safe environment that strips
    sensitive keys (API tokens, passwords). When env is explicitly passed,
    uses it as-is — the caller is responsible for filtering.

    """

    sandbox_type = ExecutionSandbox.LOCAL

    SENSITIVE_ENV_PREFIXES: tuple[str, ...] = (
        "ANTHROPIC_",
        "OPENAI_",
        "AWS_",
        "GITHUB_TOKEN",
        "SECRET",
        "PASSWORD",
        "TOKEN",
        "API_KEY",
    )

    def _build_safe_env(self) -> dict[str, str]:
        """Build environment dict with sensitive keys removed."""
        import os

        return {
            k: v
            for k, v in os.environ.items()
            if not any(k.upper().startswith(p) for p in self.SENSITIVE_ENV_PREFIXES)

        }

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

        resolved_env = env if env is not None else self._build_safe_env()

        start = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=resolved_env,

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
    """Execute commands in a Docker container (#179 PR4).

    Provides process, network, and filesystem isolation by running commands
    inside a Docker container with the workspace mounted as a volume.

    Security defaults:
    - network_mode=none (no network access)
    - Only workspace directory is mounted
    - No host environment variables leaked by default
    """

    sandbox_type = ExecutionSandbox.DOCKER

    def __init__(
        self,
        image: str = "python:3.12-slim",
        network_mode: str = "none",
    ) -> None:
        self._image = image
        self._network_mode = network_mode

    async def run_command(
        self,
        command: str,
        cwd: str,
        timeout: int = 120,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        """Execute command in a Docker container with workspace mounted."""
        import asyncio
        import time

        if not self.is_available():
            raise NotImplementedError(
                "DockerSandbox requires Docker but it is not available. "
                "Install Docker or switch to 'local' sandbox."
            )

        docker_args = self._build_docker_args(command, cwd, env)
        start = time.monotonic()

        try:
            proc = await asyncio.create_subprocess_exec(
                *docker_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout,
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
            try:
                proc.kill()
                await proc.communicate()
            except (ProcessLookupError, OSError):
                pass
            return CommandResult(
                success=False,
                exit_code=-1,
                stderr=f"Command timed out after {timeout}s",
                duration_ms=int((time.monotonic() - start) * 1000),
            )

    def _build_docker_args(
        self,
        command: str,
        cwd: str,
        env: dict[str, str] | None = None,
    ) -> list[str]:
        """Build the docker run argument list (no shell, no injection risk)."""
        args = [
            "docker", "run", "--rm",
            "-v", f"{cwd}:/workspace",
            "--network", self._network_mode,
            "-w", "/workspace",
        ]

        # Pass env variables explicitly (whitelist, not host leak)
        if env:
            for key, value in env.items():
                args.extend(["-e", f"{key}={value}"])

        args.extend([self._image, "bash", "-c", command])

        return args

    def is_available(self) -> bool:
        """Check if Docker CLI is available on the host."""
        import subprocess

        try:
            result = subprocess.run(
                ["docker", "info"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return False
