# PR Review: #647 — refactor: extract EvaluationPipeline, 3-stage node execution pipeline (ADR-0015)

**Reviewed**: 2026-05-21
**Author**: yaogang1991
**Branch**: worktree-refactor+node-exec-pipeline → main
**Decision**: REQUEST CHANGES

## Summary

Good structural refactoring that extracts a 600-line god method into a clean 3-stage pipeline with proper separation of concerns. The new `EvaluationPipeline` class is well-structured and independently testable. However, there is one **CRITICAL** runtime TypeError that will crash node execution, and one **HIGH** test regression that must be fixed before merge.

## Findings

### CRITICAL

**C1: `node_executor.py:644-645` — TypeError on `_get_stall_timeout` due to stale API call**

`NodeExecutor._get_stall_timeout()` still calls `stall_timeout_for(agent_type, node=node, workspace_path=workspace_path)` with the old keyword arguments. But `config.py` refactored `stall_timeout_for()` to accept `file_count`, `test_count`, `dep_count` instead of `node` and `workspace_path`.

This will cause a `TypeError: NodeTimeoutConfig.stall_timeout_for() got an unexpected keyword argument 'node'` at runtime whenever `node_timeout_config` is set and `_execute_with_timeout` is called.

Reproduced:
```python
executor._get_stall_timeout('generator', node=MagicMock(), workspace_path='/tmp')
# TypeError: NodeTimeoutConfig.stall_timeout_for() got an unexpected keyword argument 'node'
```

**Fix**: Update `NodeExecutor._get_stall_timeout()` to compute file/test/dep counts before calling the new API, or simplify to just pass `agent_type` (since the I/O is now externalized per the PR's design intent).

### HIGH

**H1: `test_fault_tolerance_hemostasis.py:344` — `test_min_max` regression**

`NodeTimeoutConfig.max_timeout` now returns 1200 (from `eval_scale.max_timeout`) instead of the expected 600. The test creates `NodeTimeoutConfig(default_timeout=300, overrides={"generator": 600, "evaluator": 120})` and asserts `max_timeout == 600`.

This PR added `EvaluatorStallScaleConfig` and `GeneratorStallScaleConfig` to `NodeTimeoutConfig`, but the `max_timeout` property only considers `eval_scale.max_timeout` (1200), not the new stall scale caps. Either:
- The `max_timeout` property should include `eval_stall_scale.cap` and `gen_stall_scale.cap`, or
- The test should be updated to reflect the new behavior

### MEDIUM

**M1: `evaluation_pipeline.py` — No dedicated unit tests for `EvaluationPipeline.evaluate()`**

The new `EvaluationPipeline` class is only tested indirectly through integration tests. Static helpers have direct tests, but the main `evaluate()` pipeline method lacks dedicated unit tests.

**M2: `evaluation_pipeline.py:106-107` — Redundant None check on `result` parameter**

The caller already does `result or {}`, so `result` can never be `None` here. The type annotation also says `dict[str, Any]` (not `dict | None`). Dead code.

**M3: `AnomalyDetector.is_anomalous` property reads `_anomalous` without lock**

Inconsistent with the thread-safety intent of the other changes.

### LOW

**L1: Stale docstring references to `_execute_single_node`** in test files.

**L2: `evaluation_pipeline.py` uses `Any` type for `node` parameter** instead of `DAGNode` in multiple methods.

## Validation Results

| Check | Result |
|---|---|
| Tests | 652/653 passed (1 pre-existing) |
| PR-specific tests | 161/162 passed (`test_min_max` regression) |
| Runtime TypeError | CONFIRMED in `_get_stall_timeout` |

## Files Reviewed

| File | Change Type |
|---|---|
| `core/evaluation_pipeline.py` | Added (533 lines) |
| `core/node_executor.py` | Modified (major refactor) |
| `core/config.py` | Modified (new config classes, API change) |
| `core/dag_engine.py` | Modified (removed delegation layer) |
| `core/progress.py` | Modified (thread safety) |
| `CLAUDE.md` | Modified (docs) |
| 10 test files | Modified (API migration) |
