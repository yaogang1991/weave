# ADR 0009: Three-Tier Memory Scope with Promotion

**Status:** Accepted
**Date:** 2026-05-10
**Deciders:** Project Lead

## Context

Agents need persistent memory across tasks and sessions. Requirements:

1. **Privacy** — Some memories are agent-specific (generator's coding style notes)
2. **Session sharing** — Upstream agent results should be available to downstream agents in the same session
3. **Cross-session persistence** — Learned project conventions should persist across all future sessions
4. **Automatic promotion** — Useful private memories should be promotable to broader scopes without manual intervention

## Decision

We use a **three-tier scope model** with explicit promotion:

```
PRIVATE (per-agent) → SESSION (session-wide) → GLOBAL (cross-session)
```

- **PRIVATE**: Stored under `agents/{agent_type}/`, only accessible by that agent type
- **SESSION**: Stored under `sessions/{session_id}/`, accessible to all agents in the session
- **GLOBAL**: Stored under `global/`, accessible to all agents in all sessions

**Promotion mechanism** (`memory/sharing.py`):
- `promote_to_session()` — Creates a SESSION copy of a PRIVATE entry (deduplication by content+agent+source_node_id)
- `promote_to_global()` — Creates a GLOBAL copy of a SESSION/PRIVATE entry (same deduplication)
- `share_with_downstream()` — Automatically promotes relevant PRIVATE memories when DAG edges connect agents

**Relevance ordering**: GLOBAL memories are always included in retrieval, then filtered by `relevance_score` (recency × frequency × keyword overlap).

## Consequences

**Positive:**
- Clear data lifecycle — each scope has well-defined visibility and cleanup rules
- DAG-aware sharing — upstream insights automatically flow to downstream agents
- Deduplication prevents memory bloat on repeated promotions
- GLOBAL scope provides persistent learning without per-session noise

**Negative:**
- Promotion creates copies (storage overhead), not references
- Deduplication check requires scanning existing entries (O(N) per promotion)
- No automatic promotion triggers — promotion is call-site driven

## Alternatives Considered

- **Single global pool with tags**: All memories in one pool, filtered by metadata tags. Rejected — loses the natural lifecycle boundaries; harder to implement per-scope cleanup (e.g., purge all session memories on session end).
- **Reference-based sharing**: Store references instead of copies. Rejected — adds complexity for garbage collection and scope lifecycle management; copies are simpler and the storage overhead is acceptable for single-user scenarios.
