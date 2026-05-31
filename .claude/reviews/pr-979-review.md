# PR Review: #979 — fix: --backend claude_code ImportError + silently ignored (#977, #975)

**Reviewed**: 2026-05-29
**Author**: yaogang1991
**Branch**: fix/977-975-claude-code-backend → main
**Decision**: APPROVE

## Summary
PR fixes two blocking bugs: (1) ImportError crash when `--backend claude_code` is used due to stale import name, and (2) the backend flag being silently ignored because `default_agent_backend` was never passed through `DAGEngineConfig`. Both fixes are correct and minimal.

## Findings

### CRITICAL
None

### HIGH
None

### MEDIUM
- **llm_client.py scope overlap**: PR includes `max_retries=0` changes to `core/llm_client.py` that are identical to PR #976 (fixes #967). These are correct and beneficial but unrelated to the PR's stated purpose (#977, #975). If #979 is merged first, #976 will cleanly rebase since changes are identical.

### LOW
None

## Validation Results

| Check | Result |
|---|---|
| Tests | 686/687 passed (1 flaky codex test unrelated) |
| Lint | Skipped |
| Type check | Skipped |

## Files Reviewed
- `cli/execution.py` — Modified (import fix + default_agent_backend passthrough)
- `core/llm_client.py` — Modified (max_retries=0, belongs to PR #976)
