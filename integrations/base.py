"""IssueTracker and CodeHost abstract interfaces."""
from __future__ import annotations

import abc

from integrations.models import NormalizedIssue, RawIssue


class IssueTracker(abc.ABC):
    """Abstract interface for reading issues from external trackers."""

    @abc.abstractmethod
    async def fetch(self, repo: str, labels: list[str] | None = None) -> list[RawIssue]:
        ...

    @abc.abstractmethod
    def normalize(self, raw: RawIssue) -> NormalizedIssue:
        ...

    @abc.abstractmethod
    async def health_check(self) -> bool:
        ...

    @property
    def name(self) -> str:
        return self.__class__.__name__


class CodeHost(abc.ABC):
    """Abstract interface for writing changes to external code hosts."""

    @abc.abstractmethod
    async def create_branch(self, repo: str, name: str) -> str:
        ...

    @abc.abstractmethod
    async def push_changes(self, repo: str, branch: str, *,
                          cwd: str | None = None) -> bool:
        ...

    @abc.abstractmethod
    async def create_pr(self, repo: str, branch: str, title: str, body: str,
                        draft: bool = False) -> str:
        ...

    @abc.abstractmethod
    async def find_existing_pr(self, repo: str, branch: str) -> str:
        """Return PR URL if one exists for branch, else empty string."""
        ...

    @abc.abstractmethod
    async def update_pr(self, repo: str, pr_url: str, body: str) -> bool:
        """Update body of existing PR by URL. Return True on success."""
        ...

    @abc.abstractmethod
    async def comment_on_issue(self, repo: str, issue_number: int, body: str) -> None:
        ...

    @abc.abstractmethod
    async def update_labels(self, repo: str, issue_number: int,
                            add: list[str] | None = None,
                            remove: list[str] | None = None) -> None:
        ...

    @property
    def name(self) -> str:
        return self.__class__.__name__
