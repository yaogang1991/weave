# PR Review: #901 — fix: count only active nodes in DAG explosion protection (#899)

**Reviewed**: 2026-05-26
**Author**: yaogang1991
**Branch**: fix/899-dag-explosion-active-node-count → main
**Decision**: APPROVE

## Summary
Clean, well-focused fix that switches DAG explosion protection from a blacklist (exclude dead) to a whitelist (only count schedulable) approach. The change correctly identifies only PENDING/RUNNING/RETRYING nodes as consuming execution resources, fixing a real cascade failure scenario where dead nodes blocked recovery replans.

## Findings

### CRITICAL
None

### HIGH
None

### MEDIUM
1. **Weak assertion in `test_genuinely_too_many_active_nodes_still_blocked`** (tests/test_899_active_node_count.py:113-114)
   - Uses `if not replanned: assert len(dag_out.nodes) == 15` — if `replanned` is True, the test passes silently without verifying anything. Should use `assert replanned is False` as the primary assertion, with the node count check as secondary.

### LOW
None

## Validation Results

| Check | Result |
|---|---|
| Tests (#899) | Pass (3/3) |
| Tests (#795 regression) | Pass (6/6) |
| Lint | Skipped |
| Build | Skipped |

## Files Reviewed
- `core/dag_engine.py` — Modified (5 lines changed: blacklist → whitelist approach)
- `tests/test_899_active_node_count.py` — Added (125 lines, 3 test cases)
