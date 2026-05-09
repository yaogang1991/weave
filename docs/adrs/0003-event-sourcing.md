# ADR 0003: Append-Only JSONL Event Store

**Status:** Accepted
**Date:** 2026-05-08
**Deciders:** Project Lead

## Context

The system needs to persist runtime state (job progress, agent results, session data) for recovery and audit. Options:

1. **Database (SQLite/PostgreSQL)**: Structured queries, indexing, concurrent access
2. **Append-only JSONL files**: Immutable event log, state derived by replay

## Decision

We chose **append-only JSONL** (`SessionStore` in `session/store.py`).

- Every state change is an immutable event appended to `{session_id}.jsonl`
- Current state is derived by replaying events from the beginning
- Checkpoints are file copies (no partial updates)
- One file per session/job

## Consequences

**Positive:**
- Complete audit trail — every state change is recorded
- Simple recovery — replay events to reconstruct any past state
- No database dependency — pure file I/O
- Atomic appends — crash-safe (partial lines are discarded on recovery)
- Easy backup — copy the JSONL files

**Negative:**
- No random access or efficient queries — must replay full log
- Growing file sizes — no compaction mechanism yet
- Not suitable for concurrent multi-writer scenarios (single-user M1, acceptable)
- No indexing — searching events requires linear scan

## Alternatives Considered

- **SQLite**: Would enable efficient queries and indexing. Deferred — adds dependency complexity for M1. Considered for M3 if performance requires it.
- **Hybrid (JSONL + SQLite index)**: Event log for audit, SQLite for fast queries. Interesting for M3 (see roadmap).
