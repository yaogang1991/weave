# Harness M1 Personal Edition — Engineering Specification

---
**版本:** M1.1
**状态:** 已发布
**日期:** 2026-05-09
---

> **Version:** 1.1.0
> **Status:** Published
> **Target:** Single-user, self-hosted, CLI-driven multi-agent software development harness
> **Motto:** *"I can throw tasks in, the system runs to completion or recovers from failure; I only confirm high-risk actions when necessary."*

## 状态标签说明

| 标签 | 含义 |
|------|------|
| [IMPLEMENTED] | 代码已实现，有测试覆盖 |
| [PARTIAL] | 核心实现完成，边界情况待完善 |
| [PLANNED] | 仅在规划阶段，代码未落地 |*

---

## Table of Contents

1. [Part 1 — In / Out of Scope](#part-1--in--out-of-scope)
2. [Part 2 — Job / Run State Machine](#part-2--job--run-state-machine)
3. [Part 3 — CLI Interface Contract & Error Codes](#part-3--cli-interface-contract--error-codes)
4. [Part 4 — Definition of Done (DoD)](#part-4--definition-of-done-dod)
5. [Appendix A — Data Models Reference](#appendix-a--data-models-reference)
6. [Appendix B — Architecture Overview](#appendix-b--architecture-overview)

---

## Part 1 — In / Out of Scope

### 1.1 In Scope

| # | Feature | Description | Priority |
|---|---------|-------------|----------|
| 1 | **Single-user operation** [IMPLEMENTED] | One human user submits tasks via CLI; no authentication or authorization layers beyond OS permissions. The entire system runs on the user's local machine. | P0 |
| 2 | **CLI control plane** [IMPLEMENTED] | Commands: `submit`, `status`, `list`, `cancel`, `worker`, `tickets`, `approve`, `reject`. All interactions are terminal-based. Output is structured JSON Lines for scriptability. | P0 |
| 3 | **Worker queue execution** [IMPLEMENTED] | A local worker process polls a job queue (filesystem-backed), leases jobs, executes DAG nodes via Agent Pool, and reports results. Supports concurrency control. | P0 |
| 4 | **Reliability primitives** [IMPLEMENTED] | Per-node timeout (default 120s), retry with exponential backoff (max 3 attempts per node), dead-letter queue for permanently failed jobs, graceful degradation on LLM API transient errors. | P0 |
| 5 | **Replan closed loop** [IMPLEMENTED] | When a DAG node fails after exhausting retries, the Orchestrator Agent can decide to `replan` — generating a new DAG to route around the failure. The new plan becomes a new Job that references the original. | P0 |
| 6a | **Tool risk classification** [IMPLEMENTED] | Four-layer defense: (1) tool risk classification (LOW/MEDIUM/HIGH/CRITICAL), (2) permission mode (`plan`/`default`/`accept_edits`/`auto`/`dont_ask`), (3) denied command/tool lists, (4) iteration/error limits. | P0 |
| 6b | **Interactive confirmation (stdin)** [IMPLEMENTED] | `PersonalGuardrails.request_confirmation()` reads stdin for HIGH/CRITICAL risk actions. Works in interactive terminal sessions. | P0 |
| 6c | **Approval ticket system** [IMPLEMENTED] — M1.1 | Persistent pending/approved/rejected/expired ticket flow replaces stdin blocking for unattended operation. `ApprovalTicket` + `ApprovalRepository` with atomic writes. | P0 |
| 6d | **Non-interactive auto-pending** [IMPLEMENTED] — M1.1 | `HARNESS_NON_INTERACTIVE=true` or `--non-interactive`: HIGH risk actions automatically create pending tickets without blocking stdin. | P0 |
| 6e | **Whitelist auto-approval** [IMPLEMENTED] | Command prefix and regex whitelist patterns; whitelisted commands bypass confirmation even for HIGH risk. | P0 |
| 6f | **Unified 3-state guardrail entry** [IMPLEMENTED] — M1.1 | Single `check_and_execute()` returns `allowed|blocked|pending_approval(ticket_id)`. No dual-path divergence. | P0 |
| 6g | **Multi-tenancy guardrails** [PLANNED] | Per-user/team policy isolation. Not needed for single-user M1. | M2 |
| 7 | **Basic metrics & alerting** [IMPLEMENTED] | Event-sourced metrics: task throughput, success/failure rates, average node duration, LLM token usage, tool call histograms. Alerting via CLI (`--alert` flag on `status`/`list`) and exit codes. | P1 |
| 8 | **Restart recovery** [IMPLEMENTED] | On process restart, the worker scans existing event logs (JSONL) to reconstruct Job/Run states and resumes interrupted jobs from the last successfully completed DAG level. No in-memory state is authoritative. | P0 |
| 9 | **Append-only event store** [IMPLEMENTED] | All state changes are persisted as immutable events in JSONL files. Session state is derived by replaying events. Checkpoints are file copies. | P0 |
| 10 | **Local file-based persistence** [IMPLEMENTED] | All durable state lives on the local filesystem under `./data/`: `./data/events/`, `./data/artifacts/`, `./data/plans/`, `./data/queue/`. No external database. | P0 |
| 11 | **Approval ticket system** [IMPLEMENTED] — M1.1 | Pending/approved/rejected/expired four-state approval flow for high-risk tool calls. Persistent JSON-backed storage with atomic writes. | P0 |
| 12 | **Non-interactive mode** [IMPLEMENTED] — M1.1 | Worker runs without stdin blocking; high-risk actions create pending approval tickets instead of interactive prompts. | P0 |
| 13 | **Approval CLI toolchain** [IMPLEMENTED] — M1.1 | `tickets`, `approve`, `reject` commands for managing approval tickets via CLI. | P0 |
| 14 | **Approval recovery enhancement** [IMPLEMENTED] — M1.1 | Worker restart scans pending tickets and re-processes them; expired tickets auto-transition to `expired`. | P0 |
| 15 | **Approval dimension metrics** [IMPLEMENTED] — M1.1 | Ticket counts by status (pending/approved/rejected/expired) exposed via `tickets` command `--stats`. | P1 |

### 1.2 Out of Scope

| # | Feature | Rationale | Future Roadmap |
|---|---------|-----------|----------------|
| 1 | **Multi-tenancy** | M1 targets a single developer on their own machine. No user isolation, no RBAC, no per-user quotas. | M3 — Team Edition |
| 2 | **Web UI / Dashboard** | All interaction is CLI-only. The visualizer subsystem (`viz` command) exists but is read-only monitoring, not a control plane. | M2 — Web Console |
| 3 | **Distributed execution** | Workers run on a single machine. No multi-node scheduling, no message queue (Redis/RabbitMQ), no remote agents. | M3 — Distributed Harness |
| 4 | **Persistent database** | PostgreSQL, SQLite, or any RDBMS is not used. The JSONL event log is the sole source of truth. | M2 — Optional SQLite index for large workloads |
| 5 | **Plugin marketplace** | Agents are registered via local YAML (`./harness/agents.yaml`) or programmatic API. No external plugin discovery. | M2 — Plugin registry |
| 6 | **Real-time collaboration** | No concurrent editing, no shared sessions, no multiplayer features. | M3 — Team Edition |
| 7 | **Sandboxed execution** | Docker/bubblewrap sandbox configs exist in code but are not enforced in M1. Tools run directly on host. | M2 — Sandboxed workers |
| 8 | **Billing / cost tracking** | No LLM API cost aggregation, no budget caps, no usage quotas. | M3 — Cost monitoring |

---

## Part 2 — Job / Run State Machine

### 2.1 Overview

Harness M1 uses a two-level state model:

- **Job**: The unit of user-submitted work. A Job contains a DAG (execution plan) and tracks its overall lifecycle in the queue.
- **Run**: A single execution attempt of a Job's DAG. A Job may have multiple Runs (retries, replans).

Both state machines are **strict** — illegal transitions are rejected with `E1002 InvalidStatusTransition`.

### 2.2 JobStatus Enum

```python
class JobStatus(str, Enum):
    QUEUED      = "queued"        # Job submitted, waiting for worker pickup
    LEASED      = "leased"        # Worker has leased the job, not yet executing
    RUNNING     = "running"       # DAG execution in progress
    SUCCEEDED   = "succeeded"     # All DAG nodes completed successfully
    FAILED      = "failed"        # DAG execution failed (exhausted retries/replan)
    CANCELED    = "canceled"      # User canceled before completion
    DEAD_LETTER = "dead_letter"   # Permanently failed, requires manual inspection
```

#### 2.2.1 Job State Definitions

| State | Enter Condition | Exit Condition |
|-------|----------------|----------------|
| `QUEUED` | User calls `submit` with a requirement string. Job record written to `./data/queue/`. | Worker picks up the job (lease acquired) OR user calls `cancel`. |
| `LEASED` | Worker successfully acquires lease on the job file (atomic rename). | Worker begins DAG execution OR lease expires (returns to `QUEUED`). |
| `RUNNING` | Worker begins executing the DAG via `DAGExecutionEngine.execute()`. | All nodes succeed, a node fails terminally, user cancels, or timeout fires. |
| `SUCCEEDED` | All DAG nodes reach `NodeStatus.SUCCESS`. | Terminal state — no exit. |
| `FAILED` | At least one node reaches `NodeStatus.FAILED` and Orchestrator decides `abort` or max replans exhausted. | Terminal state — no exit. Can be manually retried (creates new Run). |
| `CANCELED` | User calls `cancel` while Job is `QUEUED`, `LEASED`, or `RUNNING`. | Terminal state — no exit. Partial artifacts may exist. |
| `DEAD_LETTER` | Job has been retried >= `max_attempts` (default: 3) and never succeeded. Job file moved to `./data/queue/dead/`. | Terminal state — requires human inspection to resolve or delete. |

#### 2.2.2 Job State Transitions (Legal)

```
                        cancel
    +----------------------------------------------------+
    |                                                    v
QUEUED --lease--> LEASED --start--> RUNNING --success--> SUCCEEDED
    ^      |              |    |          |
    |      | lease_expiry |    | cancel   | fail(abort)
    |      +--------------+    |          |
    |                          v          v
    +-------------------> CANCELED      FAILED
    ^                                        |
    |         (after max_attempts)           |
    +----------------------------------------+
                                             |
                                             v
                                        DEAD_LETTER
```

| From | To | Trigger | Guard | Status |
|------|----|---------|-------|--------|
| `QUEUED` | `LEASED` | Worker acquires lease | Lease file must not exist | [IMPLEMENTED] |
| `LEASED` | `RUNNING` | Worker calls `engine.execute(dag)` | — | [IMPLEMENTED] |
| `LEASED` | `QUEUED` | Lease timeout (default 60s) | No progress event written | [IMPLEMENTED] |
| `RUNNING` | `SUCCEEDED` | All DAG nodes `SUCCESS` | `failed == 0 && skipped == 0` | [IMPLEMENTED] |
| `RUNNING` | `FAILED` | Orchestrator returns `action=abort` OR node fails after max_retries | — | [IMPLEMENTED] |
| `RUNNING` | `CANCELED` | User calls `cancel <job_id>` | Signal sent to worker | [IMPLEMENTED] |
| `QUEUED` | `CANCELED` | User calls `cancel <job_id>` | — | [IMPLEMENTED] |
| `LEASED` | `CANCELED` | User calls `cancel <job_id>` | — | [IMPLEMENTED] |
| `FAILED` | `DEAD_LETTER` | Failure count >= `max_attempts` | — | [IMPLEMENTED] |
| `FAILED` | `QUEUED` | Retry after failure | attempt < max_attempts | [IMPLEMENTED] |

#### 2.2.3 Job Illegal Transitions

| Attempted Transition | Behavior | Status |
|---------------------|----------|--------|
| `SUCCEEDED` → any | Rejected with `E1002` — "Terminal state cannot transition" | [IMPLEMENTED] |
| `FAILED` → any except `DEAD_LETTER` | Rejected with `E1002` — "Terminal state cannot transition" | [IMPLEMENTED] |
| `CANCELED` → any | Rejected with `E1002` — "Terminal state cannot transition" | [IMPLEMENTED] |
| `DEAD_LETTER` → any | Rejected with `E1002` — "Dead letter jobs require manual resolution" | [IMPLEMENTED] |
| `RUNNING` → `QUEUED` | Rejected with `E1002` — "Cannot return to queued from running" | [IMPLEMENTED] |
| `LEASED` → `SUCCEEDED` | Rejected with `E1002` — "Must pass through running" | [IMPLEMENTED] |
| `QUEUED` → `RUNNING` | Rejected with `E1002` — "Must pass through leased" | [IMPLEMENTED] |

---

### 2.3 RunStatus Enum

```python
class RunStatus(str, Enum):
    RUNNING   = "running"     # DAG execution actively in progress
    SUCCEEDED = "succeeded"   # All nodes completed successfully
    FAILED    = "failed"      # One or more nodes failed terminally
    ABORTED   = "aborted"     # Orchestrator decided action=abort
    TIMED_OUT = "timed_out"   # Overall job timeout exceeded
```

#### 2.3.1 Run State Definitions

| State | Enter Condition | Exit Condition |
|-------|----------------|----------------|
| `RUNNING` | Worker starts `DAGExecutionEngine.execute()` for this Run. | Completion, failure, abort, or timeout. |
| `SUCCEEDED` | All DAG nodes `SUCCESS`, evaluator passes. | Terminal. |
| `FAILED` | At least one node `FAILED` after max retries, and Orchestrator did not choose `abort` (i.e., failure was not critical enough to stop everything). Distinct from `ABORTED`. | Terminal. |
| `ABORTED` | Orchestrator explicitly returned `action=abort` on a failure. | Terminal. |
| `TIMED_OUT` | Job-level timeout (from `--timeout SEC`) exceeded. Worker SIGTERMs execution. | Terminal. |

#### 2.3.2 Run State Transitions (Legal)

```
                    +-- all nodes success --> SUCCEEDED
                    |
RUNNING --+---------+-- node fail(abort) ----> ABORTED
          |         |
          |         +-- node fail(no abort) --> FAILED
          |
          +-- timeout -----------------------> TIMED_OUT
```

| From | To | Trigger | Guard | Status |
|------|----|---------|-------|--------|
| `RUNNING` | `SUCCEEDED` | `engine.execute()` returns with `failed==0` | — | [IMPLEMENTED] |
| `RUNNING` | `ABORTED` | Orchestrator returns `action=abort` | — | [IMPLEMENTED] |
| `RUNNING` | `FAILED` | Node exhausts retries, orchestrator does NOT abort | — | [IMPLEMENTED] |
| `RUNNING` | `TIMED_OUT` | Wall-clock time since Run start > `--timeout` | Timeout fired | [IMPLEMENTED] |

#### 2.3.3 Run Illegal Transitions

| Attempted Transition | Behavior | Status |
|---------------------|----------|--------|
| Any terminal → any | Rejected with `E1002` | [IMPLEMENTED] |
| `SUCCEEDED` → `FAILED` / `ABORTED` / `TIMED_OUT` | Impossible by construction (engine returns definitively) | [IMPLEMENTED] |
| `TIMED_OUT` → any | Terminal — cannot revive a timed-out run. A new Run must be created. | [IMPLEMENTED] |

---

### 2.4 Full State Transition Diagram (ASCII)

```
================================================================================
                              HARNESS M1 STATE MACHINES
================================================================================

USER SUBMISSION LAYER                          EXECUTION LAYER
(Jobs)                                         (Runs)

  submit()                                          start_run()
     |                                                   |
     v                                                   v
+---------+  lease   +---------+  start   +---------+  +---------+
| QUEUED  |--------->| LEASED  |--------->| RUNNING |->| RUNNING |
+---------+          +---------+          +----+----+  +----+----+
     ^                  |   |                 |             |
     |                  |   | lease_expiry    |             |
     |                  |   +---------------->|             |
     |                  |                     |             |
     |                  |   cancel            |   success   |
     |                  +-------------------> | +---------->| +--------->
     |                  |                     |             |  SUCCEEDED
     |                  |   cancel            |   fail      |
     |                  +-------------------> | +---------->| +--------->
     |                  |                     |  (abort)    |   ABORTED
     |                  |   cancel            |             |
     +------------------+-------------------> |             | fail(no abort)
                                              |             +----------->
                                              |                FAILED
                                              |   timeout
                                              +------------------->
                                                                    TIMED_OUT

JOB TERMINAL STATES                          RUN TERMINAL STATES
+-----------+                                +-----------+
| SUCCEEDED |<--------------------------------| SUCCEEDED |
+-----------+                                +-----------+
+-----------+                                +-----------+
|  FAILED   |--+                             |   FAILED  |
+-----------+  |                             +-----------+
               |  max_attempts reached        +-----------+
               v                              |  ABORTED  |
+-----------+                                +-----------+
|DEAD_LETTER|                                +-----------+
+-----------+                                | TIMED_OUT |
+-----------+                                +-----------+
| CANCELED  |
+-----------+

================================================================================
```

---

### 2.5 State Persistence & Recovery

| Aspect | Rule |
|--------|------|
| **Authoritative state** | The event log (`./data/events/{job_id}.jsonl`) is the sole source of truth. In-memory state is a cache. |
| **State transitions** | Every transition emits a `workflow.stage_start/end` or `session.status_*` event. |
| **Lease mechanism** | Implemented as atomic file rename (`{job_id}.queued` → `{job_id}.leased`). Lease expiry is checked by worker on each poll. |
| **Recovery on restart** | Worker scans `./data/queue/` on startup. Jobs in `LEASED` with stale lease timestamps are returned to `QUEUED`. Jobs in `RUNNING` with no recent heartbeat events are marked `FAILED` and retried if attempts remain. |
| **Dead letter** | Jobs are moved to `./data/queue/dead/{job_id}.json` with metadata: original requirement, failure history, last error, number of attempts. |

---

### 2.6 Orchestrator Failure Decision → State Mapping

The Orchestrator's `FailureDecision.action` directly drives Run-level state transitions:

| FailureDecision.action | Effect on DAG | Effect on Run State |
|-----------------------|---------------|---------------------|
| `retry` | Re-execute failed node immediately | Remains `RUNNING` |
| `skip` | Mark node `SKIPPED`, continue with dependents | Remains `RUNNING` |
| `abort` | Skip all remaining nodes, stop execution | Transitions to `ABORTED` |
| `replan` | Generate new DAG, create new Job with `parent_job_id` reference | Current Run ends `FAILED`; new Job queued |

---

## Part 3 — CLI Interface Contract & Error Codes

### 3.1 Command Reference

#### 3.1.1 `submit` — Submit a new task [IMPLEMENTED]

```
Usage: harness submit "<requirement>" [OPTIONS]

Submit a user requirement to the harness. The orchestrator will generate a DAG
plan and queue it for execution.

Arguments:
  requirement          User requirement string (quoted, required)

Options:
  --project PATH       Project directory path (loads .harness/agents.yaml)
  --timeout SEC        Overall job timeout in seconds (default: 600)
  --max-attempts N     Maximum execution attempts before dead-letter (default: 3)
  --mode MODE          Guardrail permission mode: plan|default|accept_edits|auto|dont_ask
  --priority N         Job priority, higher = sooner (default: 0)
  --tag TAG            User-defined tag for grouping (optional)

Output (JSON):
  {"job_id": "<uuid>", "status": "queued", "message": "Job queued successfully"}

Examples:
  harness submit "Build a REST API for user authentication"
  harness submit "Add OAuth2 support" --project ./my-project --timeout 1200 --max-attempts 5
```

#### 3.1.2 `status` — Query job status [IMPLEMENTED]

```
Usage: harness status <job_id> [OPTIONS]

Query the current status and progress of a job.

Arguments:
  job_id               UUID of the job to query

Options:
  --detail             Include full DAG node statuses and artifact list
  --events             Include recent event log entries

Output (JSON):
  {
    "job_id": "<uuid>",
    "status": "running",
    "run_id": "<uuid>",
    "run_status": "running",
    "created_at": "2024-01-15T09:30:00Z",
    "requirement": "Build a REST API...",
    "attempt": 1,
    "max_attempts": 3,
    "progress": {
      "total_nodes": 5,
      "completed": 2,
      "failed": 0,
      "skipped": 0,
      "pending": 3
    },
    "message": "2/5 nodes completed"
  }

Examples:
  harness status abc123-def456
  harness status abc123-def456 --detail --events
```

#### 3.1.3 `list` — List jobs [IMPLEMENTED]

```
Usage: harness list [OPTIONS]

List jobs with optional filtering.

Options:
  --status STATUS      Filter by status: queued|leased|running|succeeded|failed|canceled|dead_letter|all
  --limit N            Maximum number of jobs to return (default: 20)
  --offset N           Pagination offset (default: 0)
  --tag TAG            Filter by user-defined tag
  --since TIMESTAMP    Filter jobs created after ISO timestamp
  --sort FIELD         Sort by: created_at|updated_at|status (default: created_at)
  --order ORDER        Sort order: asc|desc (default: desc)

Output (JSON Lines):
  {"job_id": "...", "status": "succeeded", "requirement": "...", "created_at": "...", "finished_at": "..."}
  {"job_id": "...", "status": "running", "requirement": "...", "created_at": "...", "progress": "2/5"}
  ...

Examples:
  harness list
  harness list --status running --limit 10
  harness list --status failed --since 2024-01-01T00:00:00Z
```

#### 3.1.4 `cancel` — Cancel a job [IMPLEMENTED]

```
Usage: harness cancel <job_id> [OPTIONS]

Cancel a queued, leased, or running job. Cancellation is cooperative —
running workers check for cancellation signals between DAG levels.

Arguments:
  job_id               UUID of the job to cancel

Options:
  --force              Force immediate termination (SIGKILL worker), may leave partial state
  --reason TEXT        Reason for cancellation (logged)

Output (JSON):
  {"job_id": "<uuid>", "status": "canceled", "message": "Job canceled by user", "reason": "..."}

Exit codes:
  0  — Job successfully canceled
  1  — Job not found (E1001)
  2  — Job already in terminal state (E1002)

Examples:
  harness cancel abc123-def456
  harness cancel abc123-def456 --reason "Changed requirements"
```

#### 3.1.5 `worker` — Start a worker process [IMPLEMENTED]

```
Usage: harness worker [OPTIONS]

Start a worker process that polls the job queue and executes DAGs.
This is the primary execution engine for the harness.

Options:
  --concurrency N      Max parallel DAG node executions (default: 3)
  --poll-interval SEC  Queue polling interval in seconds (default: 5)
  --max-jobs N         Max jobs to process before exiting (default: unlimited, 0=infinite)
  --lease-timeout SEC  Seconds before a lease is considered expired (default: 60)
  --single             Process one job and exit (useful for cron/systemd)
  --recover            On startup, scan for orphaned leases and running jobs to recover

Output (JSON Lines, one per event):
  {"timestamp": "...", "event": "worker.started", "concurrency": 3, "poll_interval": 5}
  {"timestamp": "...", "event": "job.leased", "job_id": "...", "run_id": "..."}
  {"timestamp": "...", "event": "node.started", "job_id": "...", "node_id": "...", "agent_type": "..."}
  {"timestamp": "...", "event": "node.completed", "job_id": "...", "node_id": "..."}
  {"timestamp": "...", "event": "job.completed", "job_id": "...", "status": "succeeded"}

Exit codes:
  0  — Graceful shutdown (SIGTERM received or --max-jobs reached)
  3  — Unrecoverable error (E2001)

Examples:
  harness worker
  harness worker --concurrency 5 --poll-interval 10
  harness worker --single --recover
```

#### 3.1.6 `retry` — Retry a failed/dead-letter job [IMPLEMENTED]

```
Usage: harness retry <job_id> [OPTIONS]

Manually retry a failed or dead-letter job. Creates a new Run with the same DAG.

Arguments:
  job_id               UUID of the failed/dead-letter job

Options:
  --reset-attempts     Reset attempt counter to 0 (for dead-letter jobs)
  --edit               Open the DAG plan in $EDITOR before retrying

Output (JSON):
  {"job_id": "<original>", "new_job_id": "<uuid>", "status": "queued", "attempt": 4, "message": "Retry queued"}

Examples:
  harness retry abc123-def456
  harness retry abc123-def456 --reset-attempts
```

#### 3.1.7 `tickets` — List approval tickets [IMPLEMENTED] — M1.1

```
Usage: harness tickets [OPTIONS]

List approval tickets with optional filtering.

Options:
  --status STATUS      Filter by status: pending|approved|rejected|expired
  --job JOB_ID         Filter by associated job ID

Output (JSON):
  {
    "tickets": [...],
    "count": 3,
    "stats": {"pending": 1, "approved": 1, "rejected": 0, "expired": 1}
  }
```

#### 3.1.8 `approve` — Approve a pending ticket [IMPLEMENTED] — M1.1

```
Usage: harness approve <ticket_id> [OPTIONS]

Approve a pending approval ticket, allowing the blocked tool call to proceed.

Arguments:
  ticket_id            ID of the approval ticket

Options:
  --reason TEXT        Approval reason (logged)

Output (JSON):
  {"ticket_id": "...", "status": "approved", "previous_status": "pending", "message": "Ticket approved"}

Exit codes:
  0  — Ticket successfully approved
  1  — Ticket not found (E3001) or not in pending state (E3002)
```

#### 3.1.9 `reject` — Reject a pending ticket [IMPLEMENTED] — M1.1

```
Usage: harness reject <ticket_id> [OPTIONS]

Reject a pending approval ticket, causing the blocked tool call to fail.

Arguments:
  ticket_id            ID of the approval ticket

Options:
  --reason TEXT        Rejection reason (logged)

Output (JSON):
  {"ticket_id": "...", "status": "rejected", "previous_status": "pending", "message": "Ticket rejected"}

Exit codes:
  0  — Ticket successfully rejected
  1  — Ticket not found (E3001) or not in pending state (E3003)
```

---

### 3.2 Unified Output Format

All commands produce **structured JSON output** to stdout. Human-readable messages go to stderr.

#### 3.2.1 Success Response Format

```json
{
  "success": true,
  "timestamp": "2024-01-15T09:30:00.123456Z",
  "data": { ... command-specific payload ... }
}
```

#### 3.2.2 Error Response Format

```json
{
  "success": false,
  "timestamp": "2024-01-15T09:30:00.123456Z",
  "error": {
    "code": "E1001",
    "message": "Job not found: abc123",
    "detail": "Searched in ./data/queue/ and ./data/queue/dead/",
    "suggestion": "Use 'harness list' to see available jobs."
  }
}
```

#### 3.2.3 Worker Event Stream Format

Worker output is **JSON Lines** (one JSON object per line, suitable for `jq` streaming):

```jsonl
{"ts":"2024-01-15T09:30:00Z","lvl":"INFO","evt":"worker.start","pid":12345}
{"ts":"2024-01-15T09:30:05Z","lvl":"INFO","evt":"poll","queue_depth":2}
{"ts":"2024-01-15T09:30:05Z","lvl":"INFO","evt":"job.leased","job_id":"j-abc","run_id":"r-xyz"}
{"ts":"2024-01-15T09:30:06Z","lvl":"INFO","evt":"node.start","job_id":"j-abc","node_id":"plan","agent":"planner"}
{"ts":"2024-01-15T09:30:12Z","lvl":"INFO","evt":"node.end","job_id":"j-abc","node_id":"plan","status":"success","dur_ms":6000}
{"ts":"2024-01-15T09:30:12Z","lvl":"INFO","evt":"node.start","job_id":"j-abc","node_id":"impl","agent":"generator"}
{"ts":"2024-01-15T09:30:45Z","lvl":"INFO","evt":"node.end","job_id":"j-abc","node_id":"impl","status":"success","dur_ms":33000}
{"ts":"2024-01-15T09:30:45Z","lvl":"INFO","evt":"job.end","job_id":"j-abc","run_id":"r-xyz","status":"succeeded"}
```

---

### 3.3 Error Code Reference

#### 3.3.1 Job-Level Errors (E1xxx)

| Code | Name | HTTP Equiv | Description |
|------|------|-----------|-------------|
| `E1001` | `JobNotFound` | 404 | The specified job_id does not exist in any queue |
| `E1002` | `InvalidStatusTransition` | 409 | Attempted a state transition that violates the state machine |
| `E1003` | `JobAlreadyExists` | 409 | A job with the same requirement hash was submitted within the last 60 seconds |
| `E1004` | `LeaseConflict` | 423 | Another worker holds the lease on this job |
| `E1005` | `LeaseExpired` | 410 | The lease was held but expired before work completed |
| `E1006` | `MaxAttemptsExceeded` | 422 | The job has reached max_attempts and cannot be retried automatically |
| `E1007` | `JobTimeout` | 408 | Overall job timeout (--timeout) was exceeded |
| `E1008` | `CancelRejected` | 409 | Cannot cancel a job in a terminal state |
| `E1009` | `QueueFull` | 503 | The job queue has reached its maximum capacity (default: 1000) |

#### 3.3.2 Execution Errors (E2xxx)

| Code | Name | Description |
|------|------|-------------|
| `E2001` | `WorkerFatalError` | Unrecoverable worker error — worker exits |
| `E2002` | `DAGExecutionError` | DAG engine encountered an internal error (e.g., cycle detected) |
| `E2003` | `AgentExecutionError` | Agent worker raised an unhandled exception |
| `E2004` | `LLMUnavailable` | LLM API is unreachable after all retries |
| `E2005` | `ToolExecutionError` | Tool registry failed to execute a tool |
| `E2006` | `OrchestratorError` | Orchestrator failed to produce valid plan/decision |
| `E2007` | `EvaluatorError` | Evaluator failed to run checks |
| `E2008` | `ArtifactNotFound` | Expected output artifact missing after node completion |

#### 3.3.3 Configuration & Guardrail Errors (E3xxx)

| Code | Name | Description |
|------|------|-------------|
| `E3001` | `InvalidConfig` | Configuration file is missing or malformed |
| `E3002` | `GuardrailBlocked` | Tool call blocked by guardrail policy |
| `E3003` | `HumanApprovalRequired` | High-risk action requires human confirmation (default mode) |
| `E3001` | `TicketNotFound` | Approval ticket does not exist |
| `E3002` | `ApproveFailed` | Cannot approve ticket (not pending or already decided) |
| `E3003` | `RejectFailed` | Cannot reject ticket (not pending or already decided) |
| `E3004` | `PermissionDenied` | Tool/command explicitly denied by policy |
| `E3005` | `IterationLimitExceeded` | Agent exceeded max_iterations guardrail |
| `E3006` | `ContextWindowExceeded` | Agent context exceeded max_context_tokens |
| `E3007` | `UnknownAgentType` | DAG references an agent type not in AgentRegistry |

#### 3.3.4 System Errors (E4xxx)

| Code | Name | Description |
|------|------|-------------|
| `E4001` | `EventStoreError` | Cannot read/write event log file |
| `E4002` | `DiskFull` | Insufficient disk space for artifacts/events |
| `E4003` | `QueueCorruption` | Queue file is malformed or unreadable |
| `E4004` | `RecoveryFailed` | Failed to recover state from event log on restart |

---

### 3.4 Exit Code Convention

| Exit Code | Meaning | When Used |
|-----------|---------|-----------|
| `0` | Success | Command completed successfully |
| `1` | General error | Unhandled exception, config missing |
| `2` | Invalid usage | Bad arguments, missing required params |
| `3` | Worker fatal | Unrecoverable error in worker mode |
| `4` | Guardrail blocked | Action blocked by policy (requires user intervention) |
| `130` | Interrupted | SIGINT (Ctrl+C) received |
| `143` | Terminated | SIGTERM received |

---

### 3.5 CLI Command → Exit Code Mapping

| Command | Exit 0 | Exit 1 | Exit 2 | Exit 3 | Exit 4 |
|---------|--------|--------|--------|--------|--------|
| `submit` | Job queued | E1xxx error | Invalid args | — | — |
| `status` | Status returned | E1001/E4001 | Invalid args | — | — |
| `list` | List returned | E4001 | Invalid args | — | — |
| `cancel` | Job canceled | E1001/E1008 | Invalid args | — | — |
| `worker` | Graceful shutdown | E2001 | Invalid args | Worker fatal | E3002/E3003 |
| `retry` | Retry queued | E1001/E1006 | Invalid args | — | — |

---

## Part 4 — Definition of Done (DoD)

### 4.1 Functional Acceptance Criteria

#### A1. Continuous Task Processing
> **Criterion:** The system must process >= 20 consecutive tasks without requiring human intervention for flow control (planning, execution, failure handling, status reporting).

| # | Test Case | Pass Criteria |
|---|-----------|---------------|
| A1.1 | Submit 20 jobs with varying requirements | All 20 jobs reach a terminal state within 4 hours | [IMPLEMENTED] |
| A1.2 | No job remains stuck in `QUEUED`, `LEASED`, or `RUNNING` | `harness list --status running` returns empty 30 min after last submission | [IMPLEMENTED] |
| A1.3 | Worker operates without restart | Single `harness worker` process handles all 20 jobs |
| A1.4 | No manual `cancel` or `retry` needed | Zero user commands issued between submit and final status check |

**Measurement:** Automatable integration test that submits 20 synthetic requirements and asserts all terminal states.

---

#### A2. Deterministic Terminal States
> **Criterion:** Every job MUST end in exactly one of: `succeeded`, `failed`, `canceled`, or `dead_letter`. No job may remain in an intermediate state indefinitely.

| # | Test Case | Pass Criteria |
|---|-----------|---------------|
| A2.1 | Query all jobs after batch completion | `harness list --status queued,leased,running` returns empty | [IMPLEMENTED] |
| A2.2 | Verify terminal state distribution | Count of (succeeded + failed + canceled + dead_letter) == total submitted | [IMPLEMENTED] |
| A2.3 | Verify no state oscillation | Each job's event log contains exactly one terminal state event | [IMPLEMENTED] |
| A2.4 | Dead letter enforcement | Jobs failing 3 times appear in `./data/queue/dead/` with complete history | [IMPLEMENTED] |

---

#### A3. Replan Closed-Loop Success
> **Criterion:** When the Orchestrator decides `replan` on failure, the new plan must be queued, executed, and the overall task must eventually succeed.

| # | Test Case | Pass Criteria |
|---|-----------|---------------|
| A3.1 | Submit a task that triggers replan (e.g., agent type mismatch) | Orchestrator emits `action=replan` decision | [IMPLEMENTED] |
| A3.2 | Verify new job creation | A new job appears in queue with `parent_job_id` referencing original | [IMPLEMENTED] |
| A3.3 | Verify chain success | The replanned job reaches `SUCCEEDED` | [IMPLEMENTED] |
| A3.4 | Verify artifact continuity | New job has access to artifacts from parent job's completed nodes | [IMPLEMENTED] |
| A3.5 | Event log completeness | Both original and replan jobs have complete event logs traceable via `parent_job_id` | [IMPLEMENTED] |

**Simulated scenario:** Submit "Implement feature X using nonexistent agent" → fails → orchestrator replans with valid agent → succeeds.

---

#### A4. High-Risk Action Confirmation
> **Criterion:** HIGH/CRITICAL risk tool calls must not execute without explicit human confirmation in `default` permission mode.

| # | Test Case | Pass Criteria |
|---|-----------|---------------|
| A4.1 | `bash` tool invoked with destructive command | Guardrail returns `E3003 HumanApprovalRequired`, tool NOT executed | [IMPLEMENTED] |
| A4.2 | `write` tool in `default` mode | Guardrail returns `E3003` (write is MEDIUM risk, requires approval in default mode) | [IMPLEMENTED] |
| A4.3 | `read` tool in any mode | Auto-approved, no confirmation needed | [IMPLEMENTED] |
| A4.4 | `bash` tool in `accept_edits` mode | Returns `E3002 GuardrailBlocked` (bash is HIGH risk, exceeds accept_edits threshold) | [IMPLEMENTED] |
| A4.5 | Confirm and approve | After user inputs `y`, tool executes and returns success | [IMPLEMENTED] |
| A4.6 | Deny and skip | After user inputs `n`, tool returns error, agent continues without it | [IMPLEMENTED] |

**Risk mapping verification:**
- `read`, `glob`, `grep` → `RiskLevel.LOW` → Auto-approve
- `write`, `edit`, `git` → `RiskLevel.MEDIUM` → Auto-approve in `accept_edits` / require approval in `default`
- `bash` → `RiskLevel.HIGH` → Require approval in all modes except `dont_ask` (if explicitly allowed)

---

#### A5. Metrics & Alerting
> **Criterion:** The system must report operational metrics and trigger at least one class of actionable alert.

| # | Test Case | Pass Criteria |
|---|-----------|---------------|
| A5.1 | `harness status <job_id>` includes metrics | Response contains `progress`, `duration_ms`, `token_usage` fields | [IMPLEMENTED] |
| A5.2 | Post-batch metrics report | Scriptable: `harness list --status all --format json | jq '.metrics'` returns aggregate stats |
| A5.3 | Failure rate alert | When failed job rate > 50% in last 10 jobs, worker stderr emits `ALERT` line with JSON payload |
| A5.4 | Timeout alert | When a job exceeds 80% of `--timeout`, worker emits `ALERT: job_timeout_approaching` |
| A5.5 | Event log size | `./data/events/` contains one `.jsonl` file per job with >= 10 events per completed job |

**Alert format:**
```jsonl
{"ts":"2024-01-15T09:30:00Z","lvl":"ALERT","alert_type":"high_failure_rate","threshold":0.5,"actual":0.7,"window":10,"message":"7/10 recent jobs failed"}
{"ts":"2024-01-15T09:30:00Z","lvl":"ALERT","alert_type":"timeout_approaching","job_id":"...","timeout_sec":600,"elapsed_sec":540,"message":"Job at 90% of timeout"}
```

---

#### A6. Restart Recovery
> **Criterion:** After worker process restart, in-flight and orphaned jobs must be recovered and resumed or retried correctly.

| # | Test Case | Pass Criteria |
|---|-----------|---------------|
| A6.1 | Kill worker during job execution | SIGKILL worker while RUNNING job has 2/5 nodes completed |
| A6.2 | Restart worker with `--recover` | Worker identifies orphaned RUNNING job from stale event log |
| A6.3 | Verify job continuation | Job resumes from start of next incomplete DAG level (not from beginning) |
| A6.4 | Verify completed nodes intact | Already-successful nodes are not re-executed; their artifacts preserved |
| A6.5 | Lease expiry recovery | Jobs in LEASED with timestamp > 60s old are returned to QUEUED |
| A6.6 | Event log integrity | No duplicate events for already-completed nodes in recovered job |

**Recovery algorithm:**
1. Scan `./data/queue/*.leased` — check `lease_timestamp`
2. If `now - lease_timestamp > lease_timeout` → rename to `.queued`
3. Scan `./data/events/*.jsonl` — find jobs with last event ≠ terminal
4. For each interrupted job, replay events to rebuild DAG state
5. Nodes with `completed` events are marked `SUCCESS`
6. Remaining pending nodes are re-scheduled from their topological level

---

### 4.2 Non-Functional Acceptance Criteria

| # | Criterion | Target |
|---|-----------|--------|
| N1 | **Cold start time** | `harness submit` returns within 3 seconds |
| N2 | **Worker poll latency** | Time from `submit` to `LEASED` < 10 seconds (with idle worker) |
| N3 | **Event log write latency** | Event persistence < 10ms per event |
| N4 | **Memory footprint** | Worker process < 512MB RSS under normal load |
| N5 | **Disk usage** | Event logs + artifacts < 100MB per 100 jobs (typical) |
| N6 | **Log readability** | Worker JSONL output parseable by `jq` without preprocessing |
| N7 | **Config reload** | Worker picks up config changes on restart (no hot reload required) |

---

### 4.3 Test Evidence Requirements

For each acceptance criterion (A1-A6), the following evidence must exist:

1. **Automated test** in `./tests/test_m1_acceptance.py` that can be run via `pytest`
2. **Event log artifact** from the test run, stored in `./data/events/`
3. **Metrics snapshot** showing the criterion was met

---

## Appendix A — Data Models Reference

### A.1 JobRecord

```python
class JobRecord(BaseModel):
    """Persistent record of a submitted job."""
    job_id: str                          # UUID v4
    requirement: str                     # Original user requirement
    status: JobStatus                    # Current status
    project_path: str | None             # --project path
    timeout: int                         # --timeout seconds
    max_attempts: int                    # --max-attempts
    permission_mode: PermissionMode      # --mode
    priority: int                        # --priority
    tag: str | None                      # --tag
    parent_job_id: str | None            # Set for replan jobs
    attempt_count: int = 0               # How many Runs created
    created_at: datetime
    updated_at: datetime
    queued_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    dag_plan: dict | None                # Serialized DAG
    last_error: str = ""
```

### A.2 RunRecord

```python
class RunRecord(BaseModel):
    """A single execution attempt of a Job."""
    run_id: str                          # UUID v4
    job_id: str                          # Parent job reference
    status: RunStatus                    # Current status
    dag_snapshot: dict                   # DAG as of Run start
    started_at: datetime
    finished_at: datetime | None
    node_results: dict[str, dict] = {}   # node_id -> {status, error, artifacts}
    orchestrator_decisions: list[dict] = []  # FailureDecision log
    total_duration_ms: int = 0
```

### A.3 Queue File Format

Jobs are stored as JSON files in `./data/queue/`:

```
./data/queue/
  pending/
    {job_id}.json          # JobRecord in JSON
  leased/
    {job_id}.leased        # Atomic rename from pending/
  dead/
    {job_id}.json          # Dead letter with failure history
```

---

## Appendix B — Architecture Overview

### B.1 Component Interaction Diagram

```
                    +------------+
                    |   User     |
                    |  (CLI)     |
                    +----+-------+
                         |
              +----------+----------+----------+
              |          |          |          |
              v          v          v          v
         +--------+ +--------+ +-------+ +---------+
         | submit | | status | | list  | | cancel  |
         +----+---+ +----+---+ +---+---+ +----+----+
              |          |          |          |
              +----------+----------+----------+
                         |
                    +----v---------------+
                    |   Queue Manager    |
                    |  (filesystem)      |
                    +----+-------+-------+
                         |       |
              +----------+       +----------+
              |                          |
              v                          v
       +-------------+           +-------------+
       | Job Records |           | Event Store |
       |  (JSON)     |           |   (JSONL)   |
       +-------------+           +------+------+
                                        |
                                        v
                              +---------+---------+
                              |   Worker Process  |
                              |  (harness worker) |
                              +----+-------+------+
                                   |       |
                         +---------+       +---------+
                         |                           |
                         v                           v
                  +-------------+          +-----------------+
                  | DAG Engine  |          |   Agent Pool    |
                  | (topo sort  |          | (WorkerAgent    |
                  |  + parallel)|          |   instances)    |
                  +------+------+          +--------+--------+
                         |                          |
                +--------+--------+        +--------+--------+
                |                 |        |        |        |
                v                 v        v        v        v
         +-----------+   +-----------+ +------+ +------+ +------+
         |Orchestrator|   | Evaluator | |planner| |generator| |evaluator|
         |  (LLM)    |   |  (checks) | +------+ +------+ +------+
         +-----------+   +-----------+     |        |        |
                                          +--------+--------+|
                                          |  Tool Registry   ||
                                          | (read/write/     ||
                                          |  bash/glob/grep) ||
                                          +--------+--------++
                                                   |         |
                                          +--------+         |
                                          v                  v
                                    +------------+   +-----------+
                                    | Guardrails |   | LLMClient |
                                    | (policy)   |   | (API)     |
                                    +------------+   +-----------+
```

### B.2 File Layout

```
harness/
├── core/
│   ├── models.py              # DAG, DAGNode, FailureDecision, etc.
│   ├── dag_engine.py          # DAGExecutionEngine
│   ├── config.py              # HarnessConfig, LLMConfig
│   ├── llm_client.py          # LLM API client
│   └── agent_registry.py      # AgentRegistry
├── orchestrator/
│   └── intelligent_orchestrator.py  # Planning & failure adaptation
├── agent/
│   ├── worker.py              # AgentWorker (LLM loop)
│   └── agent_pool.py          # AgentPool (instance management)
├── guardrails/
│   └── policy.py              # Guardrails & PermissionMode
├── session/
│   └── store.py               # SessionStore (JSONL events)
├── tools/
│   └── registry.py            # ToolRegistry (built-in + MCP)
├── evaluator/
│   └── engine.py              # EvaluatorEngine (quality gates)
├── reporter/
│   └── logger.py              # Metrics & alerting
├── visualizer/
│   ├── cli_renderer.py        # CLI DAG visualization
│   ├── event_bridge.py        # WebSocket event bridge
│   └── server.py              # Web dashboard server
├── tests/
│   ├── test_dag_engine.py
│   ├── test_models.py
│   ├── test_worker.py
│   ├── test_evaluator.py
├── data/                      # Runtime data (gitignored)
│   ├── events/                # JSONL event logs
│   ├── artifacts/             # Generated artifacts
│   ├── plans/                 # Saved DAG plans
│   └── queue/                 # Job queue files
│       ├── pending/
│       ├── leased/
│       └── dead/
├── docs/
│   └── m1_personal_spec.md    # This document
├── main.py                    # CLI entry point
└── conftest.py                # Test fixtures
```

### B.3 Key Existing Models (from `core/models.py`)

| Model | Purpose |
|-------|---------|
| `DAG` / `DAGNode` / `DAGEdge` | Execution plan — nodes are agent tasks, edges are dependencies |
| `NodeStatus` | PENDING, RUNNING, SUCCESS, FAILED, SKIPPED, RETRYING |
| `ExecutionEvent` | Immutable event: started/completed/failed/retrying/skipped |
| `FailureDecision` | Orchestrator output: retry/skip/abort/replan |
| `RiskLevel` | LOW(1), MEDIUM(2), HIGH(3), CRITICAL(4) |
| `PermissionMode` | plan/default/accept_edits/auto/dont_ask |
| `GuardrailPolicy` | Per-session policy config |
| `Event` / `EventType` | Session-level event sourcing |
| `SessionState` | Recoverable state derived from event replay |
| `SessionMetrics` | Token usage, duration, error counts |
| `HandoffArtifact` | Structured inter-agent data transfer |
| `EvaluationResult` | Evaluator pass/fail with score and feedback |
| `AgentCapability` | Registered agent type description |

---

## M1 完成状态

| 任务 | 状态 |
|------|------|
| Task 01 — 规格文档 | ✅ 已完成 |
| Task 02 — 数据模型 | ✅ 已完成 |
| Task 03 — 持久化仓储 | ✅ 已完成 |
| Task 04 — CLI 控制面 | ✅ 已完成 |
| Task 05 — 执行服务 | ✅ 已完成 |
| Task 06 — Worker 队列 | ✅ 已完成 |
| Task 07 — 超时/重试/死信 | ✅ 已完成 |
| Task 08 — replan 闭环 | ✅ 已完成 |
| Task 09 — 个人模式 Guardrails | ✅ 已完成 |
| Task 10 — 指标聚合 | ✅ 已完成 |
| Task 11 — 告警 | ✅ 已完成 |
| Task 12 — 重启恢复 | ✅ 已完成 |
| Task 13 — 测试补齐 | ✅ 已完成 |
| Task 14 — 文档更新 | ✅ 已完成 |

完成日期: 2026-05-09

---

## Revision History

| Version | Date | Author | Changes |
|---------|------|--------|---------|
| 1.0.0 | 2024-01-15 | Spec Team | Initial M1 Personal Edition specification |

---

*End of Document*
