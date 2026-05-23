"""GitHub IssueTracker -- fetches issues via gh CLI."""
from __future__ import annotations

import json
import logging
from datetime import datetime

from integrations.base import IssueTracker
from integrations.models import NormalizedIssue, RawIssue
from core.subprocess_runner import run_with_progress

logger = logging.getLogger(__name__)


class GitHubIssueTracker(IssueTracker):
    """Fetches GitHub Issues using the `gh` CLI."""

    async def fetch(self, repo: str, labels: list[str] | None = None) -> list[RawIssue]:
        cmd = [
            "gh", "issue", "list",
            "--repo", repo,
            "--state", "open",
            "--json", "number,title,body,labels,url,createdAt,author",
            "--limit", "50",
        ]
        if labels:
            for label in labels:
                cmd.extend(["--label", label])

        result = run_with_progress(cmd, timeout=30)
        if result.returncode != 0:
            logger.error("gh issue list failed: %s", result.stderr)
            return []

        items = json.loads(result.stdout) if result.stdout.strip() else []
        return [RawIssue(source="github", data=item) for item in items]

    def normalize(self, raw: RawIssue) -> NormalizedIssue:
        d = raw.data
        created = None
        if d.get("createdAt"):
            try:
                created = datetime.fromisoformat(d["createdAt"].replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass

        return NormalizedIssue(
            number=d.get("number", 0),
            title=d.get("title", ""),
            body=d.get("body", ""),
            labels=[lbl.get("name", "") for lbl in d.get("labels", [])],
            url=d.get("url", ""),
            repo=d.get("repo", ""),
            metadata=d,
            created_at=created,
            author=(
                d.get("author", {}).get("login", "")
                if isinstance(d.get("author"), dict) else ""
            ),
        )

    async def health_check(self) -> bool:
        result = run_with_progress(["gh", "auth", "status"], timeout=10)
        return result.returncode == 0
