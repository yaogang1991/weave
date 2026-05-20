"""AgentBackend domain models -- BackendResult, BackendContext, BackendStatus."""
from __future__ import annotations

import threading
from enum import Enum
from typing import Any, Callable

from pydantic import BaseModel, Field

from core.dag_models import HandoffArtifact


class BackendStatus(str, Enum):
    """Result status from an AgentBackend execution."""
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class BackendResult(BaseModel):
    """Unified result from any AgentBackend.

    Every backend returns this model. The DAG layer consumes it
    identically regardless of which backend produced it.
    """
    status: BackendStatus = BackendStatus.COMPLETED
    summary: str = ""
    artifacts: list[str] = Field(default_factory=list)
    output: str = ""
    error: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to the dict format expected by NodeExecutor.

        Includes token_usage and cost_usd from metadata when present (#612 #9).
        """
        result: dict[str, Any] = {
            "status": self.status.value,
            "summary": self.summary,
            "artifacts": self.artifacts,
            "output": self.output,
        }
        token_usage = self.metadata.get("token_usage")
        if token_usage:
            result["token_usage"] = token_usage
        cost_usd = self.metadata.get("cost_usd")
        if cost_usd is not None:
            result["cost_usd"] = cost_usd
        session_id = self.metadata.get("session_id")
        if session_id:
            result["session_id"] = session_id
        return result


class BackendContext(BaseModel):
    """Context passed to every AgentBackend.execute() call.

    Carries everything a backend needs to execute a node, without
    leaking internal details like SandboxProvider or ToolRegistry.
    """
    # Full DAGNode — typed as Any to avoid core <-> agent circular import.
    # BuiltinBackend passes it through to the AgentPool executor closure.
    node: Any
    artifacts: list[HandoffArtifact] = Field(default_factory=list)
    session_id: str = ""
    workspace_path: str | None = None
    job_id: str = ""
    run_id: str | None = None

    model_config = {"arbitrary_types_allowed": True}

    cancel_event: threading.Event | None = None
    progress_callback: Callable[[], None] | None = None
    progress_tracker: Any | None = None  # M4.5: ProgressTracker for progress-driven timeout
