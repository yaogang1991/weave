# SPEC: core/dag_engine.py

## Purpose

Executes a `DAG` plan by computing topological levels, running nodes at each level in parallel (bounded by `max_parallel`), handling failures via orchestrator callback, and supporting true replanning with a configurable limit. Includes a watchdog subsystem that monitors node health via heartbeats and kills unresponsive nodes.

## Public Interfaces

### Type Aliases

```python
EventHandler = Callable[[ExecutionEvent], Coroutine[Any, Any, None]]
ReplanHandler = Callable[[DAG, str], Coroutine[Any, Any, DAG]]
```

### `DAGExecutionEngine`

```python
class DAGExecutionEngine:
    def __init__(
        self,
        agent_executor: Callable[[DAGNode, list[HandoffArtifact]], Coroutine[Any, Any, dict]],
        failure_handler: Callable[[DAG, str, str], Coroutine[Any, Any, FailureDecision]],
        replan_handler: ReplanHandler | None = None,
        max_replans: int = 3,
        max_parallel: int = 5,
        evaluator: Any | None = None,
        artifact_path: str = "./data/artifacts",
        job_timeout: float | None = None,
        heartbeat_interval_sec: float = 5.0,
        heartbeat_miss_threshold: int = 3,
        enable_watchdog: bool = True,
    ) -> None
    def on_event(self, handler: EventHandler) -> None
    async def execute(self, dag: DAG) -> DAG
    def get_execution_summary(self, dag: DAG) -> dict[str, Any]
```

#### Constructor Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `agent_executor` | `Callable[[DAGNode, list[HandoffArtifact]], Coroutine[...dict]]` | required | Async function to execute a single node |
| `failure_handler` | `Callable[[DAG, str, str], Coroutine[...FailureDecision]]` | required | Async callback for failure decisions: `(dag, node_id, error) -> FailureDecision` |
| `replan_handler` | `ReplanHandler | None` | `None` | Async callback for replanning: `(dag, failed_node_id) -> new DAG` |
| `max_replans` | `int` | `3` | Maximum replan attempts before abort |
| `max_parallel` | `int` | `5` | Max concurrent node executions per level |
| `evaluator` | `Any | None` | `None` | Evaluator object with `evaluate_stage()` method |
| `artifact_path` | `str` | `"./data/artifacts"` | Path for artifact resolution |
| `job_timeout` | `float | None` | `None` | Overall job timeout (currently unused in execute) |
| `heartbeat_interval_sec` | `float` | `5.0` | Seconds between heartbeat checks |
| `heartbeat_miss_threshold` | `int` | `3` | Missed beats before node is killed |
| `enable_watchdog` | `bool` | `True` | Whether to run the watchdog |

#### Public Methods

**`on_event(handler: EventHandler) -> None`**
Registers an event handler. Multiple handlers supported. Handlers are called for every `ExecutionEvent` emitted during execution. Exceptions in handlers are logged but do not break execution.

**`async execute(dag: DAG) -> DAG`**
Executes the full DAG. Returns the same `DAG` object with all node statuses and results populated.

Flow:
1. Compute topological levels via `dag.topological_levels()`. Raises `ValueError` if cycle detected.
2. Start watchdog background task.
3. For each level, execute nodes in parallel (bounded by `max_parallel` semaphore).
4. After each level, check for failed nodes and delegate to `failure_handler`.
5. Handle `FailureDecision.action`:
   - `"abort"` -- Skip all remaining nodes, return DAG.
   - `"retry"` -- Retry with exponential backoff. If still failed, skip remaining.
   - `"skip"` -- Mark node as `SKIPPED`, continue.
   - `"replan"` -- Call `replan_handler`, merge results, reset to level 0 with recomputed levels. Enforces `max_replans`.
6. Stop watchdog in `finally` block.

**`get_execution_summary(dag: DAG) -> dict[str, Any]`**
Returns execution summary dict:
```python
{
    "total_nodes": int,
    "success": int,
    "failed": int,
    "skipped": int,
    "all_succeeded": bool,
    "node_details": {
        node_id: {
            "status": str,     # NodeStatus value
            "agent": str,      # agent_type
            "duration_ms": float | None,
        }
    }
}
```

#### Internal Methods

**`async _emit(event: ExecutionEvent) -> None`**
Calls all registered event handlers. Catches and logs handler exceptions.

**`_skip_remaining(dag: DAG, levels: list[list[str]], from_level: int) -> None`**
Marks all `PENDING` nodes from `from_level` onward as `SKIPPED`.

**`_merge_dag_results(old_dag: DAG, new_dag: DAG) -> DAG`**
After replan, copies `status`, `result`, `output_artifacts`, `started_at`, `completed_at` from `SUCCESS` nodes in `old_dag` to matching nodes in `new_dag` to avoid re-execution.

**`_compute_backoff(retry_count: int) -> float`**
Returns `min(2 ** retry_count, 60.0)` seconds. Capped at 60s.

**`async _execute_single_node(dag: DAG, node_id: str) -> None`**
Executes one node:
1. Skips if already `SUCCESS`, or not `PENDING`/`RETRYING`.
2. Collects input `HandoffArtifact` from dependency nodes.
3. Sets status to `RUNNING`, registers with watchdog.
4. Emits `"started"` event.
5. Calls `_execute_with_heartbeat()`.
6. If evaluator is configured and node has `success_criteria`, runs evaluation gate.
7. On success: sets `SUCCESS`, populates `result` and `output_artifacts`.
8. On `CancelledError`: checks if watchdog-killed (`DEAD` state); swallows if so, re-raises otherwise.
9. On `Exception`: increments `retry_count`. If under `max_retries`, sets `RETRYING` and recurses with backoff. Otherwise sets `FAILED`.
10. Unregisters from watchdog in `finally` (unless killed by watchdog).

**`async _execute_with_heartbeat(node: DAGNode, input_artifacts: list[HandoffArtifact]) -> dict[str, Any]`**
Wraps `agent_executor` with heartbeat polling:
- Uses `asyncio.wait_for(asyncio.shield(task), timeout=heartbeat_interval)` to poll.
- On `TimeoutError`: records heartbeat, continues polling.
- On `CancelledledError`: cancels inner task and waits for cleanup, then re-raises.

**`_collect_input_artifacts(dag: DAG, node_id: str) -> list[HandoffArtifact]`**
Collects artifacts from all `SUCCESS` dependency nodes. Also includes `eval_feedback` from previous attempt if present.

### Watchdog Subsystem

**`_start_watchdog() -> None`**
Creates `asyncio.Task` for `_watchdog_loop()` if `enable_watchdog` is `True`.

**`_stop_watchdog() -> None`**
Cancels and clears the watchdog task.

**`async _watchdog_loop() -> None`**
Background coroutine:
1. Sleeps for `heartbeat_interval_sec`.
2. Iterates all `_running_nodes`.
3. Calls `node.check_health()`.
4. On `MISSED`: emits `"heartbeat_missed"` event.
5. On `UNHEALTHY`: emits `"unhealthy_killed"`, marks node `DEAD`/`FAILED`, cancels the node's asyncio task, removes from tracking, emits `"health_alert"`.

## Data Flow

```
DAG (from Orchestrator)
  -> execute() -> topological_levels()
  -> For each level:
     -> Semaphore(max_parallel) bounds concurrency
     -> _execute_single_node() per node
        -> _collect_input_artifacts() from dependency nodes
        -> _execute_with_heartbeat() wraps agent_executor
        -> Optional evaluator.evaluate_stage() gate
        -> Emits ExecutionEvent at each state transition
     -> asyncio.gather() for level
  -> On failure:
     -> failure_handler() -> FailureDecision
     -> retry / skip / abort / replan
  -> On replan:
     -> replan_handler() -> new DAG
     -> _merge_dag_results() preserves completed work
     -> Restart from level 0 with new topological_levels()
  -> get_execution_summary() for final report
```

## Error Codes

No numeric error codes. Error handling:

| Condition | Behavior |
|-----------|----------|
| Cycle in DAG | `execute()` raises `ValueError(f"Invalid DAG: {e}")` |
| Node execution exception | Caught, formatted with traceback, stored in `DAGNode.error` |
| Node retries exhausted | `DAGNode.status = FAILED` |
| Watchdog kill | `DAGNode.status = FAILED`, `DAGNode.health_status = DEAD`, task cancelled |
| Event handler exception | Logged as warning, execution continues |
| Replan limit exceeded | `DAGNode.error = "Max replans reached"`, remaining nodes skipped |
| No replan handler | Treated as abort |

## Dependencies

- `core.models` -- `DAG`, `DAGNode`, `NodeStatus`, `NodeHealth`, `ExecutionEvent`, `FailureDecision`, `HandoffArtifact`
- Python stdlib: `asyncio`, `logging`, `traceback`, `datetime`, `typing`

## Configuration

Configured via constructor parameters (typically from `HarnessConfig`):

| Engine Parameter | Maps to HarnessConfig |
|-----------------|----------------------|
| `max_parallel` | CLI `--max-parallel` flag |
| `artifact_path` | `HarnessConfig.artifact_path` |
| `heartbeat_interval_sec` | Default `5.0` |
| `heartbeat_miss_threshold` | Default `3` |
| `enable_watchdog` | Default `True` |

## Extension Points

1. **Event handlers**: Register via `on_event()` for monitoring, logging, or UI updates.
2. **Custom evaluator**: Pass an object with `evaluate_stage(node_id, stage_id, criteria, artifact_path)` method to enable the evaluation gate.
3. **Replan handler**: Provide `replan_handler` callback for closed-loop plan adaptation.
4. **Failure handler**: Provide `failure_handler` callback for custom failure strategies.
5. **Watchdog tuning**: Adjust `heartbeat_interval_sec` and `heartbeat_miss_threshold` for different workloads.

## Invariants

1. `execute()` always stops the watchdog in its `finally` block, even on exceptions.
2. `_running_nodes` and `_running_tasks` are cleared on `execute()` entry and exit.
3. Replanning resets `level_idx` to `0` and recomputes topological levels from the merged DAG.
4. `max_replans` is an absolute limit; exceeding it triggers abort.
5. Watchdog-killed nodes have `health_status = DEAD` and are not retried by `_execute_single_node`.
6. Exponential backoff is capped at 60 seconds.
7. Successful nodes from the pre-replan DAG are never re-executed (preserved by `_merge_dag_results`).
8. Event handler failures never propagate to break the execution loop.
9. `_execute_with_heartbeat` uses `asyncio.shield()` so heartbeat polling timeouts do not cancel the underlying executor task.
10. The `asyncio.Semaphore` ensures at most `max_parallel` nodes execute concurrently per level.
