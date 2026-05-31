# PR Review: #926 — fix: classify node failures for provider health tracking (#921, #924)

**Reviewed**: 2026-05-26
**Author**: yaogang1991
**Branch**: fix/provider-health-failure-classification-921 → main
**Decision**: APPROVE

## Summary

Well-scoped fix that adds failure classification to the provider health tracker, preventing evaluation failures, stall timeouts, and local timeouts from incorrectly triggering provider health degradation. Clean design with good backward compatibility and thorough test coverage.

## Findings

### CRITICAL
None

### HIGH
None

### MEDIUM
None

### LOW

1. **`_classify_failure` uses substring matching on error text** — `core/dag_engine.py:55-70`
   - The classifier checks for substrings like "eval", "stall", "timeout" in error messages. While adequate for current error messages produced by the system, a message like "failed to evaluate alternative" would incorrectly classify as `EVALUATION`.
   - Severity: LOW — the current error messages from the DAG engine are well-structured and unlikely to produce false positives. The ordering of checks (rate_limit → stall → eval → timeout) is correct.

2. **`_API_FAILURE_CATEGORIES` includes `UNKNOWN`** — `core/provider_health.py:38-40`
   - Unknown failures counting toward the unhealthy threshold is the conservative default and documented in the code. This is correct behavior.

3. **Empty error string maps to `API_ERROR`** — `core/dag_engine.py:56-57`
   - When `final.error` is empty/None and the node failed, it defaults to `API_ERROR`. Conservative but correct.

## Validation Results

| Check | Result |
|---|---|
| Type check | Skipped (no mypy/pyright in CI) |
| Lint | Skipped |
| Tests | Pass (18/18) |
| Build | Skipped (Python, no build step) |

## Files Reviewed

| File | Change Type |
|---|---|
| `core/provider_health.py` | Modified — added `FailureCategory` enum, `_API_FAILURE_CATEGORIES` set, updated `record_failure()` signature |
| `core/dag_engine.py` | Modified — added `_classify_failure()`, updated `record_failure` call site |
| `tests/test_provider_health.py` | Added — 18 tests covering all categories, mixed failures, backward compatibility |
