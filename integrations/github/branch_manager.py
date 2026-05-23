"""Branch management for GitHub integration."""
from __future__ import annotations

import asyncio
import logging
import re

from integrations.models import NormalizedIssue
from core.subprocess_runner import run_with_progress

logger = logging.getLogger(__name__)


def generate_slug(title: str, max_words: int = 5, max_length: int = 50) -> str:
    """Generate a branch slug from an issue title.

    Falls back to empty string if title yields nothing (e.g. non-ASCII).
    Caller should use 'issue-{number}' as final fallback.
    """
    words = re.sub(r"[^a-z0-9\s-]", "", title.lower()).split()[:max_words]
    slug = "-".join(words)
    return slug[:max_length].rstrip("-")


class BranchManager:
    """Manages branch creation for issue-based execution."""

    def __init__(self, repo_root: str = ".") -> None:
        self._repo_root = repo_root

    def _branch_name(self, issue: NormalizedIssue) -> str:
        slug = generate_slug(issue.title)
        if not slug:
            slug = f"issue-{issue.number}"
        return f"fix/{issue.number}-{slug}"

    async def create_branch(self, repo: str, issue: NormalizedIssue) -> str:
        """Create a branch for the given issue. Returns branch name."""
        branch = self._branch_name(issue)

        check = await asyncio.to_thread(
            run_with_progress,
            ["git", "rev-parse", "--verify", branch],
            cwd=self._repo_root, timeout=10,
        )
        if check.returncode == 0:
            logger.info("Branch %s already exists, reusing", branch)
            await asyncio.to_thread(
                run_with_progress,
                ["git", "checkout", branch],
                cwd=self._repo_root, timeout=30,
            )
            return branch

        await asyncio.to_thread(
            run_with_progress,
            ["git", "checkout", "-b", branch],
            cwd=self._repo_root, timeout=30,
        )
        logger.info("Created branch %s for issue #%d", branch, issue.number)
        return branch

    async def push_branch(self, branch: str, force: bool = False) -> bool:
        """Push branch to origin. Returns True on success."""
        args = ["git", "push", "origin", branch]
        if force:
            args.insert(2, "--force")
        result = await asyncio.to_thread(run_with_progress, args, cwd=self._repo_root, timeout=60)
        if result.returncode != 0:
            logger.error("git push failed: %s", result.stderr)
            return False
        return True
