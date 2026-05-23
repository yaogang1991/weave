# ADR 0013: Plugin Interface for Issue Source and Change Sink

**Status:** Accepted
**Date:** 2026-05-22
**Deciders:** Project Lead

## Context

M5 introduces automated Issue-to-PR workflow: Weave fetches issues from an external system (GitHub), executes them as DAGs, and pushes changes back as PRs. The question is how to integrate this external system coupling.

Options:
1. **Hardcoded GitHub module** — `github/` directory with direct GitHub API calls scattered across control_plane
2. **Plugin interface** — Define `IssueSource` and `ChangeSink` ABCs in `plugins/base.py`, GitHub as first implementation in `plugins/github/`

## Decision

We chose **plugin interface** with GitHub as the built-in plugin.

### Interface Design

```python
# plugins/base.py

class IssueSource(ABC):
    """Fetch and parse issues from external systems."""

    @abstractmethod
    async def fetch_issues(self, labels: list[str]) -> list[IssueInfo]:
        """Fetch open issues matching labels."""

    @abstractmethod
    async def parse_issue(self, issue: IssueInfo) -> Requirement:
        """Convert external issue to Weave requirement."""

    @abstractmethod
    async def rank_issues(self, issues: list[Requirement]) -> list[Requirement]:
        """Rank issues by priority (LLM-driven)."""

    @abstractmethod
    async def update_label(self, issue_id: str, from_label: str, to_label: str) -> None:
        """Transition issue label."""


class ChangeSink(ABC):
    """Push changes back to external systems."""

    @abstractmethod
    async def create_branch(self, name: str, workspace_path: Path) -> str:
        """Create and checkout a named branch."""

    @abstractmethod
    async def push_changes(self, branch: str, workspace_path: Path) -> None:
        """Git push to remote."""

    @abstractmethod
    async def create_pr(self, branch: str, title: str, body: str, draft: bool) -> str:
        """Create pull request, return PR URL."""

    @abstractmethod
    async def comment_on_issue(self, issue_id: str, body: str) -> None:
        """Post comment on issue."""

    @abstractmethod
    async def create_review_comment(self, pr_url: str, comments: list[ReviewComment]) -> None:
        """Post review comments on PR."""
```

### GitHub Plugin Structure

```
plugins/
├── __init__.py          # Plugin registry
├── base.py              # IssueSource + ChangeSink ABCs
└── github/
    ├── __init__.py      # GitHubPlugin(IssueSource, ChangeSink)
    ├── source.py        # Issue fetching via gh CLI
    ├── sink.py          # PR creation via gh CLI
    ├── webhook.py       # Independent FastAPI webhook server
    └── branch_manager.py # Branch + worktree management
```

### Why this over hardcoded module

1. **Extensibility** — Adding GitLab/Jira/Linear support requires only a new plugin, no core changes
2. **Separation of concerns** — External system logic stays out of control_plane/orchestrator
3. **Testability** — Plugin interface is easy to mock in tests
4. **Self-contained** — GitHub plugin includes its own webhook server (independent FastAPI + uvicorn), not mounted on visualizer

## Consequences

### Positive
- Future integrations (GitLab, Jira) are additive, no refactoring
- Core Weave remains external-system-agnostic
- Each plugin can have its own dependencies and lifecycle

### Negative
- Initial implementation slightly more work than hardcoded module
- Plugin interface may need iteration as new use cases emerge
- Need a plugin discovery/registration mechanism (`plugins/__init__.py`)

### Risks
- Over-abstracting for M5: only GitHub plugin exists, interface is designed by speculation
- Mitigation: keep interface minimal (only methods M5 actually calls), extend later

## References

- PRD: `.claude/PRPs/prds/m5-production-orchestration.prd.md`
- CONTEXT.md: Plugin interface terms
- Related: ADR-0007 (Config-Driven Backend Selection) — similar plugin pattern for ExecutionBackend
