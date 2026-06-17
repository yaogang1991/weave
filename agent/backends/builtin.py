"""BuiltinBackend -- wraps LightweightLLMCaller for single-shot node execution."""
from __future__ import annotations

import asyncio
import contextlib
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

# Sentinel to distinguish "no lightweight_caller provided" from "None passed"
_UNSET = object()


class BuiltinBackend(AgentBackend):
    """Agent backend that uses LightweightLLMCaller for single-shot node execution.

    Since M7.2.5 the BuiltinBackend is lightweight-only: planner/evaluator
    nodes run via single-shot LLM calls (no tool loop). Generator nodes
    execute through external backends (claude_code/codex) via the
    BackendRegistry; when an external backend is unavailable the registry
    falls back here, where the lightweight path produces a text response
    (no file writes). NodeExecutor's quality gate (zero-output detection)
    and DAGEngine's ``adapt_to_failure()`` recover from that degraded path.
    """

    # #1122: Interval between heartbeats emitted while awaiting an LLM
    # response in the lightweight path. Must stay well below the node's
    # stall_timeout (default ~120s) so the StallDetector does not kill
    # planner/annotator/evaluator nodes during long LLM calls.
    _HEARTBEAT_INTERVAL_SEC = 15.0

    def __init__(
        self,
        lightweight_caller: Any = _UNSET,
        session_store: Any = None,
        session_id: str = "",
    ) -> None:
        self._lightweight_caller = lightweight_caller
        self._session_store = session_store
        self._session_id = session_id

    def _get_system_prompt(self, agent_type: str) -> str:
        """Resolve the system prompt for a given agent type."""
        prompt = SYSTEM_PROMPTS.get(agent_type)
        if prompt:
            return prompt
        # Fallback for unknown agent types
        return (
            f"You are a {agent_type} agent in a software development team. "
            "Perform the task described below."
        )

    def _build_user_message(self, context: BackendContext) -> str:
        """Build the user message from BackendContext fields."""
        parts: list[str] = []

        # Primary task
        parts.append(f"## Task\n{context.node.task_description}")

        # Input artifacts
        if context.artifacts:
            parts.append("\n## Input Artifacts")
            for artifact in context.artifacts:
                parts.append(f"\n### From {artifact.from_agent}")
                if artifact.content:
                    parts.append(artifact.content)
                if artifact.file_paths:
                    parts.append(
                        "Files: " + ", ".join(artifact.file_paths)
                    )

        # Memory prompt
        if context.memory_prompt:
            parts.append(f"\n## Relevant Memory\n{context.memory_prompt}")

        # Project context
        if context.project_context:
            parts.append(f"\n## Project Context\n{context.project_context}")

        # Runtime environment context
        project_root = context.workspace_path or str(Path.cwd())
        parts.append(
            "\n## Runtime Environment\n"
            f"- OS: {platform.system()} {platform.release()}\n"
            f"- CWD: {Path.cwd().resolve()}\n"
            f"- PROJECT_ROOT: {Path(project_root).resolve()}\n"
            f"- PYTHON: {sys.executable}\n"
        )

        # Eval feedback (for retries)
        node = context.node
        if node.eval_feedback:
            parts.append(
                f"\n## Evaluation Feedback (from previous attempt)\n"
                f"{node.eval_feedback}"
            )

        # Auto-eval result (for downstream agents)
        if node.auto_eval_result:
            parts.append(
                f"\n## Automated Evaluation Results\n"
                f"```json\n{json.dumps(node.auto_eval_result, indent=2)}\n```"
            )

        return "\n".join(parts)

    def _start_heartbeat(self, context: BackendContext) -> asyncio.Task | None:
        """Start a periodic heartbeat task tied to the node's progress_callback.

        #1122: The lightweight path awaits a single LLM call that can take
        longer than ``stall_timeout`` on non-native APIs. Without periodic
        heartbeats the StallDetector force-kills the node before the
        response completes (evidence: ``input_tokens=0, output_tokens=0``).

        Returns ``None`` when no ``progress_callback`` is wired (backward
        compat), leaving behavior unchanged for callers that don't supply one.
        """
        cb = context.progress_callback
        if cb is None:
            return None

        async def _loop():
            while True:
                await asyncio.sleep(self._HEARTBEAT_INTERVAL_SEC)
                try:
                    cb()
                except Exception:
                    logger.debug("Heartbeat callback failed", exc_info=True)

        return asyncio.create_task(_loop())

    async def _execute_lightweight(self, context: BackendContext) -> BackendResult:
        """Execute via LightweightLLMCaller -- single-shot LLM call."""
        system_prompt = self._get_system_prompt(context.node.agent_type)
        user_message = self._build_user_message(context)

        # #1122: keep the StallDetector alive across long LLM calls by
        # emitting periodic heartbeats via the node's progress_callback.
        heartbeat_task = self._start_heartbeat(context)
        try:
            response_text = await self._lightweight_caller.call(
                system_prompt=system_prompt,
                user_message=user_message,
                session_id=self._session_id or context.session_id,
                cancel_event=context.cancel_event,
                agent_type=context.node.agent_type,
            )
        finally:
            if heartbeat_task is not None:
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task

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

    async def execute(self, context: BackendContext) -> BackendResult:
        """Execute via LightweightLLMCaller (single-shot LLM call).

        BuiltinBackend is lightweight-only since M7.2.5: planner/evaluator
        nodes run via single-shot LLM calls; generator nodes execute through
        external backends (claude_code/codex) via the BackendRegistry. When an
        external backend is unavailable the registry degrades to this backend,
        where the lightweight path produces a text response for the node.

        Re-raises exceptions (PendingApprovalError, RateLimitError, etc.)
        so NodeExecutor's retry/timeout/cancellation logic works unchanged.
        """
        if self._lightweight_caller is _UNSET or self._lightweight_caller is None:
            raise AgentExecutionError(
                "BuiltinBackend: no lightweight_caller configured. "
                "Generator nodes require an external backend (claude_code/codex) "
                "via BackendRegistry; planner/evaluator nodes require a "
                "LightweightLLMCaller."
            )
        return await self._execute_lightweight(context)

    async def health_check(self) -> bool:
        """Builtin backend is always available."""
        return True

    def get_capabilities(self) -> list[str]:
        """Supports all agent types."""
        return []

    @property
    def name(self) -> str:
        return "builtin"
