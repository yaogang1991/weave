"""
Backend lifecycle manager.

Manages creation, execution, cleanup, and preservation of execution backends.
Supports configuration-driven backend selection.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from backend.base import ExecutionBackend, BackendType
from backend.local import LocalBackend
from backend.worktree import WorktreeBackend
from backend.docker_stub import DockerBackend


class BackendManager:
    """
    Backend lifecycle manager.

    Responsibilities:
    1. Select backend based on configuration
    2. Manage backend lifecycle (setup -> execute -> cleanup/preserve)
    3. Support risk level -> backend mapping

    Usage:
        manager = BackendManager(default_backend="worktree")
        work_dir = manager.setup(job_id, run_id)
        # ... execute in work_dir ...
        manager.cleanup(job_id, run_id)  # on success
        # or
        manager.preserve(job_id, run_id, reason="failed")  # on failure
    """

    def __init__(
        self,
        default_backend: str = "local",
        repo_root: str | None = None,
        base_path: str = "./data/backends",
        risk_backend_map: dict[str, str] | None = None,
        cleanup_policy: str = "on_success",
    ):
        self.default_backend_type = BackendType(default_backend)
        self.repo_root = repo_root
        self.base_path = base_path
        self.risk_backend_map = risk_backend_map or {
            "low": "local",
            "medium": "local",
            "high": "worktree",
            "critical": "worktree",
        }
        self.cleanup_policy = cleanup_policy
        _VALID_POLICIES = ("always", "on_success", "never")
        if self.cleanup_policy not in _VALID_POLICIES:
            raise ValueError(
                f"Invalid cleanup_policy '{self.cleanup_policy}', "
                f"must be one of {_VALID_POLICIES}"
            )
        self._backends: dict[str, ExecutionBackend] = {}
        self._active_runs: dict[str, ExecutionBackend] = {}  # run_id -> backend

    def _get_backend(self, backend_type: BackendType) -> ExecutionBackend:
        """Get or create backend instance."""
        key = backend_type.value
        if key not in self._backends:
            if backend_type == BackendType.LOCAL:
                self._backends[key] = LocalBackend(
                    self.repo_root, self.base_path
                )
            elif backend_type == BackendType.WORKTREE:
                self._backends[key] = WorktreeBackend(
                    self.repo_root, self.base_path
                )
            elif backend_type == BackendType.DOCKER:
                self._backends[key] = DockerBackend(
                    self.repo_root, self.base_path
                )
            else:
                raise ValueError(f"Unknown backend type: {backend_type}")
        return self._backends[key]

    def setup(
        self,
        job_id: str,
        run_id: str,
        backend_type: str | None = None,
        risk_level: str | None = None,
    ) -> Path:
        """
        Prepare execution environment.

        Args:
            job_id: Task ID
            run_id: Run ID
            backend_type: Explicitly specify backend (overrides default)
            risk_level: Risk level (used for auto-selecting backend)
        """
        # Determine backend type
        btype = self._resolve_backend_type(backend_type, risk_level)
        backend = self._get_backend(btype)

        # Check availability
        if not backend.is_available():
            if btype == BackendType.WORKTREE:
                # Worktree not available, fall back to local
                backend = self._get_backend(BackendType.LOCAL)
            else:
                raise RuntimeError(
                    f"Backend {btype.value} is not available"
                )

        work_dir = backend.setup(job_id, run_id)
        self._active_runs[run_id] = backend
        return work_dir

    def get_work_dir(self, job_id: str, run_id: str) -> Path | None:
        """Get working directory."""
        backend = self._active_runs.get(run_id)
        if backend:
            return backend.get_work_dir(job_id, run_id)
        return None

    def cleanup(self, job_id: str, run_id: str) -> None:
        """Clean up execution environment (on success)."""
        if self.cleanup_policy == "never":
            self.preserve(job_id, run_id, reason="cleanup_policy=never")
            return
        backend = self._active_runs.pop(run_id, None)
        if backend:
            backend.cleanup(job_id, run_id)

    def preserve(
        self, job_id: str, run_id: str, reason: str = ""
    ) -> Path | None:
        """Preserve execution scene (on failure)."""
        if self.cleanup_policy == "always":
            backend = self._active_runs.pop(run_id, None)
            if backend:
                backend.cleanup(job_id, run_id)
            return None
        backend = self._active_runs.pop(run_id, None)
        if backend:
            return backend.preserve(job_id, run_id, reason)
        return None

    def finalize(
        self, job_id: str, run_id: str, success: bool, reason: str = ""
    ) -> Path | None:
        """
        Finalize a run based on outcome and cleanup_policy.

        Args:
            job_id: Task ID
            run_id: Run ID
            success: Whether the run succeeded
            reason: Reason if failed

        Returns:
            Preserved path if preserved, None if cleaned up.
        """
        if success:
            if self.cleanup_policy == "never":
                return self.preserve(job_id, run_id, reason="success_but_never_policy")
            self.cleanup(job_id, run_id)
            return None
        else:
            if self.cleanup_policy == "always":
                self.cleanup(job_id, run_id)
                return None
            return self.preserve(job_id, run_id, reason=reason)

    def _resolve_backend_type(
        self,
        explicit: str | None,
        risk_level: str | None,
    ) -> BackendType:
        """Resolve final backend type."""
        if explicit:
            return BackendType(explicit)
        if risk_level and risk_level.lower() in self.risk_backend_map:
            mapped = self.risk_backend_map[risk_level.lower()]
            return BackendType(mapped)
        return self.default_backend_type

    def get_active_runs(self) -> dict[str, str]:
        """Get currently active runs (run_id -> backend_type)."""
        return {
            rid: b.backend_type.value
            for rid, b in self._active_runs.items()
        }
