# PR Review: #974 — M6 architecture audit gap closure — CodexBackend MCP config + issue cleanup

**Reviewed**: 2026-05-29
**Author**: yaogang1991
**Branch**: fix/m6-audit-gap-closure → main
**Decision**: APPROVE with comments

## Summary

Small, focused PR that closes the last MCP config gap between CodexBackend and ClaudeCodeBackend, clarifies bidirectional protocol status, and cleans up 4 stale issues. The MCP config passing follows the same pattern established in ClaudeCodeBackend. Code is clean and well-structured.

## Findings

### CRITICAL
None

### HIGH
None

### MEDIUM

1. **Missing test coverage for new MCP config functionality** — `tests/test_codex_backend.py`
   - No tests for `_write_codex_mcp_config()` or the `--mcp-config` flag in `execute()`. The 25 existing tests verify the new `mcp_config` field doesn't break anything, but the new path (config writing, flag passing, cleanup) is untested.
   - Suggested: Add at least 2 tests — one verifying `_write_codex_mcp_config` produces a valid file, and one verifying `execute()` passes `--mcp-config` when mcp_config is set.

2. **Cleanup inconsistency with ClaudeCodeBackend** — `agent/backends/codex.py:123-127`
   - CodexBackend uses raw `mcp_config_path.unlink()` with `debug` log on failure.
   - ClaudeCodeBackend uses `MCPConfigExporter.cleanup_config()` which logs at `warning` level.
   - Suggested: Use `MCPConfigExporter.cleanup_config(mcp_config_path)` for consistency and appropriate log level.

### LOW

1. **MCP config leak on FileNotFoundError path** — `agent/backends/codex.py:96-100`
   - If `create_subprocess_exec` raises `FileNotFoundError`, the method returns early without cleaning up the MCP config file. The file would persist on disk until manually cleaned or OS temp cleanup.
   - Low severity because this only happens when the codex binary doesn't exist, which is a misconfiguration scenario.

2. **Broad exception swallowing in `_write_codex_mcp_config`** — `agent/backends/codex.py:340-341`
   - `except Exception` silently returns `None` if MCP config export fails for any reason. This makes debugging harder if the export breaks.
   - The existing `logger.debug(..., exc_info=True)` mitigates this somewhat.

## Validation Results

| Check | Result |
|---|---|
| Tests (CodexBackend) | 25/25 passed |
| Lint | Skipped |

## Files Reviewed

| File | Change |
|---|---|
| `agent/backends/codex.py` | Modified — MCP config support (+37/-3) |
| `core/config.py` | Modified — `mcp_config` field on CodexBackendConfig (+2/-1) |
| `agent/backends/bidirectional.py` | Modified — docstring clarification (+4/-1) |
