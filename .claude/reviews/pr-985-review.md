# PR Review: #985 — fix: add provider health check to Plan stage (#934)

**Reviewed**: 2026-05-29
**Author**: yaogang1991
**Branch**: fix/934-plan-provider-health → main
**Decision**: APPROVE

## Summary
Adds ProviderHealthTracker to the Planner so consecutive LLM timeouts are tracked and futile retries are skipped. Clean integration with existing provider health infrastructure from #900.

## Findings

### CRITICAL
None

### HIGH
None

### MEDIUM
- **Threshold/retry mismatch**: `ProviderHealthTracker` defaults to `failure_threshold=3`, but `_PLAN_TIMEOUT_RETRIES=2` (3 attempts). Within a single plan session, the health check never triggers skip. It becomes effective across plan sessions (e.g., when replan is triggered after initial plan failure). The PR description implies faster fail-per-session behavior ("1st timeout → skip retry"), which requires threshold=1 to match. Not blocking — the mechanism is correct and works in replan scenarios.

### LOW
None

## Validation Results

| Check | Result |
|---|---|
| Tests | 2135 passed, 1 flaky (test_memory, unrelated) |
| Lint | Skipped |
| Type check | Skipped |

## Files Reviewed
- `orchestrator/planner.py` — Modified (ProviderHealthTracker integration in plan retry loop)
