# SPEC: core/models.py

## Purpose

Single source of truth for all data models in the harness. Defines every Pydantic `BaseModel` and enum used across the orchestration, execution, session, guardrails, and evaluation layers. No other module defines data models.

## Public Interfaces

### Enums

| Enum | Values | Description |
|------|--------|-------------|
| `NodeStatus(str, Enum)` | `PENDING`, `RUNNING`, `SUCCESS`, `FAILED`, `SKIPPED`, `RETRYING` | Execution status of a DAG node |
| `NodeHealth(str, Enum)` | `HEALTHY`, `MISSED`, `UNHEALTHY`, `DEAD` | Health status of a running DAG node (heartbeat protocol) |
| `EventType(str, Enum)` | `USER_MESSAGE`, `USER_COMMAND`, `AGENT_MESSAGE`, `AGENT_TOOL_USE`, `AGENT_TOOL_RESULT`, `AGENT_ERROR`, `SESSION_START`, `SESSION_IDLE`, `SESSION_RUNNING`, `SESSION_ERROR`, `SESSION_END`, `WORKFLOW_STAGE_START`, `WORKFLOW_STAGE_END`, `WORKFLOW_STAGE_ERROR`, `TOOL_EXEC_START`, `TOOL_EXEC_END`, `TOOL_EXEC_ERROR`, `EVAL_START`, `EVAL_RESULT`, `EVAL_CONTRACT_CHECK`, `CHECKPOINT_CREATED`, `CHECKPOINT_RESTORED` | Event types following `{domain}.{action}` convention |
| `RiskLevel(int, Enum)` | `LOW=1`, `MEDIUM=2`, `HIGH=3`, `CRITICAL=4` | Risk classification for operations |
| `PermissionMode(str, Enum)` | `PLAN`, `DEFAULT`, `ACCEPT_EDITS`, `AUTO`, `DONT_ASK` | Guardrail permission modes |

### Models

#### `AgentCapability(BaseModel)`
Describes a worker agent's capabilities for registry and orchestrator consumption.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `id` | `str` | required | Unique agent identifier |
| `name` | `str` | required | Human-readable name |
| `description` | `str` | required | What the agent does |
| `skills` | `list[str]` | `[]` | Skill tags |
| `input_schema` | `list[str]` | `[]` | Expected input types |
| `output_schema` | `list[str]` | `[]` | Produced output types |
| `constraints` | `list[str]` | `[]` | Behavioral constraints |
| `system_prompt` | `str` | `""` | Optional custom system prompt |

#### `DAGNode(BaseModel)`
A single node in the execution DAG (one agent task).

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `id` | `str` | auto `node_{uuid8}` | Node identifier |
| `agent_type` | `str` | required | References `AgentCapability.id` |
| `task_description` | `str` | required | Task to perform |
| `status` | `NodeStatus` | `PENDING` | Current execution status |
| `result` | `dict[str, Any]` | `{}` | Execution result data |
| `error` | `str` | `""` | Error message if failed |
| `output_artifacts` | `list[str]` | `[]` | Artifact file paths |
| `success_criteria` | `list[str]` | `[]` | Criteria for evaluation |
| `eval_feedback` | `str` | `""` | Evaluator feedback for retry |
| `max_retries` | `int` | `3` | Max retry attempts |
| `retry_count` | `int` | `0` | Current retry count |
| `started_at` | `datetime | None` | `None` | Start timestamp |
| `completed_at` | `datetime | None` | `None` | Completion timestamp |
| `health_status` | `NodeHealth` | `HEALTHY` | Current health (heartbeat) |
| `last_heartbeat_at` | `datetime | None` | `None` | Last heartbeat timestamp |
| `heartbeat_count` | `int` | `0` | Total heartbeats received |
| `missed_heartbeats` | `int` | `0` | Consecutive missed beats |

Methods:
- `record_heartbeat() -> None` -- Records a heartbeat, resets missed count, recovers health.
- `check_health(heartbeat_interval_sec: float = 5.0, miss_threshold: int = 3) -> NodeHealth` -- Returns current health based on elapsed time since last heartbeat.
- `model_post_init(__context: Any) -> None` -- Auto-generates `id` if empty.

#### `DAGEdge(BaseModel)`

| Field | Type | Description |
|-------|------|-------------|
| `from_node` | `str` | Source node ID |
| `to_node` | `str` | Target node ID |

#### `DAG(BaseModel)`
Directed Acyclic Graph representing an execution plan.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `nodes` | `dict[str, DAGNode]` | `{}` | Nodes keyed by ID |
| `edges` | `list[DAGEdge]` | `[]` | Directed edges |
| `reasoning` | `str` | `""` | Orchestrator's reasoning |

Methods:
- `add_node(node: DAGNode) -> None`
- `add_edge(from_id: str, to_id: str) -> None`
- `get_dependencies(node_id: str) -> list[str]` -- Predecessor node IDs.
- `get_dependents(node_id: str) -> list[str]` -- Successor node IDs.
- `topological_levels() -> list[list[str]]` -- Returns nodes grouped by parallel execution level. Raises `ValueError` on cycle detection.
- `get_ready_nodes() -> list[str]` -- Nodes whose dependencies are all `SUCCESS`.

#### `ExecutionEvent(BaseModel)`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `timestamp` | `datetime` | `datetime.now(UTC)` | Event time |
| `node_id` | `str` | required | Related node |
| `event_type` | `Literal[...]` | required | One of: `started`, `completed`, `failed`, `retrying`, `skipped`, `heartbeat`, `heartbeat_missed`, `unhealthy_killed`, `health_recovered`, `health_alert` |
| `details` | `dict[str, Any]` | `{}` | Event payload |

#### `FailureDecision(BaseModel)`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `action` | `Literal["retry", "skip", "abort", "replan"]` | required | Decision action |
| `reasoning` | `str` | `""` | Why this decision |
| `modifications` | `dict[str, Any]` | `{}` | DAG modifications for replan |

#### `OrchestratorPlan(BaseModel)`

| Field | Type | Description |
|-------|------|-------------|
| `reasoning` | `str` | Planning rationale |
| `nodes` | `list[dict[str, Any]]` | Node definitions |
| `edges` | `list[dict[str, str]]` | Edge definitions |

#### `HandoffArtifact(BaseModel)`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `from_agent` | `str` | required | Source agent type |
| `to_agent` | `str` | required | Target agent type |
| `content` | `str` | `""` | Human-readable summary |
| `file_paths` | `list[str]` | `[]` | Generated file paths |
| `metadata` | `dict[str, Any]` | `{}` | Structured data |
| `created_at` | `datetime` | `datetime.utcnow` | Creation timestamp |

#### `Event(BaseModel)`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `id` | `str` | `uuid4()` | Event ID |
| `timestamp` | `datetime` | `datetime.utcnow` | Event time |
| `type` | `EventType` | required | Event type enum |
| `session_id` | `str` | required | Session identifier |
| `payload` | `dict[str, Any]` | `{}` | Event data |
| `metadata` | `dict[str, Any]` | `{}` | Event metadata |

#### `ToolCall(BaseModel)`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `id` | `str` | `uuid4()` | Call ID |
| `name` | `str` | required | Tool name |
| `arguments` | `dict[str, Any]` | `{}` | Tool arguments |

#### `ToolResult(BaseModel)`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `tool_call_id` | `str` | required | Corresponding `ToolCall.id` |
| `success` | `bool` | required | Whether execution succeeded |
| `output` | `str` | `""` | Output text |
| `error` | `str` | `""` | Error text |
| `duration_ms` | `int` | `0` | Execution duration |

#### `AgentMessage(BaseModel)`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `role` | `Literal["system", "user", "assistant", "tool"]` | required | Message role |
| `content` | `str` | required | Message text |
| `tool_calls` | `list[ToolCall]` | `[]` | Tool invocations |
| `tool_call_id` | `str | None` | `None` | For role=tool responses |

#### `SessionMetrics(BaseModel)`

| Field | Type | Default |
|-------|------|---------|
| `total_events` | `int` | `0` |
| `total_tool_calls` | `int` | `0` |
| `total_tokens_input` | `int` | `0` |
| `total_tokens_output` | `int` | `0` |
| `total_duration_ms` | `int` | `0` |
| `stage_durations` | `dict[str, int]` | `{}` |
| `errors` | `list[str]` | `[]` |

#### `SessionState(BaseModel)`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `session_id` | `str` | required | Session identifier |
| `created_at` | `datetime` | required | Creation time |
| `status` | `Literal["created", "running", "idle", "error", "completed"]` | `"created"` | Session status |
| `current_stage` | `str | None` | `None` | Current stage |
| `stages_completed` | `list[str]` | `[]` | Completed stages |
| `artifacts` | `dict[str, str]` | `{}` | Artifact map |
| `context_window` | `list[AgentMessage]` | `[]` | Message history |
| `metrics` | `SessionMetrics` | `SessionMetrics()` | Runtime metrics |

#### `GuardrailPolicy(BaseModel)`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `mode` | `PermissionMode` | `DEFAULT` | Permission mode |
| `allowed_tools` | `list[str]` | `[]` | Whitelisted tools |
| `denied_tools` | `list[str]` | `[]` | Blacklisted tools |
| `allowed_commands` | `list[str]` | `[]` | Allowed bash commands |
| `denied_commands` | `list[str]` | `[]` | Denied bash commands |
| `max_bash_duration` | `int` | `120` | Bash timeout (seconds) |
| `max_iterations` | `int` | `50` | Max agent loop iterations |
| `auto_approve_read` | `bool` | `True` | Auto-approve read operations |
| `require_human_on_error` | `bool` | `True` | Escalate errors to human |

#### `PersonalGuardrailPolicy(GuardrailPolicy)`

Inherits all `GuardrailPolicy` fields, adds:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `whitelist_patterns` | `list[str]` | `[]` | Command prefix/regex whitelist |
| `whitelist_commands` | `list[str]` | `[]` | Auto-approved commands |
| `auto_approve_high` | `bool` | `False` | Auto-approve HIGH risk |
| `confirmation_timeout_sec` | `int` | `300` | Confirmation timeout |

#### `EvaluationResult(BaseModel)`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `passed` | `bool` | required | Overall pass/fail |
| `score` | `float` | `0.0` | Numeric score |
| `criteria_results` | `dict[str, bool]` | `{}` | Per-criterion results |
| `feedback` | `str` | `""` | Evaluation feedback |
| `suggestions` | `list[str]` | `[]` | Improvement suggestions |

## Data Flow

```
User requirement
  -> OrchestratorPlan (nodes + edges)
  -> DAG (runtime graph with DAGNode instances)
  -> DAGExecutionEngine executes levels, emitting ExecutionEvents
  -> DAGNode.result populated, HandoffArtifacts passed between nodes
  -> EvaluationResult gates node completion
  -> FailureDecision drives retry/skip/abort/replan
  -> SessionState recovered from Event log (EventType + Event + AgentMessage)
  -> ToolCall / ToolResult for tool interactions
```

## Error Codes

No numeric error codes. Error states expressed through:
- `NodeStatus.FAILED` -- Node-level failure with `DAGNode.error` string.
- `NodeHealth.UNHEALTHY` / `NodeHealth.DEAD` -- Watchdog-killed nodes.
- `ValueError("Cycle detected in DAG")` -- From `DAG.topological_levels()`.
- `ValueError("Cannot unregister protected agent: {id}")` -- From registry unregistration.

## Dependencies

- `pydantic` (`BaseModel`, `Field`)
- Python stdlib: `uuid`, `datetime`, `enum`, `typing`

## Configuration

No direct configuration. Models receive their values from callers (orchestrator, session manager, engine). `Field(default_factory=...)` used for mutable defaults per Pydantic best practices.

## Extension Points

- **New agent types**: Create `AgentCapability` instances and register them in `AgentRegistry`.
- **New event types**: Add members to `EventType` enum following `{domain}.{action}` naming.
- **New permission modes**: Add to `PermissionMode` enum.
- **New risk levels**: Add to `RiskLevel` enum.
- **Custom guardrail policies**: Subclass `GuardrailPolicy` (see `PersonalGuardrailPolicy`).

## Invariants

1. All data models MUST be Pydantic `BaseModel` subclasses with `model_dump()` serialization.
2. `DAG.topological_levels()` MUST raise `ValueError` on cycle detection.
3. `DAGNode.id` is auto-generated (format `node_{uuid8}`) if not provided.
4. `EventType` values follow `{domain}.{action}` naming convention.
5. `RiskLevel` is an `int` enum ordered LOW(1) < MEDIUM(2) < HIGH(3) < CRITICAL(4).
6. Protected agents (`planner`, `generator`, `evaluator`) cannot be unregistered.
7. All `datetime` fields default to UTC.
8. Mutable defaults use `Field(default_factory=...)` to avoid shared state between instances.
