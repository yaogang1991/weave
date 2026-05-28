"""BackendRegistry -- manages AgentBackend instances with fallback."""
from __future__ import annotations

import logging
from typing import Any

from core.backend_models import BackendContext, BackendResult
from agent.backends.base import AgentBackend
from agent.backends.builtin import BuiltinBackend

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

    @classmethod
    def from_pool(
        cls,
        pool: Any,
        session_id: str = "",
        lightweight_caller: Any = None,
        session_store: Any = None,
    ) -> BackendRegistry:
        """Backward-compatible factory: create a BackendRegistry with a BuiltinBackend.

        Use this during gradual migration from the old (pool, session_id) constructor.
        New code should prefer constructing a BuiltinBackend explicitly and passing
        it to ``BackendRegistry(builtin=...)`` directly.
        """
        builtin = BuiltinBackend(
            lightweight_caller=lightweight_caller,
            session_store=session_store,
            session_id=session_id,
            pool=pool,
        )
        return cls(builtin=builtin)

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
        Execution errors are not caught here — they propagate to
        NodeExecutor's retry/timeout logic.
        """
        backend = self.get_backend(backend_name)

        # Health check for non-builtin backends
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
