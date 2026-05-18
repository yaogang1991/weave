# Control Plane Module SPEC

## Purpose

Provides the job queue, run tracking, approval ticket system, execution service, and asynchronous worker that together form the control plane for the Weave. Manages the full lifecycle of a job: submission, lease acquisition, DAG planning and execution, retry/dead-letter handling, approval gating for high-risk tool calls, and graceful worker shutdown.

Sources: `control_plane/models.py`, `control_plane/repository.py`, `control_plane/service.py`, `control_plane/worker.py`, `control_plane/approval.py`, `control_plane/hooks.py`, `control_plane/execution_factory.py`, `control_plane/job_lifecycle.py`, `control_plane/run_lifecycle.py`, `control_plane/backend_lifecycle.py`, `control_plane/worker_executor.py`, `control_plane/worker_recovery.py`

---

## Public Interfaces

### models.py

#### Enum: `JobStatus(str, Enum)`

| Value | Description |
|---|---|
| `QUEUED` | Waiting to be leased. |
| `LEASED` | Acquired by a worker, not yet running. |
| `RUNNING` | Worker is actively executing. |
| `PENDING_APPROVAL` | Paused awaiting human approval. |
| `SUCCEEDED` | Terminal: completed successfully. |
| `FAILED` | Terminal (possibly retried). |
| `CANCELED` | Terminal: cancelled by user. |
| `DEAD_LETTER` | Terminal: exhausted retries. |

#### Enum: `RunStatus(str, Enum)`

| Value | Description |
|---|---|
| `RUNNING` | Execution in progress. |
| `PENDING_APPROVAL` | Paused for human approval. |
| `SUCCEEDED` | Completed successfully. |
| `FAILED` | Execution failed. |
| `ABORTED` | Aborted (e.g., approval rejected). |
| `TIMED_OUT` | Exceeded wall-clock timeout. |

#### Enum: `TicketStatus(str, Enum)`

| Value | Description |
|---|---|
| `PENDING` | Awaiting decision. |
| `APPROVED` | Approved. |
| `REJECTED` | Rejected. |
| `EXPIRED` | Timed out without decision. |

#### Model: `RetryPolicy(BaseModel)`

| Field | Type | Default | Validation |
|---|---|---|---|
| `max_attempts` | `int` | 3 | `>= 1` |
| `backoff_sec` | `int` | 5 | `>= 1` |

#### Model: `Job(BaseModel)`

| Field | Type | Default |
|---|---|---|
| `id` | `str` | -- |
| `requirement` | `str` | -- |
| `status` | `JobStatus` | `QUEUED` |
| `project_path` | `str \| None` | `None` |
| `retry_policy` | `RetryPolicy` | `RetryPolicy()` |
| `attempt` | `int` | 0 |
| `last_error` | `str` | `""` |
| `error_category` | `str` | `""` |
| `created_at` | `datetime` | -- |
| `updated_at` | `datetime` | -- |
| `lease_owner` | `str \| None` | `None` |
| `lease_expires_at` | `datetime \| None` | `None` |
| `metadata` | `dict[str, Any]` | `{}` |

Methods:
- `bump_attempt() -> None` -- Increment `attempt`.
- `is_terminal() -> bool` -- True if status is `SUCCEEDED`, `FAILED`, `CANCELED`, or `DEAD_LETTER`.
- `is_active() -> bool` -- True if status is `QUEUED`, `LEASED`, or `RUNNING`.

Valid `error_category` values: `""`, `"timeout"`, `"eval_failed"`, `"tool_blocked"`, `"unknown"`, `"watchdog"`.

#### Model: `Run(BaseModel)`

| Field | Type | Default |
|---|---|---|
| `id` | `str` | -- |
| `job_id` | `str` | -- |
| `session_id` | `str` | -- |
| `status` | `RunStatus` | `RUNNING` |
| `dag_result` | `dict[str, Any]` | `{}` |
| `started_at` | `datetime` | -- |
| `completed_at` | `datetime \| None` | `None` |
| `created_at` | `datetime` | -- |
| `updated_at` | `datetime` | -- |

Method: `is_terminal() -> bool` -- True if status is `SUCCEEDED`, `FAILED`, `ABORTED`, or `TIMED_OUT`.

#### Model: `Ticket(BaseModel)` (in models.py)

| Field | Type | Default |
|---|---|---|
| `id` | `str` | -- |
| `job_id` | `str` | -- |
| `tool_name` | `str` | -- |
| `status` | `TicketStatus` | `PENDING` |
| `risk_level` | `str` | `"medium"` |
| `args_preview` | `str` | `""` |
| `reason` | `str` | `""` |
| `requested_at` | `datetime` | -- |
| `expires_at` | `datetime \| None` | `None` |
| `resolved_at` | `datetime \| None` | `None` |

Method: `is_expired() -> bool`.

---

### repository.py -- `JobRepository`

```python
class JobRepository:
    def __init__(self, base_path: str = "./data/jobs")
```

**Job CRUD:**
- `create_job(requirement: str, project_path: str | None = None, retry_policy: RetryPolicy | None = None) -> Job`
- `get_job(job_id: str) -> Job | None`
- `update_job(job: Job) -> Job`
- `list_jobs(status: JobStatus | None = None) -> list[Job]`

**Run CRUD:**
- `create_run(job_id: str, session_id: str) -> Run`
- `get_run(run_id: str) -> Run | None`
- `update_run(run: Run) -> Run`
- `list_runs_by_job(job_id: str) -> list[Run]`

**Status transitions:**
- `transition_job_status(job_id: str, to_status: JobStatus, error: str = "", error_category: str = "") -> Job` -- Validates against `_VALID_TRANSITIONS` allowlist. Special handling for `FAILED -> QUEUED` (bumps attempt, clears error/lease) and `* -> DEAD_LETTER` (clears lease).

Valid transitions:
```
QUEUED   -> LEASED, CANCELED
LEASED   -> RUNNING, QUEUED, CANCELED
RUNNING  -> SUCCEEDED, FAILED, CANCELED
FAILED   -> QUEUED (retry), DEAD_LETTER (exhausted)
Terminal -> (none)
```

**Lease management:**
- `acquire_lease(job_id: str, owner: str, lease_duration_sec: int = 60) -> Job | None` -- Only `QUEUED` or expired-`LEASED` jobs can be leased.
- `release_lease(job_id: str) -> Job` -- Sets status back to `QUEUED`, clears lease fields.
- `get_stale_leases(max_age_sec: int = 120) -> list[Job]` -- Jobs in `LEASED` status with expired leases.

**Recovery:**
- `list_active_jobs() -> list[Job]` -- Jobs in `QUEUED`, `LEASED`, or `RUNNING`.
- `recover_orphan_jobs() -> list[Job]` -- Leased/running jobs with expired leases.

All writes are atomic (write-to-temp + `os.replace`).

---

### approval.py -- `ApprovalRepository`

```python
class ApprovalRepository:
    def __init__(self, base_path: str = "./data/approvals")
```

#### Model: `ApprovalTicket(BaseModel)`

| Field | Type | Default |
|---|---|---|
| `id` | `str` | -- |
| `job_id` | `str` | -- |
| `run_id` | `str \| None` | `None` |
| `node_id` | `str \| None` | `None` |
| `tool_name` | `str` | -- |
| `args_hash` | `str` | -- |
| `args_preview` | `str` | -- |
| `risk_level` | `str` | -- |
| `status` | `TicketStatus` | `PENDING` |
| `requested_at` | `datetime` | -- |
| `decided_at` | `datetime \| None` | `None` |
| `decided_by` | `str \| None` | `None` |
| `reason` | `str` | `""` |
| `expires_at` | `datetime \| None` | `None` |
| `created_at` | `datetime` | -- |
| `updated_at` | `datetime` | -- |

Methods:
- `is_terminal() -> bool`
- `is_pending() -> bool`
- `verify_args(args: dict) -> bool` -- Tamper check via SHA-256 hash.

**CRUD:**
- `create_ticket(job_id: str, tool_name: str, args: dict, risk_level: str = "high", run_id: str | None = None, node_id: str | None = None, timeout_sec: int = 300) -> ApprovalTicket`
- `get_ticket(ticket_id: str) -> ApprovalTicket | None`
- `update_ticket(ticket: ApprovalTicket) -> ApprovalTicket`
- `list_tickets(status: TicketStatus | None = None, job_id: str | None = None, tool_name: str | None = None) -> list[ApprovalTicket]`

**Status transitions:**
- `approve_ticket(ticket_id: str, reason: str = "", decided_by: str = "user") -> ApprovalTicket`
- `reject_ticket(ticket_id: str, reason: str = "", decided_by: str = "user") -> ApprovalTicket`
- `expire_tickets() -> list[ApprovalTicket]` -- Scan and mark all expired `PENDING` tickets.

**Query helpers:**
- `get_pending_for_job(job_id: str) -> list[ApprovalTicket]`
- `get_stats() -> dict[str, int]` -- Count per status.

---

### service.py -- `RunService`

```python
class RunService:
    def __init__(
        self,
        repository: JobRepository,
        llm_config: LLMConfig,
        max_parallel: int = 3,
        agent_timeout: int = 120,
        max_context_tokens: int = 100000,
        artifact_path: str = "./data/artifacts",
        event_store_path: str = "./data/events",
        max_iterations: int = 50,
        policy: GuardrailPolicy | None = None,
        default_backend: str = "local",
        backend_base_path: str = "./data/backends",
        non_interactive: bool = False,
        approval_repo: Any | None = None,
        approval_timeout_sec: int = 300,
    )
```

**Public methods:**
- `async submit_job(requirement: str, project_path: str | None = None, timeout: int = 600, max_attempts: int = 3) -> Job`
- `async run_job(job_id: str) -> Run` -- Full lifecycle: lease, plan, execute, summarize, retry/dead-letter.
- `async get_job_status(job_id: str) -> dict[str, Any]` -- Job fields plus all runs.
- `async list_jobs(status: JobStatus | None = None) -> list[Job]`
- `async cancel_job(job_id: str) -> Job`
- `async handle_job_failure(job: Job, error: str, error_category: str = "unknown") -> Job` -- Retry (`FAILED -> QUEUED`) or dead-letter (`FAILED -> DEAD_LETTER`).
- `async resume_after_approval(job_id: str, ticket_id: str) -> Run | None`
- `async abort_after_rejection(job_id: str, ticket_id: str, reason: str = "") -> Job`

**Internal methods:**
- `async _execute_plan_and_run(job, session_id, store, work_dir, run_id=None) -> Any`
- `_register_hooks() -> None`
- `_create_orchestrator(store) -> IntelligentOrchestrator`
- `_create_execution_engine(session_id, store, replan_handler, work_dir, memory_manager) -> DAGExecutionEngine`
- `async _run_before_hooks(ctx) -> None`
- `async _run_after_hooks(ctx, result_dag) -> None`

---

### worker.py -- `TaskWorker`

---

### hooks.py -- Execution Hooks

Execution hooks decouple subsystems (memory, learning, impact analysis) from the core execution flow in `RunService._execute_plan_and_run`.

#### Model: `ExecutionContext`

| Field | Type | Default | Description |
|---|---|---|---|
| `job` | `Any` | -- | Current Job |
| `session_id` | `str` | -- | Session identifier |
| `store` | `Any` | -- | SessionStore |
| `work_dir` | `Path` | -- | Execution working directory |
| `run_id` | `str \| None` | `None` | Run identifier |
| `memory_manager` | `Any \| None` | `None` | Per-job MemoryManager (set by MemoryHook) |
| `llm_config` | `Any \| None` | `None` | LLM configuration |
| `repository` | `Any \| None` | `None` | JobRepository instance |
| `metadata` | `dict[str, Any]` | `{}` | Merged into job.metadata after execution |
| `_state` | `dict[str, Any]` | `{}` | Internal state shared between before/after hooks |

#### Abstract class: `ExecutionHook`

```python
class ExecutionHook(ABC):
    async def before_execution(self, ctx: ExecutionContext) -> None: ...
    async def after_execution(self, ctx: ExecutionContext, result_dag: Any) -> None: ...
```

All hook errors are caught and logged — they never abort execution.

#### `MemoryHook`

Creates a per-job `MemoryManager` and attaches it to `ctx.memory_manager`. Service-level maintenance runs once via `threading.Lock`.

| Phase | Action |
|---|---|
| `before_execution` | Create `MemoryManager`, run once-only maintenance, store in `ctx.memory_manager` |
| `after_execution` | No-op |

#### `LearningHook`

Triggers learning analysis if due. Exposes `optimizer` for RunService to inject into Orchestrator.

Constructor: `LearningHook(repository: Any | None = None)`

| Phase | Action |
|---|---|
| `before_execution` | Call `scheduler.maybe_run_analysis()` |
| `after_execution` | No-op |

Exposed attributes:
- `optimizer: Any | None` — `LearningOptimizer` instance for planning hints

#### `ImpactHook`

Predicts impact before execution; verifies changes after.

Constructor: `ImpactHook(llm_config: Any | None = None)`

| Phase | Action |
|---|---|
| `before_execution` | Predict impact scope, capture file snapshot, store in `ctx._state` and `ctx.metadata` |
| `after_execution` | Verify changes, store learning in memory, persist impact record to `data/impact/` |

#### Hook Registration (in RunService)

```python
def _register_hooks(self) -> None:
    self._hooks = [
        MemoryHook(),                            # Must run first (sets ctx.memory_manager)
        LearningHook(repository=self.repository), # DI: repository
        ImpactHook(llm_config=self.llm_config),  # DI: llm_config
    ]
```

Ordering invariant: `MemoryHook` runs before `ImpactHook` so `ctx.memory_manager` is available for the predictor.

```python
class WorkerConfig:
    concurrency: int = 1
    poll_interval_sec: int = 5
    lease_duration_sec: int = 60
    recovery_max_age_sec: int = 120
    heartbeat_interval_sec: int = 30
    max_poll_backoff_sec: int = 60
    non_interactive: bool = False

class TaskWorker:
    def __init__(
        self,
        repository: JobRepository,
        run_service: RunService,
        config: WorkerConfig | None = None,
    )
```

**Public methods:**
- `async start() -> None` -- Recover orphans, start heartbeat, enter poll loop. Blocks until stopped.
- `async stop() -> None` -- Signal graceful shutdown, cancel heartbeat, await in-flight jobs, cancel poll loop.

**Internal methods:**
- `async _recover_orphan_jobs() -> list[str]` -- Return orphaned jobs to `QUEUED` or mark `FAILED`.
- `async _recover_pending_tickets() -> list[str]` -- Expire timed-out tickets, handle orphaned pending tickets.
- `async _poll_loop() -> None` -- Continuous polling with exponential backoff on empty queues.
- `async _poll_and_execute() -> bool` -- One poll iteration: list queued, acquire lease, spawn task.
- `async _execute_job_with_semaphore(job_id: str) -> None`
- `async _execute_job(job_id: str) -> None`
- `async _handle_failure(job_id, error, error_category) -> None`
- `async _heartbeat() -> None` -- Periodically refresh leases for in-flight jobs.
- `_classify_error(exc) -> str` -- Map exception to error category.

Module-level entry point:
- `async run_worker(repository, run_service, config) -> None` -- Create worker, wire SIGTERM/SIGINT handlers, start.

---

### execution_factory.py -- `ExecutionFactory`

Builds the object graph for DAG execution: `IntelligentOrchestrator`, `DAGExecutionEngine`, `AgentPool`, `Guardrails`, `ToolRegistry`, `EvaluatorEngine`. Extracted from `RunService` for testability and separation of concerns.

### job_lifecycle.py -- `JobLifecycleManager`

Manages job lifecycle transitions: failure classification, retry/dead-letter decisions, approval resume/abort flows, job status queries and listing. Extracted from `RunService`.

### run_lifecycle.py -- `RunLifecycleManager`

Centralized Run status transitions: succeeded, failed, timed_out, canceled, pending_approval. Each method returns the updated `Run` so the caller can chain or return directly. Extracted from `RunService`.

### service.py -- `_write_job_result()` (inlined from deleted `job_result.py`)

Generates standardized `job_result.json` artifacts from `Job`/`Run`/summary data. Inlined as a module-level function in `service.py` (#572).

### backend_lifecycle.py -- `BackendLifecycleService`

Manages the full backend lifecycle for a job run: resolve effective backend config, setup workspace via `BackendManager`, run project hooks (`after_create`/`before_run`/`after_run`/`before_remove`), cleanup/preserve workspace. Extracted from `RunService.run_job()`.

### worker_executor.py

Worker job execution and approval polling logic. Extracted from `TaskWorker` for maintainability. Handles the `_execute_job` flow, approval polling loop, and structured JSON logging.

### worker_recovery.py

Worker recovery logic: orphan job and pending ticket recovery at startup. Extracted from `TaskWorker` for maintainability. Handles `_recover_orphan_jobs()` and `_recover_pending_tickets()`.

---

## Data Flow

```
submit_job(requirement)
    |
    v
JobRepository.create_job()  --> Job(QUEUED)
    |
    v  [TaskWorker polls]
_poll_loop()  --> acquire_lease()  --> Job(LEASED)
    |
    v
_execute_job()
    |
    +---> transition_job_status(RUNNING)
    +---> RunService.run_job(job_id)
    |         |
    |         +---> BackendManager.setup()           --> work_dir
    |         +---> _execute_plan_and_run()
    |         |         +---> _run_hooks("before_execution")  --> MemoryHook, LearningHook, ImpactHook
    |         |         +---> persist metadata (from before-hooks)
    |         |         +---> orchestrator.plan()              --> DAG
    |         |         +---> DAGExecutionEngine.execute(dag)
    |         |         +---> _run_hooks("after_execution")   --> ImpactHook verification
    |         |         +---> persist metadata (from after-hooks)
    |         +---> Evaluate summary
    |         +---> BackendManager.cleanup/preserve
    |         +---> handle_job_failure() (if failed)
    |
    v
Job(SUCCEEDED | FAILED | DEAD_LETTER)
```

**Approval flow (when guardrails returns `pending_approval`):**
```
Guardrails.check_and_execute() --> GuardrailResult(pending_approval)
    |
    +---> ApprovalRepository.create_ticket()  --> ApprovalTicket(PENDING)
    |
    v  [Human decides]
approve_ticket() --> TicketStatus.APPROVED
    |                                --> resume_after_approval()
reject_ticket() --> TicketStatus.REJECTED
    |                                --> abort_after_rejection()
```

---

## Error Codes

| Condition | Error Type | Detail |
|---|---|---|
| Job not found | `ValueError` | `"Job not found: {job_id}"` |
| Illegal status transition | `ValueError` | `"Illegal status transition: {from} -> {to}"` |
| Cannot cancel terminal job | `ValueError` | `"Cannot cancel job: already in terminal state"` |
| Cannot release non-leased job | `ValueError` | `"Cannot release lease: job is {status}"` |
| Invalid `error_category` | `ValueError` | Pydantic field validator rejection. |
| Invalid `TicketStatus` / `RunStatus` / `JobStatus` | `ValueError` | Pydantic field validator rejection. |
| Invalid `risk_level` | `ValueError` | `"Invalid risk_level: {v!r}"` |
| Ticket expired at decision time | `ValueError` | `"Cannot approve/reject ticket: ticket expired"` |
| Ticket not in PENDING status | `ValueError` | `"Cannot approve/reject ticket: status is {status}"` |

Error categories used in `error_category` field: `""`, `"timeout"`, `"eval_failed"`, `"tool_blocked"`, `"unknown"`, `"watchdog"`.

---

## Dependencies

| Dependency | Module | Usage |
|---|---|---|
| `LLMConfig`, `WeaveConfig` | `core.config` | LLM and Weave configuration. |
| `DAGExecutionEngine` | `core.dag_engine` | DAG execution. |
| `AgentRegistry` | `core.agent_registry` | Agent type discovery. |
| `IntelligentOrchestrator` | `orchestrator.intelligent_orchestrator` | DAG planning. |
| `AgentPool` | `agent.agent_pool` | Worker agent pool. |
| `SessionStore` | `session.store` | Event logging. |
| `ToolRegistry` | `tools.registry` | Tool execution. |
| `Guardrails`, `PersonalGuardrails`, `GuardrailPolicy`, `PermissionMode` | `guardrails.policy` | Permission enforcement. |
| `PersonalGuardrailPolicy` | `core.models` | Personal-mode policy model. |
| `EvaluatorEngine` | `evaluator.engine` | Quality gates. |
| `BackendManager` | `backend.lifecycle` | Execution backend management. |
| `ExecutionHook`, `ExecutionContext`, `MemoryHook`, `LearningHook`, `ImpactHook` | `control_plane.hooks` | Execution lifecycle hooks. |
| `ExecutionFactory` | `control_plane.execution_factory` | Builds DAG execution object graph. |
| `JobLifecycleManager` | `control_plane.job_lifecycle` | Job failure/retry/approval handling. |
| `RunLifecycleManager` | `control_plane.run_lifecycle` | Run status transitions. |
| `_write_job_result` | `control_plane.service` | Job result artifact generation (inlined). |
| `BackendLifecycleService` | `control_plane.backend_lifecycle` | Backend setup/cleanup lifecycle. |
| `worker_executor`, `worker_recovery` | `control_plane.worker_executor`, `control_plane.worker_recovery` | Worker execution and recovery logic. |
| `pydantic` | External | All data models. |

---

## Configuration

| Component | Parameter | Default |
|---|---|---|
| `JobRepository` | `base_path` | `"./data/jobs"` |
| `ApprovalRepository` | `base_path` | `"./data/approvals"` |
| `RunService` | `max_parallel` | 3 |
| `RunService` | `agent_timeout` | 120 |
| `RunService` | `max_context_tokens` | 100000 |
| `RunService` | `artifact_path` | `"./data/artifacts"` |
| `RunService` | `event_store_path` | `"./data/events"` |
| `RunService` | `max_iterations` | 50 |
| `RunService` | `default_backend` | `"local"` |
| `RunService` | `approval_timeout_sec` | 300 |
| `WorkerConfig` | `concurrency` | 1 |
| `WorkerConfig` | `poll_interval_sec` | 5 |
| `WorkerConfig` | `lease_duration_sec` | 60 |
| `WorkerConfig` | `heartbeat_interval_sec` | 30 |

---

## Extension Points

1. **Custom retry policies**: Pass a `RetryPolicy` with different `max_attempts` / `backoff_sec` per job.
2. **Alternative storage**: Subclass `JobRepository` or `ApprovalRepository` to use a database instead of JSON files.
3. **Additional error categories**: Add values to the `error_category` validator in `Job`.
4. **Worker scaling**: Increase `WorkerConfig.concurrency` or run multiple `TaskWorker` processes.
5. **Approval automation**: Call `approve_ticket`/`reject_ticket` programmatically instead of manually.
6. **Custom execution hooks**: Extend `ExecutionHook` and register in `RunService._register_hooks()`. Hooks receive per-job `ExecutionContext` and can read/write `ctx.metadata` (persisted to job) and `ctx._state` (internal).

---

## Invariants

1. All repository writes are atomic (temp-file + `os.replace`); readers never see partial state.
2. Job status transitions are validated against `_VALID_TRANSITIONS`; illegal transitions raise `ValueError`.
3. Terminal job states (`SUCCEEDED`, `CANCELED`, `DEAD_LETTER`, `FAILED` once exhausted) have no outbound transitions.
4. `handle_job_failure` always transitions `FAILED -> QUEUED` (retry) or `FAILED -> DEAD_LETTER` (exhausted); the decision is based on `attempt < max_attempts`.
5. `TaskWorker` assigns a unique `owner` ID (`hostname-uuid`) to prevent lease collisions across workers.
6. `ApprovalTicket.args_hash` is a SHA-256 truncation used for tamper detection; `verify_args` checks that arguments have not changed since ticket creation.
7. Lease durations are in UTC; callers must ensure clock accuracy.
8. `_recover_orphan_jobs` and `_recover_pending_tickets` are idempotent -- safe to call at every worker startup.
9. `run_job` always transitions through `QUEUED -> LEASED -> RUNNING` before execution begins.
