# ADR 0007: Config-Driven Backend Selection (M2)

**Status:** Accepted
**Date:** 2026-05-10
**Deciders:** Project Lead

## Context

The system supports multiple execution backends (local, worktree, future: Docker). How should the system choose which backend to use for a given job?

1. **Hardcoded**: One backend compiled into the code
2. **Per-command**: User specifies backend on each invocation
3. **Config-driven**: Default backend in config, with automatic selection based on risk level

## Decision

We chose **config-driven backend selection** (`backend/lifecycle.py` — `BackendManager`).

- `HARNESS_DEFAULT_BACKEND=local|worktree` sets the default
- Risk-based mapping: HIGH risk → worktree (auto-isolate)
- Per-job override: jobs can specify a preferred backend
- Automatic fallback: worktree unavailable → local
- Backend interface: abstract `ExecutionBackend` class with `setup/get_work_dir/cleanup/preserve/is_available`

## Consequences

**Positive:**
- Zero-config default works (local backend)
- Safety-conscious: high-risk tasks automatically isolated
- Extensible: new backends just implement the interface
- Graceful degradation: automatic fallback

**Negative:**
- Backend selection logic adds complexity to `BackendManager`
- Risk-to-backend mapping may not cover all scenarios

## Alternatives Considered

- **Per-command only**: Too much burden on the user. Would require the user to remember which tasks need isolation.
- **All-worktree**: Wasteful for read-only tasks. Adds overhead for every job.
