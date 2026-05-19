"""BuiltinBackend -- wraps the existing AgentPool.get_executor() closure."""
from __future__ import annotations

import logging
from typing import Any, Callable

from core.backend_models import BackendContext, BackendResult, BackendStatus
from agent.backends.base import AgentBackend

logger = logging.getLogger(__name__)


class BuiltinBackend(AgentBackend):
    """Wraps the existing AgentPool + WorkerAgent execution path.

    Provides 100% backward compatibility by delegating to the
    AgentPool.get_executor() closure, which handles per-node tool
    registry, guardrails, file ownership, and memory injection.
    """

    def __init__(self, pool: Any, session_id: str) -> None:
        self._pool = pool
        self._session_id = session_id
        self._executor_closure: Callable | None = None

    def _ensure_closure(self) -> Callable:
        """Lazily create the executor closure from AgentPool."""
        if self._executor_closure is None:
            self._executor_closure = self._pool.get_executor(self._session_id)
        return self._executor_closure

    async def execute(self, context: BackendContext) -> BackendResult:
        """Execute via the built-in AgentPool executor closure.

        Re-raises exceptions (PendingApprovalError, RateLimitError, etc.)
        so NodeExecutor's retry/timeout/cancellation logic works unchanged.
        """
        closure = self._ensure_closure()

        result_dict = await closure(
            context.node,
            context.artifacts,
            cancel_event=context.cancel_event,
            progress_callback=context.progress_callback,
            workspace_path=context.workspace_path,
        )
        if not result_dict:
            result_dict = {}
        return BackendResult(
            status=BackendStatus.COMPLETED,
            summary=result_dict.get("summary", ""),
            artifacts=result_dict.get("artifacts", []),
            output=result_dict.get("output", ""),
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
