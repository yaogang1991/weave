# PR Review: #993 — fix: stall timeout falsely kills active generator nodes (#978)

**Reviewed**: 2026-05-29
**Author**: yaogang1991 (fix by reviewer)
**Branch**: fix/978-stall-timeout-false-kill → main
**Decision**: APPROVE (after fix)

## Summary
Fixes stall detector killing active generator nodes. PR had config default changes and tests but was missing the core fix — `tracker.report("heartbeat")` in `_on_progress`. Reviewer added the missing call and updated affected tests.

## Findings

### CRITICAL
None

### HIGH
- **Missing core fix**: `_on_progress` callback in `core/node_executor.py:643` did not call `tracker.report()`. The PR description stated this change but the diff did not include it. Fixed by reviewer.

### MEDIUM
None

### LOW
None

## Actions Taken
1. Added `tracker.report("heartbeat")` to `_on_progress` in `core/node_executor.py`
2. Updated `test_722_feature_timeout_scaling.py` for new defaults (base 120→300, cap 600→900)
3. Closed PR #982 as superseded

## Validation Results

| Check | Result |
|---|---|
| Tests | 19/19 stall-related tests passed |
| Lint | Skipped |

## Files Reviewed
- `core/config.py` — Modified (GeneratorStallScaleConfig defaults increased)
- `core/node_executor.py` — Modified (added tracker.report to _on_progress, by reviewer)
- `tests/test_978_stall_reset.py` — Added (6 new tests)
- `tests/test_722_feature_timeout_scaling.py` — Modified (updated for new defaults, by reviewer)
