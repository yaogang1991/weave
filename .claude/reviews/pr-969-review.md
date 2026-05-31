# PR Review: #969 — feat: LightweightLLMCaller + BuiltinBackend/BackendRegistry refactoring (M6.3)

**Reviewed**: 2026-05-29
**Author**: yaogang1991
**Branch**: feat/m6.3-lightweight-llm-caller → main
**Decision**: APPROVE with comments

## Summary

Clean, well-scoped refactoring that introduces `LightweightLLMCaller` for single-shot LLM calls (planner/evaluator nodes) while preserving full backward compatibility via `BuiltinBackend` dual-path routing. 22 files changed (+912/-76), 24 new tests, 14 test files updated. All 177 related tests pass.

## Findings

### CRITICAL

None.

### HIGH

None.

### MEDIUM

**M1: `token_usage` passed by reference, not copy**
`agent/backends/builtin.py:145`

```python
metadata={"token_usage": self._lightweight_caller.token_usage},
```

`token_usage` is a mutable dict that accumulates across calls. Every `BackendResult.metadata["token_usage"]` points to the **same dict object**, so inspecting earlier results shows accumulated totals rather than per-call usage. Downstream code that snapshots or logs metadata may be misled.

Fix: `metadata={"token_usage": dict(self._lightweight_caller.token_usage)}`

**M2: `hasattr` checks on Pydantic model fields**
`agent/backends/builtin.py:106-118`

```python
if hasattr(node, "eval_feedback") and node.eval_feedback:
if hasattr(node, "auto_eval_result") and node.auto_eval_result:
```

`DAGNode` defines both `eval_feedback: str = ""` and `auto_eval_result: dict | None = None` as actual Pydantic fields (`core/dag_models.py:134-135`). They are always present — `hasattr` is unnecessary and unidiomatic for Pydantic models.

Fix: Direct attribute access with truthy check:
```python
if node.eval_feedback:
if node.auto_eval_result:
```

### LOW

**L1: `agent_executor` closure is dead code in execution_factory**
`control_plane/execution_factory.py:236`

```python
agent_executor=pool.get_executor(session_id),
```

Since `backend_registry` is always non-None and `NodeExecutor` prioritizes the registry path, this closure is never invoked. It also triggers the new `DeprecationWarning` in `AgentPool.get_executor`. Harmless but wasteful. Consider passing a no-op or removing once AgentPool is fully deprecated.

**L2: Duplicated test helper boilerplate**
Multiple test files (`test_retry_progress_preservation.py`, `test_retry_feedback.py`, `test_existing_files_context.py`) create `_Planner` instances with nearly identical setup code. The `_make_orchestrator` helper in `test_existing_files_context.py` is a good pattern but not extracted into a shared conftest.

## Validation Results

| Check | Result |
|---|---|
| Type check | Skipped (no mypy/pyright configured) |
| Lint (flake8) | Pass |
| Tests (core modules) | 55/55 passed |
| Tests (orchestrator integration) | 122/122 passed |

## Files Reviewed

| File | Change |
|---|---|
| `agent/lightweight_llm_caller.py` | Added (87 lines) |
| `agent/backends/builtin.py` | Modified (major refactor) |
| `agent/backends/registry.py` | Modified (constructor decoupling) |
| `agent/agent_pool.py` | Modified (deprecation warning) |
| `control_plane/execution_factory.py` | Modified (wiring) |
| `cli/execution.py` | Modified (from_pool migration) |
| `tests/test_lightweight_llm_caller.py` | Added (515 lines, 24 tests) |
| `tests/test_*.py` (14 files) | Modified (constructor signature updates) |
