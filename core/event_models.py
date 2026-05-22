"""Event and session domain models."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

from core.tool_models import AgentMessage


class EventType(str, Enum):
    """Event types following {domain}.{action} convention."""
    # User events
    USER_MESSAGE = "user.message"
    USER_COMMAND = "user.command"

    # Agent events
    AGENT_MESSAGE = "agent.message"
    AGENT_TOOL_USE = "agent.tool_use"
    AGENT_TOOL_RESULT = "agent.tool_result"
    AGENT_ERROR = "agent.error"

    # Session events
    SESSION_START = "session.status_start"
    SESSION_DAG = "session.dag"
    SESSION_IDLE = "session.status_idle"
    SESSION_RUNNING = "session.status_running"
    SESSION_ERROR = "session.status_error"
    SESSION_END = "session.status_end"

    # Workflow events
    WORKFLOW_STAGE_START = "workflow.stage_start"
    WORKFLOW_STAGE_END = "workflow.stage_end"
    WORKFLOW_STAGE_ERROR = "workflow.stage_error"

    # Tool events
    TOOL_EXEC_START = "tool.exec_start"
    TOOL_EXEC_END = "tool.exec_end"
    TOOL_EXEC_ERROR = "tool.exec_error"

    # Evaluation events
    EVAL_START = "eval.start"
    EVAL_RESULT = "eval.result"
    EVAL_CONTRACT_CHECK = "eval.contract_check"
    EVAL_AUTOFIX_APPLIED = "eval.autofix_applied"

    # Checkpoint events
    CHECKPOINT_CREATED = "checkpoint.created"
    CHECKPOINT_RESTORED = "checkpoint.restored"

    # Memory events (M3.2)
    MEMORY_STORED = "memory.stored"
    MEMORY_ACCESSED = "memory.accessed"
    MEMORY_SHARED = "memory.shared"
    MEMORY_EXPIRED = "memory.expired"
    MEMORY_PRUNED = "memory.pruned"

    # Learning events (M3.3)
    LEARNING_ANALYSIS_START = "learning.analysis_start"
    LEARNING_INSIGHT_GENERATED = "learning.insight_generated"
    LEARNING_OPTIMIZATION_APPLIED = "learning.optimization_applied"

    # Impact analysis events (M3.5)
    IMPACT_PREDICTED = "impact.predicted"
    IMPACT_VERIFIED = "impact.verified"
    IMPACT_MISMATCH = "impact.mismatch"
    IMPACT_LEARNED = "impact.learned"

    # MCP events (M3.6)
    MCP_SERVER_CONNECTED = "mcp.server_connected"
    MCP_SERVER_DISCONNECTED = "mcp.server_disconnected"
    MCP_SERVER_ERROR = "mcp.server_error"
    MCP_TOOL_DISCOVERED = "mcp.tool_discovered"

    # Budget events (M4.2)
    BUDGET_WARNING = "budget.warning"
    BUDGET_EXHAUSTED = "budget.exhausted"
    AGENT_STUCK = "agent.stuck"

    # Degeneration events
    DEGENERATION_RECOVERED = "degeneration_recovered"


class Event(BaseModel):
    """Immutable event in the session log."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    type: EventType
    session_id: str
    payload: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionMetrics(BaseModel):
    """Runtime metrics for a session."""
    total_events: int = 0
    total_tool_calls: int = 0
    total_tokens_input: int = 0
    total_tokens_output: int = 0
    total_duration_ms: int = 0
    stage_durations: dict[str, int] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)


class SessionState(BaseModel):
    """Recoverable session state derived from event log."""
    session_id: str
    created_at: datetime
    status: Literal["created", "running", "idle", "error", "completed"] = "created"
    current_stage: str | None = None
    stages_completed: list[str] = Field(default_factory=list)
    artifacts: dict[str, str] = Field(default_factory=dict)
    context_window: list[AgentMessage] = Field(default_factory=list)
    metrics: SessionMetrics = Field(default_factory=SessionMetrics)
