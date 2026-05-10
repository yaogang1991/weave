# ADR 0006: Git Worktree Isolation (M2)

**Status:** Accepted
**Date:** 2026-05-10
**Deciders:** Project Lead

## Context

Agent tasks modify files. In the default setup, all jobs run against the main working directory. Problems:

- Concurrent jobs can conflict (edit same files)
- Failed jobs leave partial changes in the main repo
- No clean boundary between job artifacts and project source

Options:

1. **Docker containers**: Full OS-level isolation
2. **Git worktrees**: Each job gets its own worktree (lightweight checkout)
3. **Copy-on-write**: Copy the repo, work on the copy
4. **No isolation**: Jobs run directly in the main repo

## Decision

We chose **git worktrees** (`backend/worktree.py`).

- Each job/run creates a worktree: `git worktree add {path} -b job/{job_id}`
- Agent tools operate within the worktree directory
- Success → cleanup worktree
- Failure → preserve worktree for debugging

## Consequences

**Positive:**
- True file isolation — concurrent jobs don't interfere
- Git-native — no extra dependencies
- Lightweight — worktrees share the `.git` object store (minimal disk overhead)
- Failed worktrees are inspectable (preserved for debugging)
- Easy cleanup — `git worktree remove`

**Negative:**
- Requires a git repository (won't work for non-git projects)
- Worktrees count as git refs (potential ref proliferation)
- No process/network isolation — only filesystem isolation

## Alternatives Considered

- **Docker**: Full isolation (process, network, filesystem). Higher infrastructure cost. Docker backend stubbed for future (`backend/docker_stub.py`).
- **Copy-on-write**: Simpler but wasteful disk usage. No git integration.
- **No isolation**: M1 default. Acceptable for single-user but risky with concurrent jobs.
