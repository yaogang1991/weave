# PR Review: #929 — fix: skip LLM calls when provider unhealthy (#911)

**Reviewed**: 2026-05-26
**Author**: yaogang1991
**Branch**: fix/911-skip-replan-unhealthy-provider → main
**Decision**: APPROVE (with comments)

## Summary

The PR correctly extends #900 provider health tracking to skip `failure_handler` and `replan_handler` LLM calls when the provider is already unhealthy, avoiding ~150s timeout per call. Clean extraction of `_get_provider_model()` helper, good test coverage with 6 new tests, and correct fix for 2 pre-existing test failures.

## Findings

### CRITICAL

None.

### HIGH

None.

### MEDIUM

1. **Unprotected fallback `failure_handler` call after retry exhaustion** (`core/dag_engine.py:777`)

   The retry-failure fallback path still calls `failure_handler` without checking provider health:
   ```python
   fallback = await self.failure_handler(
       dag, failed_id,
       dag.nodes[failed_id].error or "",
   )
   ```
   If the provider goes unhealthy during a retry, this LLM call will still timeout for ~150s. The first `failure_handler` call (line ~596) is now protected, but this second call (line 777) is not.

   **Likelihood**: Low. Requires provider to be healthy at first `failure_handler` call (returns "retry"), then become unhealthy during retry execution, then still be unhealthy at fallback call.

   **Recommendation**: Add the same health check before line 777 in a follow-up, or as part of `TODO(#911-followup)`. Not blocking — the main case (provider already unhealthy before first call) is correctly handled.

### LOW

1. **`TestSkipFailureHandlerWhenUnhealthy` tests replicate engine logic instead of testing through `execute()`** (`tests/test_911_unhealthy_skip_replan.py:50-92`)

   The failure handler tests manually replicate the unhealthy-check if/else logic rather than exercising it through the engine's `execute()` method. This means they verify the decision logic but not the integration path. Contrast with `TestSkipReplanWhenUnhealthy` which correctly tests through `_try_execute_replan()`.

   **Recommendation**: Consider an integration test that creates an engine with a healthy→unhealthy transition and runs `execute()` to verify the full path. Low priority since the replan tests cover the integration pattern.

2. **`_get_provider_model()` always returns `("anthropic", "")` in practice** (`core/dag_engine.py:189-202`)

   The TODO comment is clear about this. Not a bug — the method correctly captures current behavior. Just noting it for awareness that multi-provider scenarios won't benefit until `LLMConfig` is threaded through.

## Validation Results

| Check | Result |
|---|---|
| Tests (new) | Pass — 6/6 |
| Tests (reliability) | Pass — 26/26 |
| Lint | Skipped |
| Build | Skipped |

## Files Reviewed

| File | Change |
|---|---|
| `core/dag_engine.py` | Modified — added `_get_provider_model()`, health checks before failure_handler and replan_handler |
| `tests/test_911_unhealthy_skip_replan.py` | Added — 6 tests covering skip/healthy/recovery scenarios |
| `tests/test_reliability.py` | Modified — fixed 2 pre-existing failures by passing high-threshold `ProviderHealthTracker` |
