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

    Resource limits (#482): Uses resource.setrlimit() to constrain child
    process memory and CPU usage. Limits are configurable via constructor.
    On platforms where setrlimit is unsupported or fails, limits are
    silently skipped with a debug log.
    """

    sandbox_type = ExecutionSandbox.LOCAL

    # Default resource limits
    DEFAULT_MEMORY_LIMIT_MB: int = 2048  # 2 GB
    DEFAULT_CPU_LIMIT_SEC: int = 300  # 5 minutes

    SENSITIVE_ENV_PREFIXES: tuple[str, ...] = (
        "ANTHROPIC_",
        "OPENAI_",
        "AWS_",
    )

    SENSITIVE_ENV_CONTAINS: tuple[str, ...] = (
        "API_KEY",
        "API_SECRET",
        "SECRET",
        "PASSWORD",
        "TOKEN",
        "PRIVATE_KEY",
        "GITHUB_TOKEN",
    )

    def __init__(
        self,
        memory_limit_mb: int = DEFAULT_MEMORY_LIMIT_MB,
        cpu_limit_sec: int = DEFAULT_CPU_LIMIT_SEC,
    ) -> None:
        self._memory_limit_mb = memory_limit_mb
        self._cpu_limit_sec = cpu_limit_sec

    def _build_safe_env(self, env: dict[str, str] | None = None) -> dict[str, str]:
        """Build environment dict with sensitive keys removed.

        If caller provides explicit env, return it unchanged (caller controls
        env). If env is None, build from os.environ with sensitive keys
        stripped.
        """
        import os

        if env is not None:
            return env

        return {
            k: v
            for k, v in os.environ.items()
            if not any(k.upper().startswith(p) for p in self.SENSITIVE_ENV_PREFIXES)
            and not any(p in k.upper() for p in self.SENSITIVE_ENV_CONTAINS)
        }

    def _make_preexec_fn(self):
        """Create a preexec_fn that sets resource limits (#482).

        Returns None if resource limits are not available on this platform.
        """
        import sys

        if sys.platform == "win32":
            return None

        try:
            import resource

            memory_bytes = self._memory_limit_mb * 1024 * 1024
            cpu_sec = self._cpu_limit_sec

            def _set_limits():
                try:
                    resource.setrlimit(
                        resource.RLIMIT_AS,
                        (memory_bytes, memory_bytes),
                    )
                except (ValueError, OSError):
                    pass
                try:
                    resource.setrlimit(
                        resource.RLIMIT_CPU,
                        (cpu_sec, cpu_sec),
                    )
                except (ValueError, OSError):
                    pass

            return _set_limits
        except (ImportError, AttributeError):
            return None

    async def run_command(
        self,
        command: str,
        cwd: str,
        timeout: int = 120,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        """Execute command as a subprocess with resource limits (#482)."""
        import asyncio
        import subprocess
        import time

        resolved_env = env if env is not None else self._build_safe_env()
        preexec_fn = self._make_preexec_fn()

        start = time.monotonic()
        try:
            loop = asyncio.get_event_loop()
            proc = await loop.run_in_executor(
                None,
                lambda: subprocess.Popen(
                    command,
                    shell=True,
                    cwd=cwd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=resolved_env,
                    preexec_fn=preexec_fn,
                ),
            )
            stdout, stderr = await asyncio.wait_for(
                loop.run_in_executor(None, proc.communicate),
                timeout=timeout,
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
            proc.kill()
            try:
                proc.communicate()
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
    """Execute commands in a Docker container (#179 PR4, #483).

    Provides process, network, and filesystem isolation by running commands
    inside a Docker container with the workspace mounted as a volume.

    Security defaults:
    - network_mode=none (no network access)
    - Only workspace directory is mounted
    - No host environment variables leaked by default
    - Resource limits enforced via --memory and --cpus (#483)
    """

    sandbox_type = ExecutionSandbox.DOCKER

    def __init__(
        self,
        image: str = "python:3.12-slim",
        network_mode: str = "none",
        memory_limit: str = "512m",
        cpu_limit: float = 1.0,
    ) -> None:
        self._image = image
        self._network_mode = network_mode
        self._memory_limit = memory_limit
        self._cpu_limit = cpu_limit

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
        """Build the docker run argument list (no shell, no injection risk).

        Includes resource limits from SandboxConfig (#483):
        - --memory: OOM kill when exceeded
        - --cpus: CPU time cap
        """
        args = [
            "docker", "run", "--rm",
            "-v", f"{cwd}:/workspace",
            "--network", self._network_mode,
            "--memory", self._memory_limit,
            "--cpus", str(self._cpu_limit),
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
