# SPEC: core/models.py (Re-export Hub)

## Purpose

Single source of truth for all data models in the Weave. `core/models.py` is a re-export hub that aggregates all models from domain-specific sub-modules. No other module defines data models. All `from core.models import X` statements continue to work unchanged.

**Domain modules:**

| Module | Domain | Key Types |
|--------|--------|-----------|
| `core/dag_models.py` | DAG execution | `DAG`, `DAGNode`, `DAGEdge`, `AgentCapability`, `NodeStatus`, `NodeHealth`, `ExecutionEvent`, `FailureDecision`, `OrchestratorPlan`, `HandoffArtifact`, `DependencyType`, `NodeWorkspace*`, `FileAccessPolicy`, `FileOwnershipContract`, `ConflictResolution` |
| `core/event_models.py` | Events & session | `Event`, `EventType`, `SessionMetrics`, `SessionState` |
| `core/guardrail_models.py` | Guardrails | `RiskLevel`, `PermissionMode`, `GuardrailPolicy`, `PersonalGuardrailPolicy` |
| `core/memory_models.py` | Agent memory | `MemoryEntry`, `MemoryScope`, `MemoryType`, `LearningInsight`, `LearningCategory`, `InsightType` |
| `core/analysis_models.py` | Analysis | `DAGTemplate`, `ImpactRiskLevel`, `ImpactScope`, `VerificationResult` |
| `core/eval_models.py` | Evaluation | `EvaluationResult`, `EvalStatus`, `SuccessCriterion`, `CriterionType` |
| `core/tool_models.py` | Tool & messages | `ToolCall`, `ToolResult`, `AgentMessage` |
| `core/mcp_models.py` | MCP & skills | `MCPServerStatus`, `MCPToolInfo`, `Skill`, `SkillVariable` |
| `core/artifact_handoff.py` | Artifact service | `ArtifactHandoffService` (collect/structure handoff artifacts between DAG nodes) |
| `core/exceptions.py` | Core exceptions | `PendingApprovalError` (fault tolerance contract) |

## Public Interfaces

### Enums

| Enum | Module | Values | Description |
|------|--------|--------|-------------|
| `NodeStatus(str, Enum)` | `dag_models` | `PENDING`, `RUNNING`, `SUCCESS`, `PARTIAL_PASS`, `WARNED`, `FAILED`, `SKIPPED`, `RETRYING`, `PENDING_APPROVAL` | Execution status of a DAG node |
| `NodeHealth(str, Enum)` | `dag_models` | `HEALTHY`, `MISSED`, `UNHEALTHY`, `DEAD` | Health status of a running DAG node (heartbeat protocol) |
| `DependencyType(str, Enum)` | `dag_models` | `HARD`, `SOFT` | Edge dependency semantics (HARD: upstream fail -> downstream skip; SOFT: upstream fail -> downstream continues with warning) |
| `NodeWorkspaceStrategy(str, Enum)` | `dag_models` | `SHARED`, `WORKTREE`, `COPY` | Workspace isolation strategy for a DAG node |
| `FileAccessPolicy(str, Enum)` | `dag_models` | `OWNED`, `FORBIDDEN`, `SHARED` | File access classification for ownership contracts |
| `ConflictResolution(str, Enum)` | `dag_models` | `SERIALIZE`, `MERGE_NODE`, `ERROR`, `REASSIGN` | How to resolve parallel write conflicts |
| `EventType(str, Enum)` | `event_models` | `USER_MESSAGE`, `USER_COMMAND`, `AGENT_MESSAGE`, `AGENT_TOOL_USE`, `AGENT_TOOL_RESULT`, `AGENT_ERROR`, `SESSION_START`, `SESSION_DAG`, `SESSION_IDLE`, `SESSION_RUNNING`, `SESSION_ERROR`, `SESSION_END`, `WORKFLOW_STAGE_START`, `WORKFLOW_STAGE_END`, `WORKFLOW_STAGE_ERROR`, `TOOL_EXEC_START`, `TOOL_EXEC_END`, `TOOL_EXEC_ERROR`, `EVAL_START`, `EVAL_RESULT`, `EVAL_CONTRACT_CHECK`, `EVAL_AUTOFIX_APPLIED` | Event types following `{domain}.{action}` convention |
| `RiskLevel(int, Enum)` | `guardrail_models` | `LOW=1`, `MEDIUM=2`, `HIGH=3`, `CRITICAL=4` | Risk classification for operations |
| `PermissionMode(str, Enum)` | `guardrail_models` | `PLAN`, `DEFAULT`, `ACCEPT_EDITS`, `AUTO`, `DONT_ASK` | Guardrail permission modes |
| `MemoryScope(str, Enum)` | `memory_models` | `PRIVATE`, `SESSION`, `GLOBAL` | Visibility scope of a memory entry |
| `MemoryType(str, Enum)` | `memory_models` | `FACT`, `EXPERIENCE`, `PREFERENCE`, `CONTEXT` | Classification of memory content |
| `LearningCategory(str, Enum)` | `memory_models` | `PLANNING`, `EXECUTION`, `EVALUATION`, `AGENT_SELECTION` | Category of a learning insight |
| `InsightType(str, Enum)` | `memory_models` | `PATTERN`, `RECOMMENDATION`, `ANTI_PATTERN` | Type of learning insight |
| `DAGTemplate` | `analysis_models` | (model) | Reusable DAG template with variable substitution |
| `ImpactRiskLevel(str, Enum)` | `analysis_models` | `LOW`, `MEDIUM`, `HIGH`, `CRITICAL` | Risk level of predicted impact scope |
| `EvalStatus(str, Enum)` | `eval_models` | `CLEAN_PASS`, `PARTIAL_PASS`, `WARNED`, `FAILED` | Evaluation result status |
| `CriterionType(str, Enum)` | `eval_models` | `TESTS_PASS`, `LINT`, `FILE_EXISTS`, `FILE_PATTERN`, `COVERAGE`, `NO_CRITICAL`, `FILE_CHANGED`, `PATTERN_ABSENT`, `PATTERN_PRESENT`, `TEST_FILE_EXISTS`, `CUSTOM` | Structured criterion types for evaluator dispatch |
| `MCPServerStatus(str, Enum)` | `mcp_models` | `DISCONNECTED`, `CONNECTING`, `CONNECTED`, `ERROR` | Lifecycle status of an MCP server connection |

### Models

#### `AgentCapability(BaseModel)` (dag_models)
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

#### `NodeWorkspace(BaseModel)` (dag_models)
Workspace information for a DAG node.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `node_id` | `str` | required | Node identifier |
| `strategy` | `NodeWorkspaceStrategy` | `SHARED` | Workspace isolation strategy |
| `base_path` | `str` | `""` | The run's shared work_dir |
| `workspace_path` | `str` | `""` | This node's isolated workspace |
| `baseline_commit` | `str` | `""` | Git commit SHA at workspace creation |

#### `NodeWorkspaceResult(BaseModel)` (dag_models)
Result of node execution in its workspace.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `node_id` | `str` | required | Node identifier |
| `changed_files` | `list[str]` | `[]` | Files changed in this workspace |
| `patch_content` | `str` | `""` | Unified diff patch |
| `merge_status` | `Literal["pending", "merged", "conflict"]` | `"pending"` | Merge status |
| `conflicts` | `list[str]` | `[]` | Files with conflicts |

#### `FileOwnershipContract(BaseModel)` (dag_models)
Declares which files a DAG node intends to create or modify.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `node_id` | `str` | required | Node identifier |
| `owned_files` | `list[str]` | `[]` | Files this node exclusively creates |
| `forbidden_files` | `list[str]` | `[]` | Files owned by other nodes |
| `shared_files` | `list[str]` | `[]` | Files with coordinated access |
| `access_policy` | `dict[str, FileAccessPolicy]` | `{}` | Per-file access classification |

#### `DAGNode(BaseModel)` (dag_models)
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
| `success_criteria` | `list[str \| SuccessCriterion]` | `[]` | Criteria for evaluation (accepts mixed str/structured) |
| `eval_feedback` | `str` | `""` | Evaluator feedback for retry |
| `auto_eval_result` | `dict[str, Any] \| None` | `None` | Auto-eval result for downstream agents |
| `max_retries` | `int` | `3` | Max retry attempts |
| `retry_count` | `int` | `0` | Current retry count |
| `workspace_strategy` | `NodeWorkspaceStrategy` | `SHARED` | Workspace isolation strategy |
| `owned_files` | `list[str]` | `[]` | Files this node exclusively creates |
| `started_at` | `datetime \| None` | `None` | Start timestamp |
| `completed_at` | `datetime \| None` | `None` | Completion timestamp |
| `health_status` | `NodeHealth` | `HEALTHY` | Current health (heartbeat) |
| `last_heartbeat_at` | `datetime \| None` | `None` | Last heartbeat timestamp |
| `heartbeat_count` | `int` | `0` | Total heartbeats received |
| `missed_heartbeats` | `int` | `0` | Consecutive missed beats |

Methods:
- `record_heartbeat() -> None` -- Records a heartbeat, resets missed count, recovers health.
- `check_health(heartbeat_interval_sec: float = 5.0, miss_threshold: int = 3) -> NodeHealth` -- Returns current health based on elapsed time since last heartbeat.
- `model_post_init(__context: Any) -> None` -- Auto-generates `id` if empty.

Field validators:
- `_normalize_criteria` -- Accepts `list[str]`, `list[dict]`, or `list[SuccessCriterion]`. Dicts with a recognized `type` key are parsed into `SuccessCriterion`; unrecognized types are downgraded to `CUSTOM`.

#### `DAGEdge(BaseModel)` (dag_models)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `from_node` | `str` | required | Source node ID |
| `to_node` | `str` | required | Target node ID |
| `dependency_type` | `DependencyType` | `HARD` | Edge dependency semantics |

#### `DAG(BaseModel)` (dag_models)
Directed Acyclic Graph representing an execution plan.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `nodes` | `dict[str, DAGNode]` | `{}` | Nodes keyed by ID |
| `edges` | `list[DAGEdge]` | `[]` | Directed edges |
| `reasoning` | `str` | `""` | Orchestrator's reasoning |

Methods:
- `add_node(node: DAGNode) -> None`
- `add_edge(from_id: str, to_id: str, dependency_type: DependencyType = HARD) -> None`
- `get_dependencies(node_id: str) -> list[str]` -- All predecessor node IDs.
- `get_hard_dependencies(node_id: str) -> list[str]` -- Predecessors connected by HARD edges.
- `get_soft_dependencies(node_id: str) -> list[str]` -- Predecessors connected by SOFT edges.
- `get_dependents(node_id: str) -> list[str]` -- Successor node IDs.
- `topological_levels() -> list[list[str]]` -- Returns nodes grouped by parallel execution level. Raises `ValueError` on cycle detection.
- `get_ready_nodes() -> list[str]` -- Nodes whose hard deps are terminal-success and soft deps are terminal.

Terminal state sets:
- `_TERMINAL_STATES`: `SUCCESS`, `PARTIAL_PASS`, `WARNED`, `FAILED`, `SKIPPED`
- `_TERMINAL_SUCCESS_STATES`: `SUCCESS`, `PARTIAL_PASS`, `WARNED`

#### `ExecutionEvent(BaseModel)` (dag_models)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `timestamp` | `datetime` | `datetime.now(UTC)` | Event time |
| `node_id` | `str` | required | Related node |
| `event_type` | `Literal[...]` | required | One of: `started`, `completed`, `failed`, `retrying`, `skipped`, `heartbeat`, `heartbeat_missed`, `unhealthy_killed`, `health_recovered`, `health_alert`, `failure_decision`, `upstream_retry` |
| `details` | `dict[str, Any]` | `{}` | Event payload |

#### `FailureDecision(BaseModel)` (dag_models)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `action` | `Literal["retry", "skip", "abort", "replan"]` | required | Decision action |
| `reasoning` | `str` | `""` | Why this decision |
| `modifications` | `dict[str, Any]` | `{}` | DAG modifications for replan |

#### `OrchestratorPlan(BaseModel)` (dag_models)

| Field | Type | Description |
|-------|------|-------------|
| `reasoning` | `str` | Planning rationale |
| `nodes` | `list[dict[str, Any]]` | Node definitions |
| `edges` | `list[dict[str, str]]` | Edge definitions |

#### `HandoffArtifact(BaseModel)` (dag_models)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `from_agent` | `str` | required | Source agent type |
| `to_agent` | `str` | required | Target agent type |
| `content` | `str` | `""` | Human-readable summary |
| `file_paths` | `list[str]` | `[]` | Generated file paths |
| `metadata` | `dict[str, Any]` | `{}` | Structured data |
| `created_at` | `datetime` | `datetime.utcnow` | Creation timestamp |

#### `Event(BaseModel)` (event_models)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `id` | `str` | `uuid4()` | Event ID |
| `timestamp` | `datetime` | `datetime.utcnow` | Event time |
| `type` | `EventType` | required | Event type enum |
| `session_id` | `str` | required | Session identifier |
| `payload` | `dict[str, Any]` | `{}` | Event data |
| `metadata` | `dict[str, Any]` | `{}` | Event metadata |

#### `SessionMetrics(BaseModel)` (event_models)

| Field | Type | Default |
|-------|------|---------|
| `total_events` | `int` | `0` |
| `total_tool_calls` | `int` | `0` |
| `total_tokens_input` | `int` | `0` |
| `total_tokens_output` | `int` | `0` |
| `total_duration_ms` | `int` | `0` |
| `stage_durations` | `dict[str, int]` | `{}` |
| `errors` | `list[str]` | `[]` |

#### `SessionState(BaseModel)` (event_models)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `session_id` | `str` | required | Session identifier |
| `created_at` | `datetime` | required | Creation time |
| `status` | `Literal["created", "running", "idle", "error", "completed"]` | `"created"` | Session status |
| `current_stage` | `str \| None` | `None` | Current stage |
| `stages_completed` | `list[str]` | `[]` | Completed stages |
| `artifacts` | `dict[str, str]` | `{}` | Artifact map |
| `context_window` | `list[AgentMessage]` | `[]` | Message history |
| `metrics` | `SessionMetrics` | `SessionMetrics()` | Runtime metrics |

#### `GuardrailPolicy(BaseModel)` (guardrail_models)

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

#### `PersonalGuardrailPolicy(GuardrailPolicy)` (guardrail_models)

Inherits all `GuardrailPolicy` fields, adds:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `whitelist_patterns` | `list[str]` | `[]` | Command prefix/regex whitelist |
| `whitelist_commands` | `list[str]` | `[]` | Auto-approved commands |
| `auto_approve_high` | `bool` | `False` | Auto-approve HIGH risk |
| `confirmation_timeout_sec` | `int` | `300` | Confirmation timeout |

#### `MemoryEntry(BaseModel)` (memory_models)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `id` | `str` | auto `mem_{uuid12}` | Memory entry ID |
| `agent_type` | `str` | required | Owning agent |
| `scope` | `MemoryScope` | `PRIVATE` | Visibility scope |
| `memory_type` | `MemoryType` | `FACT` | Content classification |
| `content` | `str` | required | Memory content |
| `keywords` | `list[str]` | `[]` | Search keywords |
| `session_id` | `str \| None` | `None` | Session origin |
| `source_node_id` | `str \| None` | `None` | Source DAG node |
| `access_count` | `int` | `0` | Read count |
| `relevance_score` | `float` | `1.0` | Relevance score |
| `created_at` | `datetime` | UTC now | Creation time |
| `last_accessed_at` | `datetime` | UTC now | Last access time |
| `expires_at` | `datetime \| None` | `None` | Expiration time |
| `metadata` | `dict[str, Any]` | `{}` | Extra data |

#### `LearningInsight(BaseModel)` (memory_models)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `id` | `str` | auto `ins_{uuid12}` | Insight ID |
| `category` | `LearningCategory` | required | Insight category |
| `insight_type` | `InsightType` | required | Pattern/recommendation/anti-pattern |
| `description` | `str` | required | Insight description |
| `evidence` | `dict[str, Any]` | `{}` | Supporting evidence |
| `confidence` | `float` | `1.0` | 0.0-1.0 confidence |
| `impact` | `Literal["low", "medium", "high"]` | `"medium"` | Impact level |
| `applies_to` | `list[str]` | `[]` | Applicable agent types |
| `created_at` | `datetime` | UTC now | Creation time |
| `metadata` | `dict[str, Any]` | `{}` | Extra data |

#### `ImpactScope(BaseModel)` (analysis_models)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `id` | `str` | auto `imp_{uuid12}` | Scope ID |
| `requirement` | `str` | required | User requirement |
| `predicted_files` | `list[str]` | `[]` | Predicted affected files |
| `predicted_modules` | `list[str]` | `[]` | Predicted affected modules |
| `risk_level` | `ImpactRiskLevel` | `MEDIUM` | Risk level |
| `confidence` | `float` | `0.0` | Prediction confidence |
| `reasoning` | `str` | `""` | Prediction reasoning |
| `metadata` | `dict[str, Any]` | `{}` | Extra data |
| `created_at` | `datetime` | UTC now | Creation time |

#### `VerificationResult(BaseModel)` (analysis_models)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `impact_scope_id` | `str` | required | Associated scope ID |
| `expected_files` | `list[str]` | `[]` | Predicted files |
| `actual_changed_files` | `list[str]` | `[]` | Actual changed files |
| `covered_files` | `list[str]` | `[]` | Correctly predicted files |
| `unexpected_files` | `list[str]` | `[]` | Unpredicted changes |
| `missed_files` | `list[str]` | `[]` | Predicted but unchanged |
| `coverage` | `float` | `0.0` | Prediction coverage |
| `prediction_accuracy` | `float` | `0.0` | Accuracy score |
| `passes` | `bool` | `False` | Whether prediction was adequate |
| `notes` | `str` | `""` | Additional notes |
| `created_at` | `datetime` | UTC now | Creation time |

#### `EvaluationResult(BaseModel)` (eval_models)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `passed` | `bool` | required | Overall pass/fail |
| `score` | `float` | `0.0` | Numeric score |
| `criteria_results` | `dict[str, bool]` | `{}` | Per-criterion results |
| `feedback` | `str` | `""` | Evaluation feedback |
| `suggestions` | `list[str]` | `[]` | Improvement suggestions |
| `metadata` | `dict[str, Any]` | `{}` | Extra data |
| `eval_status` | `EvalStatus` | `CLEAN_PASS` | Structured status (clean pass / partial / warned / failed) |

#### `SuccessCriterion(BaseModel)` (eval_models)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `type` | `CriterionType` | `CUSTOM` | Criterion type for dispatch |
| `test_path` | `str` | `""` | Path for test criteria |
| `path` | `str` | `""` | Path for file criteria |
| `pattern` | `str` | `""` | Pattern for pattern criteria |
| `target` | `float \| None` | `None` | Numeric target (e.g., coverage %) |
| `description` | `str` | `""` | Human-readable description |

#### `ToolCall(BaseModel)` (tool_models)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `id` | `str` | `uuid4()` | Call ID |
| `name` | `str` | required | Tool name |
| `arguments` | `dict[str, Any]` | `{}` | Tool arguments |

#### `ToolResult(BaseModel)` (tool_models)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `tool_call_id` | `str` | required | Corresponding `ToolCall.id` |
| `success` | `bool` | required | Whether execution succeeded |
| `output` | `str` | `""` | Output text |
| `error` | `str` | `""` | Error text |
| `duration_ms` | `int` | `0` | Execution duration |

#### `AgentMessage(BaseModel)` (tool_models)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `role` | `Literal["system", "user", "assistant", "tool"]` | required | Message role |
| `content` | `str` | required | Message text |
| `tool_calls` | `list[ToolCall]` | `[]` | Tool invocations |
| `tool_call_id` | `str \| None` | `None` | For role=tool responses |

#### `Skill(BaseModel)` (mcp_models)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | `str` | required | Skill name |
| `description` | `str` | required | What the skill does |
| `prompt` | `str` | required | Prompt template |
| `variables` | `dict[str, SkillVariable]` | `{}` | Variable definitions |
| `agent_types` | `list[str]` | `[]` | Applicable agent types (empty = all) |
| `tool_allowlist` | `list[str]` | `[]` | Allowed tools |
| `context_files` | `list[str]` | `[]` | Context file paths |
| `version` | `str` | `"1.0"` | Skill version |

#### `SkillVariable(BaseModel)` (mcp_models)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `default` | `str` | `""` | Default value |
| `description` | `str` | `""` | Variable description |
| `required` | `bool` | `False` | Whether required |

#### `MCPToolInfo(BaseModel)` (mcp_models)

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `prefixed_name` | `str` | required | e.g. `mcp__github__create_issue` |
| `original_name` | `str` | required | e.g. `create_issue` |
| `server_name` | `str` | required | e.g. `github` |
| `description` | `str` | `""` | Tool description |
| `input_schema` | `dict[str, Any]` | `{}` | JSON schema |

#### `PendingApprovalError(Exception)` (exceptions)
Raised when a tool call requires human approval before execution. Propagation chain: `WorkerAgent._execute_tool()` -> `Guardrails.check_and_execute()` -> `DAGEngine._execute_single_node()` -> `RunService.run_job()` -> `Worker._execute_job()`.

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
  -> MemoryEntry / LearningInsight for persistent learning
  -> Skill / MCPToolInfo for MCP tool integration
```

## Error Codes

No numeric error codes. Error states expressed through:
- `NodeStatus.FAILED` -- Node-level failure with `DAGNode.error` string.
- `NodeHealth.UNHEALTHY` / `NodeHealth.DEAD` -- Watchdog-killed nodes.
- `ValueError("Cycle detected in DAG")` -- From `DAG.topological_levels()`.
- `ValueError("Cannot unregister protected agent: {id}")` -- From registry unregistration.
- `PendingApprovalError` -- Tool requires human approval.

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
- **New criterion types**: Add to `CriterionType` enum and implement a corresponding checker in `evaluator/checkers/`.
- **New memory types**: Add to `MemoryType` enum.
- **New skill variables**: Extend `SkillVariable` for additional metadata.

## Invariants

1. All data models MUST be Pydantic `BaseModel` subclasses with `model_dump()` serialization.
2. `DAG.topological_levels()` MUST raise `ValueError` on cycle detection.
3. `DAGNode.id` is auto-generated (format `node_{uuid8}`) if not provided.
4. `EventType` values follow `{domain}.{action}` naming convention.
5. `RiskLevel` is an `int` enum ordered LOW(1) < MEDIUM(2) < HIGH(3) < CRITICAL(4).
6. Protected agents (`planner`, `generator`, `evaluator`) cannot be unregistered.
7. All `datetime` fields default to UTC.
8. Mutable defaults use `Field(default_factory=...)` to avoid shared state between instances.
9. `core/models.py` re-exports all types -- `from core.models import X` always works.
10. New domain modules should be added to both `core/models.py` re-exports and this spec.
