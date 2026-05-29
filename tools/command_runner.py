"""
ToolCommandRunner: synchronous command execution interface for tools.

Part of #179 PR 1: align sandbox execution interfaces.

Defines a clean sync protocol that ToolRegistry uses to execute bash
commands. The SyncSandboxAdapter wraps the async SandboxProvider into
this sync interface, enabling Docker sandbox integration without
converting the entire tool chain to async.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from typing import Protocol, runtime_checkable

from pydantic import BaseModel

from backend.sandbox import SandboxProvider

logger = logging.getLogger(__name__)


class ToolCommandResult(BaseModel):
    """Result of a tool command execution."""

    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


@runtime_checkable
class ToolCommandRunner(Protocol):
    """Synchronous command execution interface for tool use.

    This is the interface that ToolRegistry._tool_bash calls when a
    sandbox_runner is configured. It abstracts over local subprocess,
    Docker containers, or any other execution environment.
    """

    def run_command(
        self,
        command: str,
        cwd: str,
        timeout: int = 120,
        env: dict[str, str] | None = None,
    ) -> ToolCommandResult: ...


class SyncSandboxAdapter:
    """Wraps an async SandboxProvider into a sync ToolCommandRunner.

    Uses a dedicated thread to run the async sandbox.run_command()
    coroutine, avoiding the need for nest_asyncio or converting the
    entire tool chain to async.
    """

    def __init__(self, sandbox: SandboxProvider) -> None:
        self._sandbox = sandbox
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)

    def close(self) -> None:
        """Shut down thread pool executor to release thread."""
        self._executor.shutdown(wait=False)

    def run_command(
        self,
        command: str,
        cwd: str,
        timeout: int = 120,
        env: dict[str, str] | None = None,
    ) -> ToolCommandResult:
        future = self._executor.submit(
            self._run_async, command, cwd, timeout, env,
        )
        try:
            return future.result(timeout=timeout + 5)
        except concurrent.futures.TimeoutError:
            return ToolCommandResult(
                returncode=-1,
                stderr=f"Command timed out after {timeout}s",
                timed_out=True,
            )

    def _run_async(
        self,
        command: str,
        cwd: str,
        timeout: int,
        env: dict[str, str] | None,
    ) -> ToolCommandResult:
        try:
            coro = self._sandbox.run_command(command, cwd, timeout, env)
        except (NotImplementedError, Exception) as e:
            prefix = "" if isinstance(e, NotImplementedError) else "Sandbox error: "
            return ToolCommandResult(
                returncode=-1,
                stderr=f"{prefix}{e}",
                timed_out=False,
            )

        # Handle non-coroutine returns (e.g., mocks in tests)
        if not asyncio.iscoroutine(coro):
            result = coro
        else:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            try:
                if loop is not None and loop.is_running():
                    new_loop = asyncio.new_event_loop()
                    try:
                        result = new_loop.run_until_complete(coro)
                    finally:
                        new_loop.close()
                else:
                    result = asyncio.run(coro)
            except (NotImplementedError, Exception) as e:
                prefix = "" if isinstance(e, NotImplementedError) else "Sandbox error: "
                return ToolCommandResult(
                    returncode=-1,
                    stderr=f"{prefix}{e}",
                    timed_out=False,
                )

        return ToolCommandResult(
            returncode=result.exit_code if not result.success else 0,
            stdout=result.stdout,
            stderr=result.stderr,
            timed_out=result.exit_code == -1,
        )
