"""Local backend -- executes directly in the main repo (current behavior)."""

from __future__ import annotations

import shutil
from pathlib import Path

from backend.base import ExecutionBackend, BackendType


class LocalBackend(ExecutionBackend):
    """
    Local backend: execute directly in the main repo directory.

    This is the M1 default behavior, no isolation environment is created.
    Used for low-risk, fast execution scenarios.
    """

    backend_type = BackendType.LOCAL

    def setup(self, job_id: str, run_id: str) -> Path:
        """Return main repo root directory as working directory."""
        if self.repo_root:
            return Path(self.repo_root)
        work_dir = self.base_path / job_id / run_id
        work_dir.mkdir(parents=True, exist_ok=True)
        return work_dir

    def get_work_dir(self, job_id: str, run_id: str) -> Path:
        if self.repo_root:
            return Path(self.repo_root)
        return self.base_path / job_id / run_id

    def cleanup(self, job_id: str, run_id: str) -> None:
        """Clean up working directory."""
        if self.repo_root and self.repo_root != Path("."):
            return None
        work_dir = self.base_path / job_id / run_id
        if work_dir.exists():
            shutil.rmtree(work_dir)

    def preserve(self, job_id: str, run_id: str, reason: str = "") -> Path:
        """Preserve scene (move to preserve directory)."""
        if self.repo_root:
            return Path(self.repo_root)
        work_dir = self.base_path / job_id / run_id
        preserve_dir = self.base_path / "_preserved" / job_id / run_id
        # Create parent but not the leaf directory to avoid nesting
        preserve_dir.parent.mkdir(parents=True, exist_ok=True)
        if work_dir.exists():
            shutil.move(str(work_dir), str(preserve_dir))
        return preserve_dir

    def is_available(self) -> bool:
        """Local backend is always available."""
        return True
