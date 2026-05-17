"""
Core Models for Intelligent Multi-Agent Orchestration.

This module re-exports all models from domain-specific sub-modules.
All ``from core.models import X`` statements continue to work unchanged.

Domain modules:
    core.eval_models      -- Evaluation criteria and results
    core.tool_models      -- Tool calls, results, agent messages
    core.dag_models       -- DAG nodes, edges, graphs, orchestration
    core.event_models     -- Events, session state, session metrics
    core.guardrail_models -- Guardrail policies and permissions
    core.memory_models    -- Memory entries and learning insights
    core.analysis_models  -- DAG templates and impact analysis
    core.mcp_models       -- MCP server and skill definitions
"""

from core.eval_models import (
    CriterionType,
    EvalStatus,
    EvaluationResult,
    SuccessCriterion,
)
from core.tool_models import (
    AgentMessage,
    ToolCall,
    ToolResult,
)
from core.dag_models import (
    AgentCapability,
    ConflictResolution,
    DAG,
    DAGEdge,
    DAGNode,
    DependencyType,
    ExecutionEvent,
    FailureDecision,
    FileAccessPolicy,
    FileOwnershipContract,
    HandoffArtifact,
    NodeHealth,
    NodeStatus,
    NodeWorkspace,
    NodeWorkspaceResult,
    NodeWorkspaceStrategy,
    OrchestratorPlan,
)
from core.event_models import (
    Event,
    EventType,
    SessionMetrics,
    SessionState,
)
from core.guardrail_models import (
    GuardrailPolicy,
    PermissionMode,
    PersonalGuardrailPolicy,
    RiskLevel,
)
from core.memory_models import (
    InsightType,
    LearningCategory,
    LearningInsight,
    MemoryEntry,
    MemoryScope,
    MemoryType,
)
from core.analysis_models import (  # noqa: F401 — backward-compat re-export
    DAGTemplate,
    ImpactRiskLevel,
    ImpactScope,
    VerificationResult,
)
from core.mcp_models import (
    MCPServerStatus,
    MCPToolInfo,
    Skill,
    SkillVariable,
)

__all__ = [
    "AgentCapability",
    "AgentMessage",
    "ConflictResolution",
    "CriterionType",
    "DAG",
    "DAGEdge",
    "DAGNode",
    "DAGTemplate",
    "DependencyType",
    "EvalStatus",
    "EvaluationResult",
    "Event",
    "EventType",
    "ExecutionEvent",
    "FailureDecision",
    "FileAccessPolicy",
    "FileOwnershipContract",
    "GuardrailPolicy",
    "HandoffArtifact",
    "ImpactRiskLevel",
    "ImpactScope",
    "InsightType",
    "LearningCategory",
    "LearningInsight",
    "MCPServerStatus",
    "MCPToolInfo",
    "MemoryEntry",
    "MemoryScope",
    "MemoryType",
    "NodeHealth",
    "NodeStatus",
    "NodeWorkspace",
    "NodeWorkspaceResult",
    "NodeWorkspaceStrategy",
    "OrchestratorPlan",
    "PermissionMode",
    "PersonalGuardrailPolicy",
    "RiskLevel",
    "SessionMetrics",
    "SessionState",
    "Skill",
    "SkillVariable",
    "SuccessCriterion",
    "ToolCall",
    "ToolResult",
]
