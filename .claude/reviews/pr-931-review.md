# PR Review: #931 — refactor: split intelligent_orchestrator.py into planner + adapter (#919)

**Reviewed**: 2026-05-26
**Author**: yaogang1991
**Branch**: refactor/919-orchestrator-split → main
**Decision**: APPROVE with comments

## Summary

Clean structural refactoring that splits a 943-line monolithic orchestrator into three focused modules (planner 487L, adapter 252L, facade 234L) with no API changes. One subtle behavioral difference found in `_plan_free_text` truncation check ordering, and one pre-existing bug silently fixed.

## Findings

### CRITICAL

None.

### HIGH

None.

### MEDIUM

1. **Truncation check order changed in `_plan_free_text`** — `orchestrator/planner.py:~1553`

   The original code checked `_is_response_truncated()` on the **full** LLM response before truncating to 2000 chars. The new code checks it on the **already-truncated** content. If the response was >2000 chars but complete (not LLM-truncated), the truncated version ending with `"\n... (truncated)"` will always be flagged as truncated, causing the "produce a SIMPLER plan" prompt instead of "provide valid JSON". This changes retry behavior for edge cases.

   **Fix**: Check truncation before the length-based truncation:
   ```python
   failed_content = response.get("content", "")
   is_truncated = _is_response_truncated(failed_content)  # check FIRST
   if len(failed_content) > 2000:
       failed_content = failed_content[:2000] + "\n... (truncated)"
   messages.append({"role": "assistant", "content": failed_content})
   if is_truncated:  # use saved result
   ```

### LOW

1. **Dead code: `_check_post_estimation_budget`** — `orchestrator/intelligent_orchestrator.py:~220`
   The budget check is now inlined in `Planner._estimate_dag_tokens()`, making the facade's version unreachable via normal code paths. Consider removing if no external caller exists.

2. **Duplicated `_is_response_truncated` function** — exists as module-level function in both `intelligent_orchestrator.py` and `planner.py`. The facade's class method delegates to the local copy rather than importing from planner. Minor unnecessary duplication.

3. **`_prune_messages_for_size_static`** — `orchestrator/intelligent_orchestrator.py:~227`
   New static method passes `""` as model to `prune_messages_for_tokens`, differing from the instance method which uses `self.llm_config.model`. If model-specific token estimation matters, this could produce incorrect pruning.

### POSITIVE

1. **Silent bug fix**: Original `replan()` method referenced undefined `node_id` variable (should be `nid`) in a `logger.warning` call when `output_artifacts > 30`. The refactoring removed this code path, eliminating a latent `NameError`.

## Validation Results

| Check | Result |
|---|---|
| Tests | 1989 passed / 1 failed (pre-existing, `test_memory.py`) / 2 skipped |
| Lint | Skipped |
| Type check | Skipped |
| Build | Skipped |

## Files Reviewed

| File | Change |
|---|---|
| `orchestrator/planner.py` | Added — DAG generation, structured output, plan→DAG conversion (487 lines) |
| `orchestrator/adapter.py` | Added — failure adaptation and replan logic (252 lines) |
| `orchestrator/intelligent_orchestrator.py` | Modified — rewritten as thin backward-compat facade (234 lines) |
| `tests/test_reliability.py` | Modified — fix pre-existing ProviderHealthTracker cascade skip |
