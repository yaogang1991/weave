"""
Core Pydantic models for the Unattended Software Development Harness.
Based on Anthropic Managed Agents design principles:
- Artifact-centric: all state is serializable
- Event-sourced: append-only event log
- Minimal: only essential fields
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class EventType(str, Enum):
    """Event types following Anthropic's {domain}.{action} convention."""
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
    
    # Checkpoint events
    CHECKPOINT_CREATED = "checkpoint.created"
    CHECKPOINT_RESTORED = "checkpoint.restored"


class Event(BaseModel):
    """Immutable event in the session log."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    type: EventType
    session_id: str
    payload: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolCall(BaseModel):
    """A tool call from the agent."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    

class ToolResult(BaseModel):
    """Result of a tool execution."""
    tool_call_id: str
    success: bool
    output: str = ""
    error: str = ""
    duration_ms: int = 0


class AgentMessage(BaseModel):
    """Message from/to the agent."""
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None


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
    artifacts: dict[str, str] = Field(default_factory=dict)  # artifact_name -> file_path
    context_window: list[AgentMessage] = Field(default_factory=list)
    metrics: SessionMetrics = Field(default_factory=SessionMetrics)


class WorkflowStage(BaseModel):
    """Definition of a workflow stage."""
    name: str
    agent: Literal["planner", "generator", "evaluator"]
    input_artifacts: list[str] = Field(default_factory=list)
    output_artifacts: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    max_iterations: int = 10
    auto_approve: bool = False


class WorkflowDefinition(BaseModel):
    """Full workflow definition loaded from YAML."""
    name: str
    description: str = ""
    stages: list[WorkflowStage]
    global_config: dict[str, Any] = Field(default_factory=dict)


class RiskLevel(int, Enum):
    """Risk classification for operations."""
    LOW = 1      # Read-only, safe
    MEDIUM = 2   # File edits, reversible
    HIGH = 3     # Bash commands, network access
    CRITICAL = 4 # Irreversible, production impact


class PermissionMode(str, Enum):
    """Permission modes inspired by Claude Code."""
    PLAN = "plan"                    # Read-only
    DEFAULT = "default"              # Ask for every action
    ACCEPT_EDITS = "accept_edits"    # Auto-approve file edits
    AUTO = "auto"                    # Classifier-based approval
    DONT_ASK = "dont_ask"            # Only pre-approved tools


class GuardrailPolicy(BaseModel):
    """Policy configuration for guardrails."""
    mode: PermissionMode = PermissionMode.DEFAULT
    allowed_tools: list[str] = Field(default_factory=list)
    denied_tools: list[str] = Field(default_factory=list)
    allowed_commands: list[str] = Field(default_factory=list)
    denied_commands: list[str] = Field(default_factory=list)
    max_bash_duration: int = 120  # seconds
    max_iterations: int = 50
    auto_approve_read: bool = True
    require_human_on_error: bool = True


class EvaluationResult(BaseModel):
    """Result of an evaluation pass."""
    passed: bool
    score: float = 0.0  # 0.0 - 10.0
    criteria_results: dict[str, bool] = Field(default_factory=dict)
    feedback: str = ""
    suggestions: list[str] = Field(default_factory=list)
