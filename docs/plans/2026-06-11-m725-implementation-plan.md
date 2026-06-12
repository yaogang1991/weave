# M7.2.5 Implementation Plan — 安全移除 M6 废弃代码

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Remove ~1718 lines of deprecated M6 code (AgentPool, AgentWorker, OutputMonitor, StuckDetector) in a single pass, making BuiltinBackend lightweight-only.

**Architecture:** BuiltinBackend currently has two execution paths: lightweight (LightweightLLMCaller, for planner/evaluator) and pool (AgentPool/AgentWorker tool loop, for generator fallback). We remove the pool path entirely. Generator nodes will always use external backends (ClaudeCodeBackend/CodexBackend) via BackendRegistry. If no external backend is available, the lightweight path runs and NodeExecutor's quality gate catches the failure.

**Tech Stack:** Python 3.11+, Pydantic, asyncio, pytest

**Design doc:** `docs/plans/2026-06-11-m725-remove-m6-deprecated-code-design.md`

---

## Task 1: BuiltinBackend 去 pool 化

**Files:**
- Modify: `agent/backends/builtin.py`

**Step 1: Remove pool parameter and related methods from BuiltinBackend**

In `agent/backends/builtin.py`, make these changes:

1. Remove the `_UNSET` sentinel and `pool` parameter from `__init__`
2. Remove `_ensure_closure()` method
3. Remove `_execute_pool()` method
4. Simplify `execute()` to always use lightweight path

The file should become:

```python
"""BuiltinBackend -- wraps LightweightLLMCaller for node execution (M7.2.5).

Lightweight-only: single-shot LLM calls for planner/evaluator nodes.
Generator nodes should use external backends (claude_code/codex)
via BackendRegistry. If no external backend is available, this backend
produces text-only output (no tool loop).
"""
from __future__ import annotations

import json
import logging
import platform
import sys
from pathlib import Path
from typing import Any

from core.backend_models import BackendContext, BackendResult, BackendStatus
from core.exceptions import AgentExecutionError
from agent.backends.base import AgentBackend
from agent.prompts import SYSTEM_PROMPTS

logger = logging.getLogger(__name__)


class BuiltinBackend(AgentBackend):
    """Agent backend using LightweightLLMCaller for single-shot LLM calls.

    Suitable for planner and evaluator nodes that don't need a tool loop.
    Generator nodes should use external backends via BackendRegistry.
    """

    def __init__(
        self,
        lightweight_caller: Any = None,
        session_store: Any = None,
        session_id: str = "",
    ) -> None:
        if lightweight_caller is None:
            raise AgentExecutionError(
                "BuiltinBackend requires a lightweight_caller. "
                "Since M7.2.5, the pool-based tool loop has been removed."
            )
        self._lightweight_caller = lightweight_caller
        self._session_store = session_store
        self._session_id = session_id

    def _get_system_prompt(self, agent_type: str) -> str:
        """Resolve the system prompt for a given agent type."""
        prompt = SYSTEM_PROMPTS.get(agent_type)
        if prompt:
            return prompt
        return (
            f"You are a {agent_type} agent in a software development team. "
            "Perform the task described below."
        )

    def _build_user_message(self, context: BackendContext) -> str:
        """Build the user message from BackendContext fields."""
        parts: list[str] = []

        parts.append(f"## Task\n{context.node.task_description}")

        if context.artifacts:
            parts.append("\n## Input Artifacts")
            for artifact in context.artifacts:
                parts.append(f"\n### From {artifact.from_agent}")
                if artifact.content:
                    parts.append(artifact.content)
                if artifact.file_paths:
                    parts.append("Files: " + ", ".join(artifact.file_paths))

        if context.memory_prompt:
            parts.append(f"\n## Relevant Memory\n{context.memory_prompt}")

        if context.project_context:
            parts.append(f"\n## Project Context\n{context.project_context}")

        project_root = context.workspace_path or str(Path.cwd())
        parts.append(
            "\n## Runtime Environment\n"
            f"- OS: {platform.system()} {platform.release()}\n"
            f"- CWD: {Path.cwd().resolve()}\n"
            f"- PROJECT_ROOT: {Path(project_root).resolve()}\n"
            f"- PYTHON: {sys.executable}\n"
        )

        node = context.node
        if node.eval_feedback:
            parts.append(
                f"\n## Evaluation Feedback (from previous attempt)\n"
                f"{node.eval_feedback}"
            )

        if node.auto_eval_result:
            parts.append(
                f"\n## Automated Evaluation Results\n"
                f"```json\n{json.dumps(node.auto_eval_result, indent=2)}\n```"
            )

        return "\n".join(parts)

    async def execute(self, context: BackendContext) -> BackendResult:
        """Execute via LightweightLLMCaller -- single-shot LLM call.

        Re-raises exceptions (PendingApprovalError, RateLimitError, etc.)
        so NodeExecutor's retry/timeout/cancellation logic works unchanged.
        """
        system_prompt = self._get_system_prompt(context.node.agent_type)
        user_message = self._build_user_message(context)

        response_text = await self._lightweight_caller.call(
            system_prompt=system_prompt,
            user_message=user_message,
            session_id=self._session_id or context.session_id,
            cancel_event=context.cancel_event,
            agent_type=context.node.agent_type,
        )

        if not response_text:
            logger.warning(
                "LightweightLLMCaller returned empty response for node %s (agent_type=%s)",
                context.node.id, context.node.agent_type,
            )

        return BackendResult(
            status=BackendStatus.COMPLETED,
            summary=response_text[:200] if response_text else "",
            output=response_text or "",
            metadata={"token_usage": dict(self._lightweight_caller.token_usage)},
        )

    async def health_check(self) -> bool:
        """Builtin backend is always available."""
        return True

    def get_capabilities(self) -> list[str]:
        """Supports all agent types."""
        return []

    @property
    def name(self) -> str:
        return "builtin"
```

**Step 2: Commit**

```bash
git add agent/backends/builtin.py
git commit -m "refactor: de-pool BuiltinBackend, lightweight-only (M7.2.5 step 1)

Remove pool parameter, _execute_pool(), _ensure_closure().
BuiltinBackend now only uses LightweightLLMCaller.
Generator nodes must use external backends via BackendRegistry."
```

---

## Task 2: 移除 BackendRegistry.from_pool() 工厂方法

**Files:**
- Modify: `agent/backends/registry.py`

**Step 1: Remove the from_pool classmethod**

In `agent/backends/registry.py`:

1. Remove the `from_pool` classmethod
2. Remove the `BuiltinBackend` import (only needed by `from_pool`)

The file becomes:

```python
"""BackendRegistry -- manages AgentBackend instances with fallback."""
from __future__ import annotations

import logging
from typing import Any

from core.backend_models import BackendContext, BackendResult
from agent.backends.base import AgentBackend

logger = logging.getLogger(__name__)


class BackendRegistry:
    """Registry of AgentBackend instances with automatic fallback.

    Built-in backend is always available as the fallback.
    External backends are registered by name.
    get_backend() returns the requested backend if healthy,
    otherwise degrades to BuiltinBackend.
    """

    def __init__(self, builtin: AgentBackend) -> None:
        self._backends: dict[str, AgentBackend] = {}
        self._builtin = builtin
        self._backends["builtin"] = self._builtin

    def register(self, name: str, backend: AgentBackend) -> None:
        """Register a backend instance by name."""
        self._backends[name] = backend

    def get_backend(self, name: str) -> AgentBackend:
        """Get a backend by name with automatic fallback to builtin."""
        if name == "builtin":
            return self._builtin

        backend = self._backends.get(name)
        if backend is None:
            logger.warning("Backend '%s' not registered, falling back to builtin", name)
            return self._builtin
        return backend

    async def execute_for_node(
        self,
        backend_name: str,
        context: BackendContext,
    ) -> BackendResult:
        """Execute via the named backend with fallback on failure.

        If the requested backend is unhealthy, degrades to BuiltinBackend.
        Execution errors are not caught here -- they propagate to
        NodeExecutor's retry/timeout logic.
        """
        backend = self.get_backend(backend_name)

        if backend_name != "builtin" and backend is not self._builtin:
            try:
                healthy = await backend.health_check()
                if not healthy:
                    logger.warning(
                        "Backend '%s' unhealthy, falling back to builtin",
                        backend_name,
                    )
                    backend = self._builtin
            except Exception as exc:
                logger.warning(
                    "Backend '%s' health check failed (%s), falling back to builtin",
                    backend_name, exc,
                )
                backend = self._builtin

        return await backend.execute(context)
```

**Step 2: Commit**

```bash
git add agent/backends/registry.py
git commit -m "refactor: remove BackendRegistry.from_pool() (M7.2.5 step 2)

Callers now construct BuiltinBackend explicitly and pass to BackendRegistry."
```

---

## Task 3: 重构 execution_factory.py — 移除 AgentPool

**Files:**
- Modify: `control_plane/execution_factory.py`

**Step 1: Remove AgentPool import and usage**

In `control_plane/execution_factory.py`:

1. Remove line: `from agent.agent_pool import AgentPool`
2. Add import at top: `from core.exceptions import AgentExecutionError` (if not already imported)
3. Replace the `AgentPool(...)` construction block with direct `BuiltinBackend` construction
4. Replace `pool.get_executor(session_id)` in `DAGExecutionEngine(agent_executor=...)` with an error-raising async function
5. The error function makes it clear that the legacy path is gone:

```python
# M7.2.5: Legacy agent_executor removed. When backend_registry is active,
# this is never called. If it IS called, something is misconfigured.
async def _removed_agent_executor(node, artifacts, **kwargs):
    raise AgentExecutionError(
        "Legacy agent_executor called after M7.2.5 removal. "
        "Ensure backend_registry is configured in DAGEngineConfig."
    )
```

Then construct BuiltinBackend directly:

```python
# M6.3: Create LightweightLLMCaller for planner/evaluator single-shot calls
lightweight_caller = LightweightLLMCaller(
    config=self._llm_config,
    session_store=store,
    llm_router=getattr(self, "_llm_router", None),
)

# M7.2.5: BuiltinBackend is lightweight-only (no pool/tool-loop fallback)
builtin_backend = BuiltinBackend(
    lightweight_caller=lightweight_caller,
    session_store=store,
    session_id=session_id,
)
backend_registry = BackendRegistry(builtin=builtin_backend)
```

And in the `DAGExecutionEngine(...)` call, replace `agent_executor=pool.get_executor(session_id)` with `agent_executor=_removed_agent_executor`.

**Step 2: Commit**

```bash
git add control_plane/execution_factory.py
git commit -m "refactor: remove AgentPool from ExecutionFactory (M7.2.5 step 3)

Construct BuiltinBackend directly with LightweightLLMCaller.
agent_executor parameter is a no-op error function (backend_registry path is always used)."
```

---

## Task 4: 重构 cli/execution.py — 移除 AgentPool

**Files:**
- Modify: `cli/execution.py`

**Step 1: Remove AgentPool import and usage**

In `cli/execution.py`:

1. Remove line: `from agent.agent_pool import AgentPool`
2. Add imports:
   ```python
   from agent.backends.builtin import BuiltinBackend
   from agent.lightweight_llm_caller import LightweightLLMCaller
   from core.exceptions import AgentExecutionError
   ```
3. Replace the "Agent pool" construction block with:
   ```python
   # M7.2.5: No AgentPool — use LightweightLLMCaller + BuiltinBackend
   approval_repo = ApprovalRepository()
   lightweight_caller = LightweightLLMCaller(
       config=config.llm,
       session_store=store,
       llm_router=llm_router,
   )
   ```
4. Replace `BackendRegistry.from_pool(pool=pool, session_id=session_id)` with:
   ```python
   builtin_backend = BuiltinBackend(
       lightweight_caller=lightweight_caller,
       session_store=store,
       session_id=session_id,
   )
   backend_registry = BackendRegistry(builtin=builtin_backend)
   ```
5. Add the error-raising executor and use it in `DAGExecutionEngine(...)`:
   ```python
   async def _removed_agent_executor(node, artifacts, **kwargs):
       raise AgentExecutionError(
           "Legacy agent_executor called after M7.2.5 removal."
       )
   ```
   Replace `agent_executor=pool.get_executor(session_id)` with `agent_executor=_removed_agent_executor`.
6. Remove `"pool": pool` from the return dict. Change to `"pool": None` or remove key.

**Step 2: Commit**

```bash
git add cli/execution.py
git commit -m "refactor: remove AgentPool from CLI execution (M7.2.5 step 4)

Construct BuiltinBackend directly. CLI uses BackendRegistry for all node execution."
```

---

## Task 5: 清理 prompts.py — 移除 TOOL_ALLOWLIST

**Files:**
- Modify: `agent/prompts.py`

**Step 1: Remove TOOL_ALLOWLIST and update module docstring**

In `agent/prompts.py`:

1. Update module docstring to remove deprecation markers:

```python
"""Agent system prompts for BuiltinBackend.

Provides system prompts for planner, generator, and evaluator agent types.
Used by BuiltinBackend (lightweight LLM calls) and orchestrator/planner.

Tool allowlists were removed in M7.2.5 (tool filtering is now handled
by external backends internally).
"""
```

2. Remove the `TOOL_ALLOWLIST` dict (the last 5 lines of the file)

**Step 2: Commit**

```bash
git add agent/prompts.py
git commit -m "refactor: remove TOOL_ALLOWLIST from prompts.py (M7.2.5 step 5)

TOOL_ALLOWLIST was only used by agent_pool.py (now removed).
SYSTEM_PROMPTS retained for BuiltinBackend and orchestrator."
```

---

## Task 6: 删除废弃源文件

**Files:**
- Delete: `agent/agent_pool.py`
- Delete: `agent/worker.py`
- Delete: `guardrails/output_monitor.py`
- Delete: `core/stuck_detector.py`

**Step 1: Delete all four files**

```bash
git rm agent/agent_pool.py agent/worker.py guardrails/output_monitor.py core/stuck_detector.py
```

**Step 2: Commit**

```bash
git commit -m "refactor: delete M6 deprecated modules (M7.2.5 steps 6-9)

- agent/agent_pool.py (702 lines): AgentPool, WorkerAgent, ExecutionContext
- agent/worker.py (646 lines): AgentWorker tool loop
- guardrails/output_monitor.py (197 lines): output injection scanner
- core/stuck_detector.py (173 lines): stuck pattern detection

Total: 1718 lines removed."
```

---

## Task 7: 删除纯废弃测试文件

**Files:**
- Delete ~20 test files that ONLY test deprecated modules

**Step 1: Delete pure-deprecated test files**

These files test ONLY AgentWorker/AgentPool/StuckDetector/OutputMonitor:

```bash
git rm tests/test_worker.py
git rm tests/test_stuck_detector.py
git rm tests/test_output_monitor.py
git rm tests/test_132_refactor.py
git rm tests/test_execution_context.py
git rm tests/test_parallel_artifact_isolation.py
git rm tests/test_empty_call_auto_retry.py
git rm tests/test_empty_tool_call_breaker.py
git rm tests/test_degenerate_empty_args.py
git rm tests/test_733_degenerate_recovery.py
git rm tests/test_739_tool_exec_progress.py
git rm tests/test_malformed_args_logging.py
git rm tests/test_tool_arg_validation.py
git rm tests/test_tool_call_id.py
git rm tests/test_worker_memory.py
git rm tests/test_file_path_constraints.py
git rm tests/test_cross_file_refactoring.py
git rm tests/test_naming_convention.py
git rm tests/test_runtime_context.py
git rm tests/test_retry_incremental_fix.py
```

**Step 2: Commit**

```bash
git commit -m "refactor: delete pure-deprecated test files (M7.2.5)

Remove ~20 test files that only tested AgentWorker/AgentPool/StuckDetector/OutputMonitor."
```

---

## Task 8: 更新混合测试文件

**Files:**
- Modify: `tests/test_injection_regression.py` — remove OutputMonitor test class
- Modify: `tests/test_evaluator_file_exists.py` — remove AgentWorker test class
- Modify: `tests/test_fault_tolerance_hemostasis.py` — remove AgentWorker test class
- Modify: `tests/test_edit_efficiency.py` — remove WorkerAgent references
- Modify: `tests/test_eval_dedup.py` — remove WorkerAgent references
- Modify: `tests/test_evaluator_file_contracts.py` — remove WorkerAgent references
- Modify: `tests/test_feature_complexity_decomposition.py` — remove WorkerAgent references
- Modify: `tests/test_retry_naming_guidance.py` — remove WorkerAgent references
- Modify: `tests/test_m6_4_cleanup.py` — remove OutputMonitor/StuckDetector test methods
- Modify: `tests/test_execution_factory.py` — replace AgentPool mocks with BuiltinBackend verification
- Modify: `tests/test_llm_dag_integration.py` — replace AgentPool with BuiltinBackend
- Modify: `tests/test_m46_integration.py` — verify no deprecated references
- Modify: `tests/test_922_pydantic_future_annotations.py` — verify no deprecated references

**Step 1: For each mixed test file, read and update**

Strategy per file:
- Read the file to identify which test classes/functions use deprecated imports
- Remove only those test classes/functions (keep non-deprecated tests intact)
- If removing all deprecated tests makes the file empty, delete it instead
- Update imports to remove deprecated module references

**Step 2: For test_execution_factory.py, update AgentPool mocks**

Replace `@patch("control_plane.execution_factory.AgentPool")` patterns with verification that `BuiltinBackend` is constructed correctly. The tests should verify:
- `BuiltinBackend` is created with `lightweight_caller`
- `BackendRegistry` wraps the builtin
- No `AgentPool` reference exists

**Step 3: Commit**

```bash
git add tests/
git commit -m "refactor: update mixed test files, remove deprecated references (M7.2.5)

- Remove deprecated test classes from mixed files
- Update execution_factory tests to verify BuiltinBackend
- Clean up imports"
```

---

## Task 9: 清理注释和 docstring

**Files:**
- Modify: `core/node_executor.py` — update comment about agent_pool/worker
- Modify: `core/context.py` — update comment about AgentWorker
- Modify: `core/backend_models.py` — update comment about AgentPool
- Modify: `core/activity_detector.py` — update comment about stuck_detector
- Modify: `core/llm_client.py` — update comment about AgentWorker
- Modify: `core/exceptions.py` — update comment about AgentWorker.run()
- Modify: `guardrails/node_isolation.py` — update comment about output_monitor

**Step 1: Search for remaining text references**

```bash
grep -rn "AgentPool\|AgentWorker\|output_monitor\|stuck_detector\|agent_pool\|worker\.py" --include="*.py" .
```

For each hit:
- If it's an import: remove it (should be none after Tasks 1-8)
- If it's a comment/docstring: update to reflect current architecture
- If it's in `__init__.py` or re-exports: remove the re-export

**Step 2: Commit**

```bash
git add .
git commit -m "refactor: clean up deprecated references in comments/docstrings (M7.2.5)"
```

---

## Task 10: 全量验证

**Step 1: Run grep verification**

```bash
grep -rn "AgentPool\|AgentWorker\|OutputMonitor\|StuckDetector\|TOOL_ALLOWLIST\|from_pool" --include="*.py" .
```

Expected: only comments/docstrings about historical context, or intentionally kept references.

**Step 2: Run full test suite**

```bash
python -m pytest -v --tb=short
```

Expected: All tests PASS.

**Step 3: Run lint**

```bash
flake8 --max-line-length=100
```

Expected: No new errors.

**Step 4: Count removed lines**

```bash
git diff --stat main
```

Expected: net removal of ~1718+ source lines + ~3000+ test lines.

**Step 5: Final commit (if any fixes needed)**

```bash
git add .
git commit -m "fix: address test/lint issues from M7.2.5 removal"
```

---

## Rollback Plan

If critical issues are discovered post-merge:
1. `git revert` the merge commit
2. All four deleted files are in git history (can be restored individually)
3. The original `BuiltinBackend` with pool support is in the parent commit
