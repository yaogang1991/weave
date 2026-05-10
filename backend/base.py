"""
Execution Backend abstract interface.

All execution backends (local/worktree/docker) must implement this interface.
BackendManager manages different backends' lifecycles through this interface.
"""

from __future__ import annotations

import abc
from enum import Enum
from pathlib import Path
from typing import Any


class BackendType(str, Enum):
    LOCAL = "local"  # Execute directly in main repo (current behavior)
    WORKTREE = "worktree"  # Execute in isolated git worktree
    DOCKER = "docker"  # Execute in Docker container (reserved)


class ExecutionBackend(abc.ABC):
    """
    Abstract base class for execution backends.

    Lifecycle:
    1. setup() -- Prepare execution environment
    2. get_work_dir() / execute() -- Return execution directory (agent works here)
    3. cleanup() -- Clean up on success (or preserve() to keep the scene)

    All methods are synchronous (filesystem operations, no I/O waiting).
    """

    backend_type: BackendType

    def __init__(
        self,
        repo_root: str | None = None,
        base_path: str = "./data/backends",
    ):
        self.repo_root = Path(repo_root) if repo_root else None
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)

    @abc.abstractmethod
    def setup(self, job_id: str, run_id: str) -> Path:
        """
        Prepare execution environment and return working directory.

        Args:
            job_id: Task ID
            run_id: Run ID

        Returns:
            Path: Working directory for the agent
        """
        ...

    @abc.abstractmethod
    def get_work_dir(self, job_id: str, run_id: str) -> Path:
        """Get working directory (without creating it)."""
        ...

    @abc.abstractmethod
    def cleanup(self, job_id: str, run_id: str) -> None:
        """
        Clean up execution environment (called on success).
        Delete temporary files and release resources.
        """
        ...

    @abc.abstractmethod
    def preserve(self, job_id: str, run_id: str, reason: str = "") -> Path | None:
        """
        Preserve execution scene (called on failure, for debugging).

        Returns:
            Path: Preserved scene path, or None if nothing to preserve
        """
        ...

    @abc.abstractmethod
    def is_available(self) -> bool:
        """Check if backend is available (e.g., git is installed)."""
        ...
