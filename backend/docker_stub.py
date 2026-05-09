"""Docker backend stub -- for future implementation (M3+)."""

from __future__ import annotations

from pathlib import Path

from backend.base import ExecutionBackend, BackendType


class DockerBackend(ExecutionBackend):
    """
    Docker backend -- reserved for future implementation.

    M3 plan: Execute agent tasks in Docker containers for full isolation.
    Currently returns not available as an interface placeholder.
    """

    backend_type = BackendType.DOCKER

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
