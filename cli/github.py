"""CLI commands for GitHub issue integration (M5.2)."""
from __future__ import annotations

import logging
import sys

from integrations.config import IntegrationConfig
from integrations.github.branch_manager import BranchManager
from integrations.github.github_host import GitHubCodeHost
from integrations.github.github_tracker import GitHubIssueTracker
from integrations.ranker import IssueRanker

logger = logging.getLogger(__name__)


def _make_tracker() -> GitHubIssueTracker:
    return GitHubIssueTracker()


def _make_host() -> GitHubCodeHost:
    return GitHubCodeHost()


def _make_ranker():
    from core.config import WeaveConfig
    cfg = WeaveConfig.from_env()
    return IssueRanker(llm_config=cfg.llm)


def _make_services():
    from control_plane.repository import JobRepository
    from control_plane.service import RunService
    repo = JobRepository()
    service = RunService(repository=repo)
    return repo, service


async def _execute_issue(issue, repo: str, dry_run: bool = False):
    """Execute a single issue: labels -> branch -> submit -> run -> result labels."""
    config = IntegrationConfig.from_env()
    host = _make_host()
    branch_mgr = BranchManager()

    if dry_run:
        print(f"[DRY RUN] Would execute: #{issue.number} {issue.title}")
        return

    print(f"Executing issue #{issue.number}: {issue.title}")

    await host.update_labels(
        repo, issue.number,
        add=[config.label.running_label],
        remove=[config.label.trigger_label],
    )

    branch = await branch_mgr.create_branch(repo, issue)

    _, service = _make_services()
    requirement = issue.to_requirement()
    try:
        job = await service.submit_job(
            requirement=requirement,
            metadata={
                "issue_number": issue.number,
                "issue_url": issue.url,
                "integration_type": "github",
                "branch_name": branch,
            },
        )
        print(f"  Job {job.id} submitted, running...")
        run = await service.run_job(job.id)
    except Exception:
        # Revert label so the issue can be re-picked up later
        await host.update_labels(
            repo, issue.number,
            add=[config.label.trigger_label],
            remove=[config.label.running_label],
        )
        raise

    if run.status.value in ("succeeded",):
        print(f"  Issue #{issue.number} completed successfully")
    else:
        await host.update_labels(
            repo, issue.number,
            add=[config.label.failed_label],
            remove=[config.label.running_label],
        )
        error_msg = run.error or "Unknown error"
        await host.comment_on_issue(
            repo, issue.number,
            f"Weave attempted to resolve this issue but encountered an error.\n\n"
            f"**Error**: {error_msg[:500]}\n**Branch**: {branch}\n\n"
            f"The worktree has been preserved for debugging.",
        )
        print(f"  Issue #{issue.number} failed: {error_msg}")


async def cmd_issue_poll(args):
    """Poll GitHub issues with weave label, rank, and execute top issue."""
    repo = getattr(args, "repo", None) or IntegrationConfig.from_env().github_repo
    if not repo:
        print("Error: --repo required or set WEAVE_GITHUB_REPO")
        sys.exit(1)

    dry_run = getattr(args, "dry_run", False) or IntegrationConfig.from_env().dry_run
    limit = getattr(args, "limit", 1)

    tracker = _make_tracker()
    if not await tracker.health_check():
        print("Error: gh not authenticated. Run: gh auth login")
        sys.exit(1)

    config = IntegrationConfig.from_env()
    raw_issues = await tracker.fetch(repo, labels=[config.label.trigger_label])
    if not raw_issues:
        print("No issues found.")
        return

    issues = [tracker.normalize(r) for r in raw_issues]
    ranker = _make_ranker()
    ranked = await ranker.rank(issues)

    print(f"Found {len(ranked)} issue(s):")
    for i, issue in enumerate(ranked):
        print(f"  {i + 1}. #{issue.number} {issue.title}")

    if dry_run:
        return

    to_execute = ranked[:limit]
    for issue in to_execute:
        await _execute_issue(issue, repo, dry_run=False)


async def cmd_issue_run(args):
    """Execute a specific GitHub issue by number."""
    repo = getattr(args, "repo", None) or IntegrationConfig.from_env().github_repo
    if not repo:
        print("Error: --repo required or set WEAVE_GITHUB_REPO")
        sys.exit(1)

    issue_number = args.number
    tracker = _make_tracker()
    if not await tracker.health_check():
        print("Error: gh not authenticated. Run: gh auth login")
        sys.exit(1)

    from core.subprocess_runner import run_with_progress
    import json
    result = run_with_progress(
        ["gh", "issue", "view", str(issue_number), "--repo", repo,
         "--json", "number,title,body,labels,url,createdAt,author"],
        timeout=15,
    )
    if result.returncode != 0:
        print(f"Error: Issue #{issue_number} not found")
        sys.exit(1)

    from integrations.models import RawIssue
    raw = RawIssue(source="github", data={**json.loads(result.stdout), "repo": repo})
    target = tracker.normalize(raw)

    await _execute_issue(target, repo)


async def cmd_issue_status(args):
    """Show status of Weave-managed issues."""
    repo = getattr(args, "repo", None) or IntegrationConfig.from_env().github_repo
    if not repo:
        print("Error: --repo required or set WEAVE_GITHUB_REPO")
        sys.exit(1)

    tracker = _make_tracker()
    config = IntegrationConfig.from_env()
    raw_issues = await tracker.fetch(repo)
    if not raw_issues:
        print("No Weave-managed issues found.")
        return

    issues = [tracker.normalize(r) for r in raw_issues]
    print(f"Weave-managed issues in {repo}:")
    print(f"{'#':<6} {'Status':<16} {'Title'}")
    print("-" * 60)
    for issue in issues:
        status = "queued"
        if config.label.running_label in issue.labels:
            status = "running"
        elif config.label.pr_label in issue.labels:
            status = "PR created"
        elif config.label.failed_label in issue.labels:
            status = "failed"
        print(f"{issue.number:<6} {status:<16} {issue.title}")
