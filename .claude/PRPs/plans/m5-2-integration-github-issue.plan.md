# M5.2 Integration Interface + GitHub Issue → Execute

## Summary

Define IssueTracker + CodeHost adapter interfaces in `integrations/`, implement GitHub adapter
via `gh` CLI, add LLM-based IssueRanker, and wire `issue-poll` / `issue-run` / `issue-status`
CLI commands that fetch issues, rank them, and trigger Weave execution.

## Patterns to Mirror

| Pattern | Reference File | What to Follow |
|---------|---------------|----------------|
| ABC + Registry | `agent/backends/registry.py` | `register()` + `get_backend()` + fallback |
| ABC interface | `agent/backends/base.py` | `@abc.abstractmethod` + `health_check()` |
| CLI subparser | `main.py:109-330` | `subparsers.add_parser()` + `.set_defaults(func=)` |
| CLI command | `cli/jobs.py:cmd_submit()` | `_make_repository()` + `_make_run_service()` |
| Subprocess | `core/subprocess_runner.py:run_with_progress()` | `SubprocessResult` + timeout + cwd |
| Pydantic model | `core/dag_models.py:DAGNode` | `BaseModel` + `Field(default_factory=)` |
| Config from env | `core/config.py:WeaveConfig.from_env()` | `os.environ.get()` with defaults |
| Worktree setup | `backend/worktree.py:setup()` | `git worktree add --detach` + path pattern |

## Files to Create/Modify

| File | Action | Purpose |
|------|--------|---------|
| `integrations/__init__.py` | CREATE | Package init, re-export ABCs |
| `integrations/base.py` | CREATE | IssueTracker + CodeHost ABC |
| `integrations/models.py` | CREATE | RawIssue, NormalizedIssue, LabelConfig |
| `integrations/config.py` | CREATE | IntegrationConfig + from_env() |
| `integrations/registry.py` | CREATE | IntegrationRegistry |
| `integrations/ranker.py` | CREATE | IssueRanker (LLM ranking + fallback) |
| `integrations/github/__init__.py` | CREATE | Package init |
| `integrations/github/github_tracker.py` | CREATE | GitHubIssueTracker (gh CLI) |
| `integrations/github/github_host.py` | CREATE | GitHubCodeHost (gh CLI) |
| `integrations/github/branch_manager.py` | CREATE | BranchManager (branch naming + slug) |
| `cli/github.py` | CREATE | cmd_issue_poll, cmd_issue_run, cmd_issue_status |
| `main.py` | MODIFY | Register 3 new subparsers |
| `control_plane/models.py` | MODIFY | Add issue metadata to Job (via metadata dict, no schema change) |

## Step-by-Step Tasks

### Task 1: integrations/models.py — Data models
- `RawIssue(source: str, data: dict[str, Any])`
- `NormalizedIssue(number, title, body, labels, url, repo, metadata, created_at, author)`
- `LabelConfig(trigger_label="weave", running_label="weave-running", pr_label="weave-pr", failed_label="weave-failed")`
- `NormalizedIssue.to_requirement()` — generates requirement string (body truncated 4000 chars)

### Task 2: integrations/base.py — ABC interfaces
- `IssueTracker(ABC)`: `fetch(repo, labels) -> list[RawIssue]`, `normalize(raw) -> NormalizedIssue`, `health_check() -> bool`
- `CodeHost(ABC)`: `create_branch(repo, name) -> str`, `push_changes(repo, branch) -> bool`, `create_pr(repo, branch, title, body, draft) -> str`, `comment_on_issue(repo, issue_number, body) -> None`, `update_labels(repo, issue_number, add, remove) -> None`

### Task 3: integrations/config.py — Configuration
- `IntegrationConfig` with `label: LabelConfig`, `github_repo: str`, `dry_run: bool`
- `IntegrationConfig.from_env()` — reads `WEAVE_GITHUB_REPO`, `WEAVE_INTEGRATION_LABEL`, `WEAVE_INTEGRATION_DRY_RUN`

### Task 4: integrations/registry.py — IntegrationRegistry
- `register_tracker(name, tracker)`, `register_host(name, host)`
- `get_tracker(name)`, `get_host(name)`
- `list_trackers()`, `list_hosts()`
- Mirrors `BackendRegistry` pattern

### Task 5: integrations/github/branch_manager.py — Branch management
- `generate_slug(title, max_words=5, max_length=50) -> str` — regex filter + fallback to `issue-{number}`
- `BranchManager.__init__(repo_root: str)`
- `create_branch(repo, issue: NormalizedIssue) -> str` — check existing, create `fix/{number}-{slug}`
- Uses `run_with_progress()` for git commands

### Task 6: integrations/github/github_tracker.py — GitHub IssueTracker
- `GitHubIssueTracker.__init__()`
- `fetch(repo, labels)` — `gh issue list --repo {repo} --label {labels} --state open --json number,title,body,labels,url,createdAt,author`
- `normalize(raw)` — dict → NormalizedIssue mapping
- `health_check()` — `gh auth status` returncode check
- All commands via `run_with_progress()`

### Task 7: integrations/github/github_host.py — GitHub CodeHost
- `GitHubCodeHost.__init__()`
- `create_branch(repo, name)` — `git checkout -b {name}`
- `push_changes(repo, branch)` — `git push origin {branch} --force-if-includes`
- `create_pr(repo, branch, title, body, draft)` — `gh pr create --repo {repo} --head {branch} --title {title} --body {body} [--draft]`
- `comment_on_issue(repo, issue_number, body)` — `gh issue comment {number} --repo {repo} --body {body}`
- `update_labels(repo, issue_number, add, remove)` — `gh issue edit {number} --repo {repo} --add-label {add} --remove-label {remove}`
- All commands via `run_with_progress()`

### Task 8: integrations/ranker.py — LLM Issue Ranking
- `IssueRanker.__init__(llm_config: LLMConfig)`
- `rank(issues: list[NormalizedIssue]) -> list[NormalizedIssue]`
- Skip LLM if len <= 1
- Use structured output (tool_use) for sorted ID list + reasoning
- Fallback: chronological by created_at
- Model: Sonnet (not Opus)

### Task 9: cli/github.py — CLI Commands
- `cmd_issue_poll(args)` — fetch → normalize → rank → pick top → update labels → create branch → submit_job → run_job → update labels on result
- `cmd_issue_run(args)` — fetch single issue by number → update labels → create branch → submit_job → run_job → update labels on result
- `cmd_issue_status(args)` — list jobs with issue metadata → format status table
- Follow `cli/jobs.py` pattern: `_make_repository()` + `_make_run_service()`
- `--dry-run` flag on issue-poll: print ranked list, don't execute
- `--limit` flag on issue-poll: max issues to process (default 1)

### Task 10: main.py — Register CLI subparsers
- `issue-poll` subparser: `--repo`, `--dry-run`, `--limit`
- `issue-run` subparser: `number` positional, `--repo`
- `issue-status` subparser: `--repo`
- Import from `cli/github.py`

### Task 11: integrations/__init__.py — Re-exports
- Re-export `IssueTracker`, `CodeHost`, `NormalizedIssue`, `IntegrationRegistry`

## Validation Commands

```bash
# Type check
python -c "from integrations import IssueTracker, CodeHost, IntegrationRegistry; print('OK')"

# Unit tests
python -m pytest tests/test_integration_models.py tests/test_github_tracker.py tests/test_github_host.py tests/test_issue_ranker.py tests/test_branch_manager.py tests/test_cli_github.py -v

# Full suite (should not regress)
python -m pytest --tb=short -q

# Lint
flake8 integrations/ cli/github.py --max-line-length=100
```

## Acceptance Criteria

1. `IssueTracker` and `CodeHost` ABCs defined with all methods
2. `GitHubIssueTracker.fetch()` returns NormalizedIssue list from `gh issue list`
3. `GitHubCodeHost.create_pr()` works via `gh pr create`
4. `IssueRanker.rank()` falls back to chronological when LLM fails
5. `BranchManager.generate_slug()` handles non-ASCII titles with fallback
6. `weave issue-poll --repo owner/repo --dry-run` prints ranked issue list
7. `weave issue-run 123 --repo owner/repo` triggers execution
8. `weave issue-status --repo owner/repo` shows issue-linked job status
9. All new tests pass, no regression in existing tests
10. CONTEXT.md and ADR-0016 committed alongside implementation

## Task Dependencies

```
Task 1 (models) → Task 2 (ABC) → Task 4 (registry)
                              → Task 5 (branch) → Task 9 (CLI)
                              → Task 6 (tracker) → Task 9
                              → Task 7 (host) → Task 9
               → Task 3 (config) → Task 9
               → Task 8 (ranker) → Task 9
Task 9 (CLI) → Task 10 (main.py registration)
All → Task 11 (__init__.py)
```

Parallelizable groups:
- Group A: Tasks 1, 3 (no dependencies)
- Group B: Tasks 2, 5, 6, 7, 8 (depend on Task 1)
- Group C: Tasks 4, 9, 11 (depend on Group B)
- Group D: Task 10 (depends on Task 9)
