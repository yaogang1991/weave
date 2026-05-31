# Plan: M6.2 Node Guardrails — Permission Control at DAG Node Boundary

## Summary

在 NodeExecutor 的外部 backend 执行路径中插入 `NodeGuardrails` 类，提供确定性的 pre-check（执行前门控）和 post-check（执行后验证）。新建 `GuardrailBlockedException` 异常类，扩展 `GuardrailsConfig` 增加 `protected_paths` 字段。被阻止的节点不消耗 retry budget，发射 `guardrail_blocked` 事件用于审计。

## User Story

As a Weave developer, I want node-level safety checks before and after external backend execution, so I can trust automated changes stay within safe boundaries.

## Problem -> Solution

**Current**: M6.1 切换默认 backend 到 `claude_code` 后，Claude Code 管理自己的工具调用，Weave 看不到单个 tool call。`Guardrails.check_and_execute()` 对外部 backend 路径无任何保护。NodeExecutor 没有前置/后置检查。

**Target**: 外部 backend 执行路径有确定性的 pre-check（agent_type 门控 + workspace 路径校验 + denied_commands 匹配）和 post-check（protected_paths 检测），被阻止的任务标记 FAILED 且不重试。

## Metadata
- **Complexity**: Medium
- **Source PRD**: `.claude/PRPs/prds/m6-2-node-guardrails.prd.md`
- **PRD Phase**: All phases (1-6)
- **Estimated Files**: 7 files (3 new, 4 modified)

---

## UX Design

### Before
```
NodeExecutor._execute_with_timeout()
  -> BackendRegistry.execute_for_node(backend_name, context)
  -> (no guardrail checks)
  -> result returned
```

### After
```
NodeExecutor._execute_with_timeout()
  IF external backend:
    1. NodeGuardrails.pre_check(node, workspace_path)
       -> blocked? GuardrailBlockedException -> FAILED (no retry)
  -> BackendRegistry.execute_for_node(backend_name, context)
  -> NodeGuardrails.post_check(result, workspace_path)
       -> blocked? GuardrailBlockedException -> FAILED (no retry, result preserved)
  -> result returned
```

### Interaction Changes
| Touchpoint | Before | After | Notes |
|---|---|---|---|
| External backend execution | No guardrail checks | Pre-check + post-check | Only non-builtin backends |
| Node failure from guardrail | N/A | FAILED status, no retry | `retry_budget_preserved: true` |
| Event stream | No guardrail events | `guardrail_blocked` event | For audit/monitoring |
| Config `.weave/config.yaml` | `denied_commands` only | + `protected_paths` | Backward compatible default |

---

## Mandatory Reading

| Priority | File | Lines | Why |
|---|---|---|---|
| P0 (critical) | `core/node_executor.py` | 141-329 | Main integration point — execute_node() + _execute_with_timeout() |
| P0 (critical) | `guardrails/policy.py` | 47-88 | GuardrailResult — re-used as NodeGuardrails return type |
| P1 (important) | `guardrails/node_isolation.py` | 49-127 | NodeIsolationGuard — architectural reference for guard class |
| P1 (important) | `core/exceptions.py` | all | Exception pattern to follow for GuardrailBlockedException |
| P1 (important) | `core/project_config.py` | 57-62 | GuardrailsConfig — add protected_paths field |
| P1 (important) | `core/backend_models.py` | 20-53 | BackendResult — post_check input, `.artifacts` field |
| P2 (reference) | `control_plane/execution_factory.py` | 130-240 | Dependency injection — where NodeGuardrails is created |
| P2 (reference) | `core/dag_engine.py` | 196-220 | NodeExecutor creation — where to thread node_guardrails |

## External Documentation

No external research needed — feature uses established internal patterns (fnmatch, pathlib, pydantic BaseModel).

---

## Patterns to Mirror

### GUARDRAIL_RESULT_CLASS
// SOURCE: `guardrails/policy.py:47-88`
```python
@dataclass
class GuardrailResult:
    decision: Literal["allowed", "blocked", "pending_approval"]
    reason: str = ""
    ticket_id: str | None = None

    @property
    def is_allowed(self) -> bool:
        return self.decision == "allowed"
```

### EXCEPTION_PATTERN
// SOURCE: `core/exceptions.py:86-106`
```python
class BudgetExhaustedError(Exception):
    def __init__(
        self, used_tokens: int, budget_tokens: int, node_id: str = "",
    ) -> None:
        self.used_tokens = used_tokens
        self.budget_tokens = budget_tokens
        self.node_id = node_id
        super().__init__(
            f"Token budget exhausted: {used_tokens}/{budget_tokens} tokens used"
            f"{f' at node {node_id}' if node_id else ''}"
        )
```

### GUARD_CLASS_PATTERN
// SOURCE: `guardrails/node_isolation.py:49-127`
```python
class NodeIsolationGuard:
    def __init__(self, enabled: bool = True) -> None:
        self._enabled = enabled

    def scan_handoffs(
        self,
        artifacts: list[HandoffArtifact],
        from_node_id: str,
        to_node_id: str,
    ) -> IsolationScanResult:
        if not self._enabled or not artifacts:
            return IsolationScanResult(
                total_artifact_count=len(artifacts),
                sanitized_artifacts=list(artifacts),
            )
        # ... processing
```

### EVENT_EMISSION
// SOURCE: `core/node_executor.py:286-295`
```python
await self._emit(ExecutionEvent(
    node_id=node_id,
    event_type="failed",
    details={
        "error": str(e),
        "reason": reason,
        "retry_budget_preserved": True,
        "retry_count": dag.nodes[node_id].retry_count,
    },
))
```

### CONFIG_EXTENSION
// SOURCE: `core/project_config.py:57-62`
```python
class GuardrailsConfig(BaseModel):
    denied_commands: list[str] = Field(default_factory=list)
    approval_policy: str = "accept_edits"
```

### EXCEPTION_CATCH_NO_RETRY
// SOURCE: `core/node_executor.py:205-214`
```python
except (
    asyncio.CancelledError,
    PendingApprovalError,
    RateLimitError,
    NodeTimeoutError,
    BudgetExhaustedError,
):
    raise  # System errors -> outer catch
```

Then in outer catch (`node_executor.py:271-295`):
```python
except (RateLimitError, NodeTimeoutError) as e:
    reason = "rate_limit" if isinstance(e, RateLimitError) else "timeout"
    dag.update_node(node_id, status=NodeStatus.FAILED, ...)
    await self._emit(ExecutionEvent(..., details={"retry_budget_preserved": True, ...}))
```

### BACKEND_NAME_RESOLVE
// SOURCE: `core/node_executor.py:639`
```python
backend_name = node.backend if node.backend and node.backend != "builtin" else self._default_agent_backend
```

---

## Files to Change

| File | Action | Justification |
|---|---|---|
| `guardrails/node_guardrails.py` | CREATE | NodeGuardrails class — pre_check + post_check |
| `core/exceptions.py` | UPDATE | Add GuardrailBlockedException |
| `core/project_config.py` | UPDATE | Add `protected_paths` to GuardrailsConfig |
| `core/node_executor.py` | UPDATE | Integrate pre/post check + exception handling + event emission |
| `core/dag_engine.py` | UPDATE | Thread `node_guardrails` param to NodeExecutor |
| `control_plane/execution_factory.py` | UPDATE | Create NodeGuardrails and pass to engine |
| `tests/test_node_guardrails.py` | CREATE | Unit tests for NodeGuardrails + integration |

## NOT Building

- BuiltinBackend path guardrails changes (M6.3 scope)
- AgentPool tool-call guardrails removal (M6.3 scope)
- LLM-driven risk assessment (premature)
- `max_changed_files` post-check rule (future incremental)
- `owned_files` contract check (too noisy)
- `DAGNode.risk_level` field (agent_type derivation sufficient)

---

## Step-by-Step Tasks

### Task 1: GuardrailBlockedException
- **ACTION**: Add `GuardrailBlockedException` to `core/exceptions.py`
- **IMPLEMENT**: New exception class with `reason` and `phase` ("pre"/"post") attributes. Follow `BudgetExhaustedError` pattern.
- **MIRROR**: EXCEPTION_PATTERN
- **IMPORTS**: None needed
- **GOTCHA**: `phase` field distinguishes pre vs post check in events and logs
- **VALIDATE**: `python -c "from core.exceptions import GuardrailBlockedException; e = GuardrailBlockedException('test', phase='pre'); print(e)"`

### Task 2: GuardrailsConfig Extension
- **ACTION**: Add `protected_paths: list[str]` field to `GuardrailsConfig` in `core/project_config.py`
- **IMPLEMENT**: Add field with default list containing `.env`, `.env.*`, `credentials*`, `id_rsa`, `id_ed25519`, `.ssh/`, `.gnupg/`, `.git/config`
- **MIRROR**: CONFIG_EXTENSION
- **IMPORTS**: None new (already has `Field`)
- **GOTCHA**: Default must be via `Field(default_factory=lambda: [...])` to avoid mutable default sharing
- **VALIDATE**: `python -c "from core.project_config import GuardrailsConfig; c = GuardrailsConfig(); print(c.protected_paths)"` should print default list

### Task 3: NodeGuardrails Class
- **ACTION**: Create `guardrails/node_guardrails.py` with `NodeGuardrails` class
- **IMPLEMENT**:
  ```python
  class NodeGuardrails:
      def __init__(self, config: GuardrailsConfig, project_dir: str | None = None): ...
      def pre_check(self, node: DAGNode, workspace_path: str | None = None) -> GuardrailResult: ...
      def post_check(self, artifacts: list[str], workspace_path: str | None = None) -> GuardrailResult: ...
  ```
  - `pre_check()`: agent_type in ("planner", "evaluator") -> allowed; workspace_path outside project_dir -> blocked; denied_commands substring match on task_description.lower() -> blocked; else -> allowed
  - `post_check()`: iterate `artifacts`, match each against `protected_paths` using `fnmatch.fnmatch` on relative path; match -> blocked
  - Import `GuardrailResult` from `guardrails.policy`
- **MIRROR**: GUARD_CLASS_PATTERN + GUARDRAIL_RESULT_CLASS
- **IMPORTS**: `from guardrails.policy import GuardrailResult`, `from core.project_config import GuardrailsConfig`, `from fnmatch import fnmatch`, `from pathlib import Path`, `import logging`
- **GOTCHA**:
  - Use `fnmatch` with forward-slash normalized paths for Windows compat
  - Empty artifacts list -> return allowed (no false positives)
  - `workspace_path=None` -> skip workspace boundary check
  - `project_dir=None` -> skip workspace boundary check
- **VALIDATE**: Unit tests in Task 7

### Task 4: NodeExecutor Integration — pre_check
- **ACTION**: Integrate `NodeGuardrails.pre_check()` into `NodeExecutor._execute_with_timeout()`
- **IMPLEMENT**:
  - Add `node_guardrails: NodeGuardrails | None = None` to `__init__`
  - In `_execute_with_timeout()`, after building `BackendContext` and before `_run_via_registry()`:
    - If `self._node_guardrails` and `backend_name != "builtin"`:
      - Call `self._node_guardrails.pre_check(node, workspace_path)`
      - If `result.is_blocked`: raise `GuardrailBlockedException(result.reason, phase="pre")`
- **MIRROR**: BACKEND_NAME_RESOLVE pattern for detecting external backend
- **IMPORTS**: `from core.exceptions import GuardrailBlockedException`
- **GOTCHA**: Pre-check happens inside `_execute_with_timeout()` because `backend_name` is resolved there (line 639). Must be after `backend_name` resolution but before task creation.
- **VALIDATE**: Manual verification that pre-check is called before `asyncio.create_task(_run_via_registry())`

### Task 5: NodeExecutor Integration — post_check + exception handling + event
- **ACTION**: Add post_check after execution, exception handler for `GuardrailBlockedException`, event emission
- **IMPLEMENT**:
  1. In `execute_node()` method, after `_execute_with_timeout()` returns `result` and before trace emission:
     - If `self._node_guardrails` and `result` contains artifacts and backend was external:
       - Call `self._node_guardrails.post_check(result.get("artifacts", []), workspace_path=prep.workspace_path)`
       - If blocked: raise `GuardrailBlockedException(result.reason, phase="post")`
  2. Add `GuardrailBlockedException` to the system-error `except` clause (line 205-214) so it propagates to outer catch
  3. Add outer `except GuardrailBlockedException` handler (after `BudgetExhaustedError` catch, before `finally`):
     - Mark node FAILED with `retry_budget_preserved: True`
     - Emit `guardrail_blocked` event with phase, reason, node_id
     - Do NOT re-raise (node stays FAILED, execution continues to next node)
- **MIRROR**: EVENT_EMISSION + EXCEPTION_CATCH_NO_RETRY
- **IMPORTS**: Add `GuardrailBlockedException` to existing import from `core.exceptions`
- **GOTCHA**:
  - Post-check needs `result` which is inside `_execute_with_timeout`. Solution: post-check in `execute_node()` Stage 2 section, right after `_execute_with_timeout` returns and before trace emission.
  - `GuardrailBlockedException` from pre-check propagates through `_execute_with_timeout` -> `execute_node` Stage 2 `except` -> outer catch. Must be in the system-error raise group.
  - `GuardrailBlockedException` from post-check is raised in `execute_node` directly, caught by outer catch.
- **VALIDATE**: Trace through the exception path: pre-check raises inside `_execute_with_timeout` -> caught by `except (..., GuardrailBlockedException): raise` -> outer `except GuardrailBlockedException` handles

### Task 6: Dependency Injection
- **ACTION**: Thread `NodeGuardrails` through execution_factory -> DAGExecutionEngine -> NodeExecutor
- **IMPLEMENT**:
  1. In `execution_factory.py` `create_execution_engine()`:
     - After creating `backend_registry`:
       ```python
       node_guardrails = None
       if project_dir:
           from guardrails.node_guardrails import NodeGuardrails
           ng_config = ProjectConfig.load(work_dir).guardrails if work_dir else None
           if ng_config:
               node_guardrails = NodeGuardrails(
                   config=ng_config, project_dir=str(work_dir),
               )
       ```
     - Pass `node_guardrails=node_guardrails` to `DAGExecutionEngine` constructor
  2. In `core/dag_engine.py` `DAGExecutionEngine.__init__()`:
     - Add `node_guardrails: Any | None = None` parameter
     - Pass `node_guardrails=node_guardrails` to `NodeExecutor` constructor
  3. In `core/node_executor.py` `NodeExecutor.__init__()`:
     - Already added `node_guardrails` param in Task 4
- **MIRROR**: Follow existing pattern for `budget_manager`/`memory_manager` injection
- **IMPORTS**: `from guardrails.node_guardrails import NodeGuardrails` (in execution_factory)
- **GOTCHA**: Use `Any | None` type hint in dag_engine to avoid circular import (dag_engine already uses `Any` for many params). Import only in execution_factory where there's no circular risk.
- **VALIDATE**: `python -c "from control_plane.execution_factory import ExecutionFactory; print('OK')"` — no import errors

### Task 7: Tests
- **ACTION**: Create `tests/test_node_guardrails.py` with unit + integration tests
- **IMPLEMENT**:
  - **NodeGuardrails.pre_check tests**:
    - planner/evaluator agent_type -> allowed
    - workspace_path outside project_dir -> blocked
    - denied_commands match in task_description -> blocked
    - denied_commands no match -> allowed
    - node with backend="builtin" -> N/A (caller skips, not NodeGuardrails responsibility)
    - empty denied_commands -> allowed
    - None workspace_path with project_dir set -> allowed (skip check)
  - **NodeGuardrails.post_check tests**:
    - artifact matching protected_path (e.g., `.env`) -> blocked
    - artifact matching glob pattern (e.g., `.env.*`) -> blocked
    - artifact not matching any pattern -> allowed
    - empty artifacts list -> allowed
    - Windows-style path separators -> works correctly
    - `.ssh/` prefix match -> blocked
  - **NodeExecutor integration tests** (mock-based):
    - pre-check blocks -> GuardrailBlockedException raised
    - post-check blocks -> GuardrailBlockedException raised
    - guardrail_blocked event emitted with correct details
    - builtin backend skips guardrails
- **MIRROR**: Test structure from `tests/test_guardrails_unified.py` — fixtures, parametrize, mock patterns
- **IMPORTS**: `pytest`, `unittest.mock.MagicMock`, `NodeGuardrails`, `GuardrailsConfig`, `GuardrailBlockedException`, `DAGNode`, `GuardrailResult`
- **GOTCHA**: `DAGNode` needs required fields — check `core/dag_models.py` for required vs optional fields. Use `MagicMock` for NodeExecutor integration tests to avoid full object graph.
- **VALIDATE**: `python -m pytest tests/test_node_guardrails.py -v --tb=short`

---

## Testing Strategy

### Unit Tests

| Test | Input | Expected Output | Edge Case? |
|---|---|---|---|
| pre_check planner node | agent_type="planner" | allowed | No |
| pre_check generator node | agent_type="generator", no denied_commands | allowed | No |
| pre_check denied command | agent_type="generator", denied_commands=["rm -rf"], task="Delete files with rm -rf" | blocked | No |
| pre_check denied no match | agent_type="generator", denied_commands=["rm -rf"], task="Create hello world" | allowed | No |
| pre_check workspace escape | workspace_path="/outside/project", project_dir="/project" | blocked | Yes |
| pre_check workspace None | workspace_path=None | allowed | Yes |
| post_check .env modified | artifacts=["src/main.py", ".env"] | blocked | No |
| post_check glob .env.production | artifacts=[".env.production"] | blocked | No |
| post_check safe files | artifacts=["src/main.py", "tests/test_main.py"] | allowed | No |
| post_check empty artifacts | artifacts=[] | allowed | Yes |
| post_check SSH key | artifacts=["id_rsa"] | blocked | No |
| post_check .git/config | artifacts=[".git/config"] | blocked | No |

### Edge Cases Checklist
- [x] Empty artifacts list (skip post-check)
- [x] None workspace_path (skip workspace check)
- [x] None project_dir (skip workspace check)
- [x] Windows path separators (normalize with PurePosixPath or str replace)
- [x] Empty denied_commands (skip denied check)
- [x] Builtin backend (skip all guardrails — caller responsibility)
- [x] Case-insensitive denied_commands matching (use .lower())

---

## Validation Commands

### Static Analysis
```bash
python -m flake8 guardrails/node_guardrails.py core/exceptions.py core/project_config.py --max-line-length=100
```
EXPECT: Zero errors

### Unit Tests (new)
```bash
python -m pytest tests/test_node_guardrails.py -v --tb=short
```
EXPECT: All tests pass

### Regression Tests
```bash
python -m pytest tests/test_guardrails_unified.py tests/test_personal_guardrails.py -v --tb=short
```
EXPECT: No regressions

### Full Test Suite
```bash
python -m pytest -v --tb=short
```
EXPECT: No regressions

### Import Validation
```bash
python -c "from guardrails.node_guardrails import NodeGuardrails; from core.exceptions import GuardrailBlockedException; print('OK')"
```
EXPECT: `OK`

### Manual Validation
- [ ] `NodeGuardrails.pre_check` with planner node returns allowed
- [ ] `NodeGuardrails.post_check` with `.env` in artifacts returns blocked
- [ ] `GuardrailsConfig().protected_paths` returns default list
- [ ] `GuardrailBlockedException` message includes phase

---

## Acceptance Criteria
- [ ] All tasks completed
- [ ] All validation commands pass
- [ ] Tests written and passing
- [ ] No lint errors
- [ ] BuiltinBackend path unaffected
- [ ] Existing guardrails tests pass without modification

## Completion Checklist
- [ ] Code follows discovered patterns (GuardrailResult, exception class, guard class)
- [ ] Error handling matches codebase style (no retry for blocked, retry_budget_preserved)
- [ ] Logging follows codebase conventions (logger = logging.getLogger)
- [ ] Tests follow test patterns (fixtures, parametrize, MagicMock)
- [ ] No hardcoded values (protected_paths from config)
- [ ] No unnecessary scope additions
- [ ] Self-contained — no questions needed during implementation

## Risks
| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Post-check depends on git diff artifacts which may be empty | M | L | Empty artifacts -> skip post-check, no false positives |
| denied_commands substring match too broad (false positives) | L | M | Exact lowercase substring; user clears list via config if needed |
| Windows path normalization inconsistencies | L | M | Use `fnmatch` on forward-slash normalized paths |
| NodeExecutor parameter count grows | M | L | M6.3 will do NodeExecutorConfig grouping; M6.2 adds only 1 param |

## Notes
- Phase 1/2/3 are independent and can be implemented in parallel
- Phase 4 depends on 1+2, Phase 5 depends on 1+3
- Phase 6 (tests) depends on all prior phases
- The PRD was validated with grill-me (12 decisions) + grill-with-docs (3 CONTEXT.md updates)
- `NodeGuardrails` is intentionally stateless per-check — no accumulated state between calls
- `GuardrailResult` from `guardrails/policy.py` is reused (not redefined) for consistency
