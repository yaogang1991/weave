# PR Review: #927 — feat: M5.3 post-execution pipeline (commit + push + PR)

**Reviewed**: 2026-05-27
**Author**: yaogang1991
**Branch**: worktree-m5-3-execute-to-pr → main
**Decision**: REQUEST CHANGES

## Summary

Well-structured three-state post-execution handler with clear separation between `post_execution.py` (outcome logic), `pr_body.py` (body generation), and `CodeHost` interface extensions. 18 new tests all passing. Two HIGH issues need fixing before merge: dead code in label-based commit prefix (labels never passed through metadata), and a missing `labels` field in CLI metadata that makes the `Feat`/`Fix` distinction unreachable.

## Findings

### CRITICAL

None.

### HIGH

1. **`_commit_prefix` is dead code — `labels` never passed from CLI metadata**
   - `post_execution.py:222-226` — `_commit_prefix()` checks for `"enhancement"` in `issue.labels` to return `"Feat"` vs `"Fix"`
   - `post_execution.py:331` — `labels=job_metadata.get("labels", [])` always gets `[]`
   - `cli/github.py:64-72` — metadata dict has no `"labels"` key; `NormalizedIssue` has a `.labels` field that should be included
   - **Fix**: Add `"labels": issue.labels` to the metadata dict in `cli/github.py:64-72`

2. **PR body template hardcodes "Fix" regardless of issue type**
   - `pr_body.py:543` — `f"## Summary\nFix #{issue.number}: ..."` always says "Fix"
   - Should be consistent with `_commit_prefix` result (which itself is dead code per #1)
   - **Fix**: Pass the commit prefix or issue type into `generate_pr_body` and use it in the summary line

### MEDIUM

3. **`_detect_changes` fallback to `origin/main..HEAD` may fail in fresh worktree**
   - `post_execution.py:239-244` — If worktree doesn't have `origin/main` remote tracking branch, `git log origin/main..HEAD` returns non-zero, returning False even when commits exist
   - **Fix**: Use `git log HEAD~N..HEAD` or `git diff --stat HEAD~1` as a secondary fallback, or ensure worktree setup always fetches origin/main

4. **`generate_llm_review` embeds up to 15K chars of diff in prompt**
   - `pr_body.py:441` — `get_full_diff(work_dir, max_chars=15000)` then embedded directly in prompt string
   - Large prompts increase cost and latency; the 500-token response cap doesn't limit input
   - **Fix**: Consider reducing to 5K-8K chars for review, or summarizing large diffs

5. **New abstract methods `find_existing_pr`/`update_pr` in `CodeHost` have no implementation tests**
   - `integrations/base.py:46-56` — Two new abstract methods added
   - `github_host.py:53-76` — Implementation exists but no unit tests for `GitHubCodeHost.find_existing_pr` or `update_pr`
   - Only tested via mock in `test_post_execution.py::test_idempotent_pr_update_existing`
   - **Fix**: Add tests for the `GitHubCodeHost` implementations (JSON parsing, error handling)

### LOW

6. **`_commit_changes` does redundant `git add -u` then `git add .`**
   - `post_execution.py:250-267` — `git add -u` stages tracked files, then `git add .` stages everything. First call is redundant since `git add .` covers it.
   - Minor: two subprocess calls where one would suffice

7. **`_build_execution_summary` duration defaults to 0 when timestamps unavailable**
   - `post_execution.py:296-297` — Good defensive check, but `duration_sec: 0` shows as "Duration: 0s" which is misleading
   - Minor: consider omitting the duration line when unavailable

## Validation Results

| Check | Result |
|---|---|
| Tests (PR-specific) | 18/18 passed |
| Tests (full suite) | 129 passed, 1 pre-existing failure |
| Lint | Skipped |
| Type check | Skipped |

## Files Reviewed

| File | Action |
|---|---|
| `cli/github.py` | Modified |
| `control_plane/service.py` | Modified |
| `integrations/base.py` | Modified |
| `integrations/github/github_host.py` | Modified |
| `integrations/github/post_execution.py` | Created |
| `integrations/github/pr_body.py` | Created |
| `tests/test_post_execution.py` | Created |
| `tests/test_pr_body.py` | Created |
