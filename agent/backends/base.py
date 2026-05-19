"""AgentBackend abstract base class."""
from __future__ import annotations

import abc

from core.backend_models import BackendContext, BackendResult


class AgentBackend(abc.ABC):
    """Abstract interface for agent execution backends.

    Each backend encapsulates a strategy for executing a DAG node.
    The built-in backend wraps the existing AgentPool + WorkerAgent loop.
    Future backends may delegate to external agent runtimes.

    The backend does NOT manage:
    - Workspace isolation (handled by BackendManager)
    - Evaluation (handled by EvaluatorEngine)
    - Retry logic (handled by NodeExecutor)
    - Timeout enforcement (handled by NodeExecutor)
    """

    @abc.abstractmethod
    async def execute(self, context: BackendContext) -> BackendResult:
        """Execute a task and return the result.

        Args:
            context: Contains the DAGNode, input artifacts, session info,
                cancel_event, progress_callback, and workspace_path.

        Returns:
            BackendResult with status, summary, artifacts, and output.
        """
        ...

    @abc.abstractmethod
    async def health_check(self) -> bool:
        """Check if this backend is available and healthy.

        Called by BackendRegistry before execution. External backends
        may return False when their service is unreachable.
        """
        ...

    @abc.abstractmethod
    def get_capabilities(self) -> list[str]:
        """Return list of agent types this backend supports.

        Empty list means "supports all agent types".
        """
        ...

    @property
    def name(self) -> str:
        """Human-readable backend name for logging."""
        return self.__class__.__name__
