"""Docker workspace backend stub -- for future implementation (M3+).

Note: Docker as an execution sandbox is now in backend/sandbox.py.
This file is kept as a stub in case a Docker-based workspace backend
is needed separately from the sandbox concept.
"""

from __future__ import annotations

from pathlib import Path

from backend.base import ExecutionBackend, WorkspaceIsolation


class DockerBackend(ExecutionBackend):
    """
    Docker workspace backend -- reserved for future implementation.

    M3 plan: Create workspace directories inside Docker volumes.
    Currently returns not available as an interface placeholder.
    """

    workspace_type = WorkspaceIsolation.LOCAL  # Placeholder

    def setup(self, job_id: str, run_id: str) -> Path:
        raise NotImplementedError(
            "DockerBackend not yet implemented (planned for M3)"
        )

    def get_work_dir(self, job_id: str, run_id: str) -> Path:
        raise NotImplementedError(
            "DockerBackend not yet implemented (planned for M3)"
        )

    def cleanup(self, job_id: str, run_id: str) -> None:
        raise NotImplementedError(
            "DockerBackend not yet implemented (planned for M3)"
        )

    def preserve(self, job_id: str, run_id: str, reason: str = "") -> Path:
        raise NotImplementedError(
            "DockerBackend not yet implemented (planned for M3)"
        )

    def is_available(self) -> bool:
        """Docker backend is not yet available."""
        return False
