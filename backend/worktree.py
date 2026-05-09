"""
Worktree backend -- executes in an isolated git worktree.

Each job/run gets its own worktree, preventing workspace pollution.
On failure, the worktree can be preserved for debugging.
On success, the worktree is automatically cleaned up.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from backend.base import ExecutionBackend, BackendType


class WorktreeBackend(ExecutionBackend):
    """
    Worktree backend: execute in an isolated git worktree.

    Lifecycle:
    1. git worktree add <path> <branch/commit>
    2. Execute agent task in worktree
    3. Success: git worktree remove <path>
    4. Failure: preserve worktree for debugging

    Requirement: git >= 2.15 (supports worktree)
    """

    backend_type = BackendType.WORKTREE

    def __init__(
        self,
        repo_root: str | None = None,
        base_path: str = "./data/worktrees",
    ):
        super().__init__(repo_root, base_path)
        self.worktrees: dict[str, Path] = {}  # run_id -> worktree_path

    def setup(self, job_id: str, run_id: str) -> Path:
        """
        Create git worktree and return path.

        Command: git worktree add --detach <path>
        Uses --detach to avoid creating new branches.
        """
        worktree_path = self.base_path / job_id / run_id
        worktree_path.parent.mkdir(parents=True, exist_ok=True)

        # If already exists, remove first
        if worktree_path.exists():
            self._remove_worktree(str(worktree_path))

        # Create worktree
        result = subprocess.run(
            ["git", "worktree", "add", "--detach", str(worktree_path)],
            cwd=str(self.repo_root) if self.repo_root else None,
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            raise RuntimeError(f"git worktree add failed: {result.stderr}")

        self.worktrees[run_id] = worktree_path
        return worktree_path

    def get_work_dir(self, job_id: str, run_id: str) -> Path:
        if run_id in self.worktrees:
            return self.worktrees[run_id]
        return self.base_path / job_id / run_id

    def cleanup(self, job_id: str, run_id: str) -> None:
        """Remove git worktree."""
        worktree_path = self.worktrees.pop(
            run_id, self.base_path / job_id / run_id
        )
        self._remove_worktree(str(worktree_path))

        # Clean up empty directories
        job_dir = self.base_path / job_id
        if job_dir.exists() and not any(job_dir.iterdir()):
            job_dir.rmdir()

    def preserve(self, job_id: str, run_id: str, reason: str = "") -> Path:
        """Preserve failed worktree (do not remove git worktree, just mark)."""
        worktree_path = self.worktrees.get(
            run_id, self.base_path / job_id / run_id
        )

        # Write preserve marker
        marker = worktree_path / ".PRESERVED"
        marker.write_text(
            f"Preserved at: {datetime.now(timezone.utc).isoformat()}\n"
            f"Reason: {reason}\n"
        )

        # Remove from active worktrees but do not execute git worktree remove
        self.worktrees.pop(run_id, None)

        return worktree_path

    def is_available(self) -> bool:
        """Check if git worktree is available."""
        try:
            result = subprocess.run(
                ["git", "worktree", "list"],
                cwd=str(self.repo_root) if self.repo_root else None,
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def _remove_worktree(self, path: str) -> None:
        """Safely remove git worktree."""
        try:
            result = subprocess.run(
                ["git", "worktree", "remove", "--force", path],
                cwd=str(self.repo_root) if self.repo_root else None,
                capture_output=True,
                timeout=10,
            )
            if result.returncode != 0 and Path(path).exists():
                # git worktree remove failed (e.g. stale dir not a registered worktree)
                shutil.rmtree(path, ignore_errors=True)
        except (subprocess.TimeoutExpired, Exception):
            # Fallback: manual cleanup
            if Path(path).exists():
                shutil.rmtree(path, ignore_errors=True)

    def list_active_worktrees(self) -> list[dict]:
        """List currently active worktrees."""
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(self.repo_root),
            capture_output=True,
            text=True,
        )
        worktrees: list[dict] = []
        current: dict = {}
        for line in result.stdout.splitlines():
            if line.startswith("worktree "):
                if current:
                    worktrees.append(current)
                current = {"path": line[9:].strip()}
            elif line.startswith("HEAD "):
                current["head"] = line[5:].strip()
            elif line.startswith("branch "):
                current["branch"] = line[7:].strip()
            elif line.startswith("detached"):
                current["detached"] = True
        if current:
            worktrees.append(current)
        return worktrees
