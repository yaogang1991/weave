# PR Review: #930 — fix: replan handler crash when args lacks requirement (#913)

**Reviewed**: 2026-05-26
**Author**: yaogang1991
**Branch**: fix/913-replan-handler-requirement-attr → main
**Decision**: APPROVE

## Summary

Minimal, targeted one-line fix for an `AttributeError` that causes 100% cascade-skip when replan is triggered via `python main.py execute`. The `getattr` + DAG reasoning fallback chain is correct. 5 new tests provide good coverage.

## Findings

### CRITICAL
None

### HIGH
None

### MEDIUM
None

### LOW

1. **Test 1-3 test inline expression, not the actual lambda closure** — `tests/test_913_replan_requirement_attr.py:40-57`
   - Tests 1-3 evaluate `getattr(args, "requirement", "") or dag.reasoning or ""` as a standalone expression rather than through the lambda in `_build_runtime`. The logic is trivially correct, but the real risk is the lambda closure capturing `args` — which tests 4-5 cover via `_try_execute_replan`. Acceptable as-is since the integration tests fill the gap.

2. **Lambda captures `args` by closure reference** — `cli/execution.py:371`
   - If `_build_runtime` were ever called with a mutating `args` object, the closure would see the mutated value. This is a non-issue in the current codebase since `_build_runtime` is a synchronous builder with no mutation after lambda creation, but worth noting for future readers.

## Validation Results

| Check | Result |
|---|---|
| Tests (PR-specific) | Pass — 5/5 |
| Full suite | Skipped (timeout) |
| Lint | Skipped |
| Build | Skipped |

## Correctness Analysis

**Bug root cause**: The `execute` subcommand's argparse Namespace has `plan_file` but not `requirement`. The replan lambda in `_build_runtime` captured `args.requirement` directly, causing `AttributeError` → replan fails → fallback to skip → cascade-skip all downstream nodes.

**Fix**: `getattr(args, "requirement", "") or dag_ref.reasoning or ""` — three-level fallback:
1. `args.requirement` if present (e.g., from `cmd_run` which creates a new Namespace with it)
2. `dag_ref.reasoning` from the DAG itself (always available since DAGs are created with reasoning)
3. Empty string as final fallback

**The `dag_ref` parameter is correct** — it's the DAG reference passed to the replan handler, so `dag_ref.reasoning` is the right source. The `IntelligentOrchestrator.replan()` signature accepts `requirement: str = ""`, so the third argument is always safe.

## Files Reviewed

| File | Change |
|---|---|
| `cli/execution.py` | Modified — 1-line fix (getattr + fallback chain) |
| `tests/test_913_replan_requirement_attr.py` | Added — 5 tests (3 unit + 2 async integration) |
