"""Post-execution handler -- three-state outcome + commit + push + PR creation."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal

from pydantic import BaseModel

from core.config import LLMConfig
from core.subprocess_runner import run_with_progress
from control_plane.models import Run, RunStatus
from integrations.base import CodeHost
from integrations.github.pr_body import generate_pr_body
from integrations.models import NormalizedIssue

logger = logging.getLogger(__name__)


class PostExecutionResult(BaseModel):
    status: Literal["no_output", "partial", "success", "push_failed"]
    pr_url: str = ""
    issue_comment: str = ""


def _commit_prefix(labels: list[str]) -> str:
    """Derive commit prefix from issue labels."""
    if "enhancement" in labels:
        return "Feat"
    return "Fix"


async def _detect_changes(work_dir: str) -> bool:
    """Check for uncommitted changes OR commits ahead of origin/main."""
    uncommitted = await asyncio.to_thread(
        run_with_progress,
        ["git", "diff", "--stat", "HEAD"],
        timeout=15,
        cwd=work_dir,
    )
    if uncommitted.returncode == 0 and uncommitted.stdout.strip():
        return True
    ahead = await asyncio.to_thread(
        run_with_progress,
        ["git", "log", "--oneline", "origin/main..HEAD"],
        timeout=15,
        cwd=work_dir,
    )
    return ahead.returncode == 0 and bool(ahead.stdout.strip())


async def _commit_changes(work_dir: str, issue: NormalizedIssue) -> bool:
    """Stage changes and commit. Returns True if commit succeeded or nothing to commit."""
    add_result = await asyncio.to_thread(
        run_with_progress,
        ["git", "add", "-A"],
        timeout=30,
        cwd=work_dir,
    )
    if add_result.returncode != 0:
        logger.error("git add -A failed: %s", add_result.stderr)
        return False
    prefix = _commit_prefix(issue.labels)
    commit_result = await asyncio.to_thread(
        run_with_progress,
        ["git", "commit", "-m", f"{prefix} #{issue.number}: {issue.title}"],
        timeout=30,
        cwd=work_dir,
    )
    if commit_result.returncode != 0:
        stderr_lower = (commit_result.stderr or "").lower()
        if "nothing to commit" in stderr_lower:
            logger.info("Nothing to commit -- agent may have already committed")
            return True
        logger.error("git commit failed: %s", commit_result.stderr)
        return False
    return True


def _build_execution_summary(run: Run) -> dict[str, Any]:
    """Extract execution metrics from run for PR body."""
    dag_result = run.dag_result or {}
    summary: dict[str, Any] = {
        "nodes_total": dag_result.get("total_nodes", 0),
        "nodes_completed": dag_result.get("success_nodes", 0),
        "tokens_in": dag_result.get("tokens_in", 0),
        "tokens_out": dag_result.get("tokens_out", 0),
        "duration_sec": 0,
    }
    if run.started_at and run.completed_at:
        delta = run.completed_at - run.started_at
        summary["duration_sec"] = round(delta.total_seconds(), 1)
    test_summary = dag_result.get("test_summary")
    if test_summary:
        summary["test_summary"] = test_summary
    lint_summary = dag_result.get("lint_summary")
    if lint_summary:
        summary["lint_summary"] = lint_summary
    return summary


async def handle_result(
    run: Run,
    job_metadata: dict[str, Any],
    host: CodeHost,
    llm_config: LLMConfig | None = None,
) -> PostExecutionResult:
    """Determine post-execution outcome and create PR if appropriate."""
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
        labels=job_metadata.get("labels", []),
    )

    if not await _detect_changes(work_dir):
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
    execution_summary = _build_execution_summary(run)
    body = await generate_pr_body(
        work_dir, issue, llm_config, execution_summary=execution_summary,
    )

    pushed = await host.push_changes(repo, branch, cwd=work_dir)
    if not pushed:
        return PostExecutionResult(
            status="push_failed",
            issue_comment=f"Code push failed for branch `{branch}`.",
        )

    existing_pr = await host.find_existing_pr(repo, branch)
    if existing_pr:
        updated = await host.update_pr(repo, existing_pr, body)
        if not updated:
            logger.warning("Failed to update existing PR %s", existing_pr)
        if is_success:
            return PostExecutionResult(status="success", pr_url=existing_pr)
        error = (run.dag_result or {}).get("error", "Unknown error")
        return PostExecutionResult(
            status="partial",
            pr_url=existing_pr,
            issue_comment=(
                f"Weave re-executed this issue.\n\n"
                f"**Error**: {str(error)[:500]}\n"
                f"**PR**: {existing_pr}\n\n"
                f"The draft PR has been updated with new changes."
            ),
        )

    pr_title = f"{_commit_prefix(issue.labels)} #{issue_number}: {issue_title[:60]}"
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
