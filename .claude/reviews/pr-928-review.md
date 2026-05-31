# PR Review: #928 — refactor: group DAGExecutionEngine tunable params into DAGEngineConfig (#915)

**Reviewed**: 2026-05-26
**Author**: yaogang1991
**Branch**: refactor/dag-engine-config-915-v2 → main
**Decision**: REQUEST CHANGES

## Summary
PR has two main parts: (1) extracting 15 tunable params from DAGExecutionEngine into DAGEngineConfig, clean and well-executed; (2) new post-execution pipeline (commit/push/PR creation) with related functionality. The refactoring is fine, but new features have test coverage regression, change detection bugs, and mixed responsibilities.

## Findings

### CRITICAL
None

### HIGH

1. **test_718_replan_no_effect.py — test_replan_failure_returns_false semantics regression**
   - File: `tests/test_718_replan_no_effect.py:40-44`
   - `replan_handler=bad_replan` was removed. Test changed from verifying "handler throws exception -> returns False" to "no handler -> returns False".
   - Test docstring still says "When replan_handler throws" but this path is no longer tested.
   - The exception handling code path in `_try_execute_replan` has no test coverage.
   - **Fix**: Restore `replan_handler=bad_replan`:
     ```python
     engine = DAGExecutionEngine(
         agent_executor=AsyncMock(),
         failure_handler=AsyncMock(),
         replan_handler=bad_replan,
         config=DAGEngineConfig(max_parallel=1),
     )
     ```

### MEDIUM

1. **post_execution.py — `_has_changes` does not detect untracked files**
   - File: `integrations/github/post_execution.py:31-38`
   - `git diff --stat HEAD` only detects modifications to tracked files, not new untracked files.
   - If agent only creates new files, `_has_changes` returns False and changes are never committed.
   - **Fix**: Use `git status --porcelain`:
     ```python
     async def _has_changes(work_dir: str) -> bool:
         result = await asyncio.to_thread(
             run_with_progress,
             ["git", "status", "--porcelain"],
             timeout=15, cwd=work_dir,
         )
         return result.returncode == 0 and bool(result.stdout.strip())
     ```

2. **post_execution.py — `git add -A` may stage sensitive files**
   - File: `integrations/github/post_execution.py:48-53`
   - `git add -A` stages everything including `.env`, credentials, etc.
   - In unattended automation, this could leak secrets.
   - **Suggestion**: Add explicit exclusion list or only stage specific directories.

3. **test_retry_eval_feedback.py — indentation error**
   - File: `tests/test_retry_eval_feedback.py:46-52, 74-79`
   - DAGExecutionEngine constructor args at 4-space indent instead of 8-space.
   - Syntactically valid but violates PEP 8 (E128).
   - **Fix**: Align args to 12-space indent.

4. **DAGEngineConfig lacks input validation**
   - File: `core/dag_engine.py:55-96`
   - Plain class (not Pydantic), no parameter validation.
   - `max_parallel=0`, negative values, `backoff_base=0` silently accepted.
   - **Suggestion**: Add basic validation or use Pydantic BaseModel.

### LOW

1. **pr_body.py — `generate_llm_review` creates new LLMClient per call**
   - File: `integrations/github/pr_body.py:38-39`
   - Should reuse or accept external client.

2. **post_execution.py — `run: Any` type too broad**
   - File: `integrations/github/post_execution.py:56`
   - Should use `Run` type from `control_plane.models`.

3. **PR scope mixing**
   - Refactoring (DAGEngineConfig) and new feature (post-execution pipeline) in one PR.

## Validation Results

| Check | Result |
|---|---|
| Type check | Skipped (no mypy configured) |
| Lint | Skipped |
| Tests | Pass (3274 passed per PR description) |
| Build | N/A (Python) |

## Files Reviewed

| File | Change Type |
|---|---|
| core/dag_engine.py | Modified — DAGEngineConfig extraction |
| control_plane/execution_factory.py | Modified — caller update |
| control_plane/service.py | Modified — work_dir tracking |
| cli/execution.py | Modified — caller update |
| cli/github.py | Modified — post-execution integration |
| integrations/base.py | Modified — push_changes cwd param |
| integrations/github/github_host.py | Modified — push_changes cwd |
| integrations/github/post_execution.py | **Added** — post-execution handler |
| integrations/github/pr_body.py | **Added** — PR body generation |
| tests/test_718_replan_no_effect.py | Modified — config migration |
| tests/test_432_timeout_retry_budget.py | Modified — config migration |
| tests/test_724_impl_success_breakdown.py | Modified — config migration |
| tests/test_728_replan_merge_edges.py | Modified — config migration |
| tests/test_751_752_audit_event_remap.py | Modified — config migration |
| tests/test_770_retry_exhaustion_remap_integration.py | Modified — config migration |
| tests/test_775_replan_rewire_downstream.py | Modified — config migration |
| tests/test_789_superseded_replan.py | Modified — config migration |
| tests/test_795_replan_node_explosion.py | Modified — config migration |
| tests/test_797_replan_preserve_pending.py | Modified — config migration |
| tests/test_829_replan_continue.py | Modified — config migration |
| tests/test_cascade_skip.py | Modified — config migration |
| tests/test_dag_checkpoint.py | Modified — config migration |
| tests/test_dependency_types.py | Modified — config migration |
| tests/test_execution_summary.py | Modified — config migration |
| tests/test_fault_tolerance_hemostasis.py | Modified — config migration |
| tests/test_llm_dag_integration.py | Modified — config migration |
| tests/test_node_states.py | Modified — config migration |
| tests/test_post_execution.py | **Added** — post_execution tests |
| tests/test_pr_body.py | **Added** — pr_body tests |
| tests/test_reliability.py | Modified — config migration |
| tests/test_retry_eval_feedback.py | Modified — config migration |
| tests/test_silent_exit_prevention.py | Modified — config migration |
| tests/test_watchdog.py | Modified — config migration |
