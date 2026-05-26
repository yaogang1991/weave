"""GitHub CodeHost -- pushes changes, creates PRs via gh CLI."""
from __future__ import annotations

import asyncio
import logging

from integrations.base import CodeHost
from core.subprocess_runner import run_with_progress

logger = logging.getLogger(__name__)


class GitHubCodeHost(CodeHost):
    """Writes changes to GitHub using the `gh` CLI."""

    async def create_branch(self, repo: str, name: str) -> str:
        result = await asyncio.to_thread(
            run_with_progress,
            ["git", "checkout", "-b", name], timeout=30,
        )
        if result.returncode != 0:
            logger.error("git checkout -b failed: %s", result.stderr)
            return ""
        return name

    async def push_changes(self, repo: str, branch: str, *,
                          cwd: str | None = None) -> bool:
        result = await asyncio.to_thread(
            run_with_progress,
            ["git", "push", "origin", branch, "--force-if-includes"],
            timeout=60, cwd=cwd,
        )
        if result.returncode != 0:
            logger.error("git push failed: %s", result.stderr)
            return False
        return True

    async def create_pr(self, repo: str, branch: str, title: str, body: str,
                        draft: bool = False) -> str:
        cmd = [
            "gh", "pr", "create",
            "--repo", repo,
            "--head", branch,
            "--title", title,
            "--body", body,
        ]
        if draft:
            cmd.append("--draft")
        result = await asyncio.to_thread(run_with_progress, cmd, timeout=30)
        if result.returncode != 0:
            logger.error("gh pr create failed: %s", result.stderr)
            return ""
        return result.stdout.strip()

    async def comment_on_issue(self, repo: str, issue_number: int, body: str) -> None:
        result = await asyncio.to_thread(
            run_with_progress,
            ["gh", "issue", "comment", str(issue_number),
             "--repo", repo, "--body", body],
            timeout=30,
        )
        if result.returncode != 0:
            logger.error("gh issue comment failed: %s", result.stderr)

    async def update_labels(self, repo: str, issue_number: int,
                            add: list[str] | None = None,
                            remove: list[str] | None = None) -> None:
        cmd = ["gh", "issue", "edit", str(issue_number), "--repo", repo]
        if add:
            for label in add:
                cmd.extend(["--add-label", label])
        if remove:
            for label in remove:
                cmd.extend(["--remove-label", label])
        result = await asyncio.to_thread(run_with_progress, cmd, timeout=15)
        if result.returncode != 0:
            logger.error("gh issue edit failed: %s", result.stderr)
