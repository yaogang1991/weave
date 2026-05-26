"""Post-execution handler -- three-state outcome + commit + push + PR creation."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from pydantic import BaseModel

from core.subprocess_runner import run_with_progress
from integrations.base import CodeHost
from integrations.github.pr_body import generate_pr_body
from integrations.models import NormalizedIssue

logger = logging.getLogger(__name__)


class PostExecutionResult(BaseModel):
    status: Literal["no_output", "partial", "success", "push_failed"]
    pr_url: str = ""
    issue_comment: str = ""


async def _has_changes(work_dir: str) -> bool:
    """Check if there are uncommitted or untracked changes in work_dir."""
    result = await asyncio.to_thread(
        run_with_progress,
        ["git", "status", "--porcelain"],
        timeout=15,
        cwd=work_dir,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


async def _commit_changes(work_dir: str, issue: NormalizedIssue) -> bool:
    """Stage all changes and commit."""
    add_result = await asyncio.to_thread(
        run_with_progress,
        ["git", "add", "-A"],
        timeout=30,
        cwd=work_dir,
    )
    if add_result.returncode != 0:
        logger.error("git add failed: %s", add_result.stderr)
        return False
    commit_result = await asyncio.to_thread(
        run_with_progress,
        ["git", "commit", "-m", f"Fix #{issue.number}: {issue.title}"],
        timeout=30,
        cwd=work_dir,
    )
    if commit_result.returncode != 0:
        logger.error("git commit failed: %s", commit_result.stderr)
        return False
    return True


async def handle_result(
    run: Any,
    job_metadata: dict[str, Any],
    host: CodeHost,
    llm_config: Any = None,
) -> PostExecutionResult:
    """Determine post-execution outcome and create PR if appropriate."""
    from control_plane.models import RunStatus

    work_dir = (run.dag_result or {}).get("work_dir")
    if not work_dir:
        return PostExecutionResult(
            status="push_failed",
            issue_comment="No work directory found in run result.",
        )

    issue_number = job_metadata.get("issue_number", 0)
    issue_title = job_metadata.get("requirement", "")
    branch = job_metadata.get("branch_name", "")
    repo = job_metadata.get("repo", "")

    issue = NormalizedIssue(
        number=issue_number,
        title=issue_title,
        url=job_metadata.get("issue_url", ""),
        repo=repo,
    )

    if not await _has_changes(work_dir):
        return PostExecutionResult(
            status="no_output",
            issue_comment="Execution completed but no code changes were produced.",
        )

    if not await _commit_changes(work_dir, issue):
        return PostExecutionResult(
            status="push_failed",
            issue_comment="Failed to commit changes.",
        )

    is_success = run.status == RunStatus.SUCCEEDED
    body = await generate_pr_body(work_dir, issue, llm_config)

    pushed = await host.push_changes(repo, branch, cwd=work_dir)
    if not pushed:
        return PostExecutionResult(
            status="push_failed",
            issue_comment=f"Code push failed for branch `{branch}`.",
        )

    pr_title = f"Fix #{issue_number}: {issue_title[:60]}"
    pr_url = await host.create_pr(repo, branch, pr_title, body, draft=not is_success)

    if is_success:
        return PostExecutionResult(status="success", pr_url=pr_url)

    error = (run.dag_result or {}).get("error", "Unknown error")
    return PostExecutionResult(
        status="partial",
        pr_url=pr_url,
        issue_comment=(
            f"Weave executed this issue but encountered errors.\n\n"
            f"**Error**: {str(error)[:500]}\n"
            f"**PR**: {pr_url}\n\n"
            f"A draft PR has been created with partial changes."
        ),
    )
