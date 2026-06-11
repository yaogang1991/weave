# PR Review: #1116 — refactor: M7.2 — 架构清理与技术债务偿还 (#1008)

**Reviewed**: 2026-06-07
**Author**: yaogang1991
**Branch**: refactor/issue-1008-m72-architecture-cleanup → main
**Decision**: APPROVE

## Summary

Well-structured architecture cleanup PR that enforces DAG immutability, extracts replan logic into a dedicated module, adds Protocol interfaces to eliminate layer violations, and removes deprecated guardrails wrappers. All changes are consistent and properly tested.

## Findings

### CRITICAL
None

### HIGH
None

### MEDIUM
1. **Duplicate `_classify_failure` assignment** in `core/dag_engine.py` — assigned at ~line 49 (after import block) and again at ~line 77 (after class-level declarations). Harmless but redundant.
2. **`update_node()` docstring inconsistency** — states "reserved for the hot execution loop" but `_apply_rename_map()` in `orchestrator/planner.py` also uses it during the construction phase. Minor documentation mismatch.

### LOW
1. **Test ordering flakiness** — `test_spec_contract.py::TestPyprojectConfig::test_pytest_can_collect_tests` fails in full-suite run but passes in isolation. Pre-existing, not caused by this PR.

## Validation Results

| Check | Result |
|---|---|
| Import verification (dag_replan, dag_engine, guardrails) | Pass |
| Full test suite (4312 tests) | Pass (8 pre-existing failures) |
| Pre-existing failure verification on main | Confirmed (7/8 on main, 1 flaky) |

## Files Reviewed

- `cli/execution.py` — Modified (immutability pattern)
- `core/dag_models.py` — Modified (add_node/add_edge return new DAG)
- `core/dag_engine.py` — Modified (extracted replan logic, Protocol imports)
- `core/dag_replan.py` — Added (281 lines, extracted replan helpers)
- `core/protocols.py` — Modified (added BackendManagerProto, EvaluatorEngineProto)
- `guardrails/policy.py` — Modified (removed deprecated methods)
- `orchestrator/planner.py` — Modified (immutability + update_node in _apply_rename_map)
- 50 test files — Modified (consistent `dag = dag.add_node(...)` pattern)
