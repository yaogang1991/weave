"""Integration layer data models -- NormalizedIssue, RawIssue, LabelConfig."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class RawIssue(BaseModel):
    """Raw issue data from an external tracker."""
    source: str
    data: dict[str, Any]


class NormalizedIssue(BaseModel):
    """Unified issue model produced by an IssueTracker."""
    number: int
    title: str
    body: str = ""
    labels: list[str] = Field(default_factory=list)
    url: str = ""
    repo: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None
    author: str = ""

    def to_requirement(self, max_body: int = 4000) -> str:
        parts = [f"GitHub Issue #{self.number}: {self.title}"]
        if self.body:
            parts.append("")
            parts.append(self.body[:max_body])
        parts.append(f"\nRepo: {self.repo}")
        return "\n".join(parts)


class LabelConfig(BaseModel):
    """Label lifecycle configuration for issue tracking."""
    trigger_label: str = "weave"
    running_label: str = "weave-running"
    pr_label: str = "weave-pr"
    failed_label: str = "weave-failed"
