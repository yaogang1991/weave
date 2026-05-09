# ADR 0005: Heartbeat-Based Watchdog (M2)

**Status:** Accepted
**Date:** 2026-05-10
**Deciders:** Project Lead

## Context

Long-running agent tasks can hang (infinite loops, API timeouts, deadlocks). The system needs to detect and handle hung agents. Options:

1. **Process monitoring**: Watch the worker process (CPU, memory, output)
2. **Heartbeat protocol**: Running agents emit periodic heartbeat events; a watchdog monitors for missed heartbeats
3. **Wall-clock timeout**: Simple timeout per node

## Decision

We chose **heartbeat-based watchdog** (implemented in `core/dag_engine.py`).

- Running DAG nodes periodically emit `node.heartbeat` events
- A background watchdog coroutine (`_watchdog_loop`) monitors `DAGNode.last_heartbeat_at`
- Missing `miss_threshold` consecutive heartbeats → node marked UNHEALTHY → DEAD
- `NodeHealth` enum: HEALTHY → MISSED → UNHEALTHY → DEAD
- Threshold: `heartbeat_interval(5s) × miss_threshold(3) ≈ 15s` before action

## Consequences

**Positive:**
- Detects logical hangs (infinite loops in agent code) not just process death
- Graduated response — missed → unhealthy → dead, with events at each stage
- Events are auditable — heartbeat history in the JSONL log
- Works with async execution model (no separate process monitor needed)

**Negative:**
- Heartbeat emission depends on agent cooperation (agent must be alive to emit)
- Adds event log volume (mitigated by 5s interval)
- Watchdog is another background coroutine to manage

## Alternatives Considered

- **Process monitoring**: Could use OS-level signals or `psutil`. Doesn't detect logical hangs where the process is alive but stuck.
- **Wall-clock timeout only**: Already implemented (per-node timeout in M1). Watchdog adds a complementary health layer.
