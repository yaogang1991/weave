"""Protocol interfaces for cross-layer dependency inversion (#1085).

These Protocols allow core/ modules to type-hint dependencies from higher
layers (agent/, memory/, session/, guardrails/) without importing them,
preserving the core → agent → orchestrator → tools layering rule.

Each Protocol declares only the methods actually used by core/ callers.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from core.backend_models import BackendContext, BackendResult
from core.memory_models import MemoryEntry


# ---------------------------------------------------------------------------
# BackendRegistry (agent/backends/registry.py)
# ---------------------------------------------------------------------------

@runtime_checkable
class BackendRegistryProto(Protocol):
    """Minimal interface for backend execution used by NodeExecutor."""

    async def execute_for_node(
        self,
        backend_name: str,
        context: BackendContext,
    ) -> BackendResult: ...


# ---------------------------------------------------------------------------
# MemoryManager (memory/manager.py)
# ---------------------------------------------------------------------------

@runtime_checkable
class MemoryManagerProto(Protocol):
    """Minimal interface for memory retrieval used by NodeExecutor."""

    @property
    def config(self) -> MemoryConfigProto: ...

    def get_context_for_agent(
        self,
        agent_type: str,
        task_description: str,
        session_id: str | None = None,
    ) -> list[MemoryEntry]: ...

    def format_memory_prompt(self, entries: list[MemoryEntry]) -> str: ...


@runtime_checkable
class MemoryConfigProto(Protocol):
    """Minimal interface for MemoryManager.config."""

    enabled: bool


# ---------------------------------------------------------------------------
# SessionStore (session/store.py)
# ---------------------------------------------------------------------------

@runtime_checkable
class SessionStoreProto(Protocol):
    """Minimal interface for session event emission used by NodeExecutor."""

    def emit_event(
        self,
        session_id: str,
        event_type: object,  # EventType, but kept generic for flexibility
        payload: dict,
        metadata: dict | None = None,
    ) -> object: ...


# ---------------------------------------------------------------------------
# NodeGuardrails (guardrails/node_guardrails.py)
# ---------------------------------------------------------------------------

@runtime_checkable
class NodeGuardrailsProto(Protocol):
    """Minimal interface for node-level guardrail checks."""

    def pre_check(
        self,
        node: object,  # DAGNode, avoided circular import
        workspace_path: str | None = None,
    ) -> GuardrailResultProto: ...

    def post_check(
        self,
        artifacts: list[str],
        workspace_path: str | None = None,
    ) -> GuardrailResultProto: ...


@runtime_checkable
class GuardrailResultProto(Protocol):
    """Minimal interface for guardrail check results."""

    decision: str
    reason: str

    @property
    def is_blocked(self) -> bool: ...


# ---------------------------------------------------------------------------
# BackendManager (backend/lifecycle.py)
# ---------------------------------------------------------------------------

@runtime_checkable
class BackendManagerProto(Protocol):
    """Minimal interface for workspace lifecycle used by NodeExecutor."""

    def setup_node(
        self,
        job_id: str,
        run_id: str,
        node_id: str,
        strategy: str = "shared",
    ) -> object: ...  # Returns NodeWorkspace

    def cleanup_node(
        self,
        job_id: str,
        run_id: str,
        node_id: str,
    ) -> None: ...

    def cleanup_node_artifacts(
        self,
        job_id: str,
        run_id: str,
        node_id: str,
    ) -> list[str]: ...


# ---------------------------------------------------------------------------
# EvaluatorEngine (evaluator/engine.py)
# ---------------------------------------------------------------------------

@runtime_checkable
class EvaluatorEngineProto(Protocol):
    """Minimal interface for evaluation used by EvaluationPipeline."""

    async def evaluate(
        self,
        node: object,  # DAGNode
        workspace_path: str | None = None,
        extra_criteria: list | None = None,
    ) -> object: ...  # Returns EvaluationResult
