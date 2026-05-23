"""DAG execution domain models -- nodes, edges, graphs, and orchestration artifacts."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from core.eval_models import SuccessCriterion


class AgentCapability(BaseModel):
    """
    Description of a Worker Agent's capabilities.
    Registered in AgentRegistry, consumed by IntelligentOrchestrator.
    """
    id: str
    name: str
    description: str
    skills: list[str] = Field(default_factory=list)
    input_schema: list[str] = Field(default_factory=list)
    output_schema: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    system_prompt: str = ""  # Optional custom system prompt for this agent


class NodeStatus(str, Enum):
    """Execution status of a DAG node."""
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL_PASS = "partial_pass"  # Passed via threshold (soft failures overridden)
    WARNED = "warned"              # Passed but has uncheckable/warned criteria
    FAILED = "failed"
    SKIPPED = "skipped"
    SUPERSEDED = "superseded"  # Replaced by replan (#789)
    RETRYING = "retrying"
    PENDING_APPROVAL = "pending_approval"


class NodeHealth(str, Enum):
    """Health status of a running DAG node -- M2.0 heartbeat protocol."""
    HEALTHY = "healthy"       # Heartbeat within threshold
    MISSED = "missed"         # Last heartbeat > threshold but < kill_threshold
    UNHEALTHY = "unhealthy"   # Confirmed unhealthy (miss_threshold exceeded)
    DEAD = "dead"             # Killed by watchdog, final state


# ---------------------------------------------------------------------------
# Workspace isolation models (#176)
# ---------------------------------------------------------------------------

class NodeWorkspaceStrategy(str, Enum):
    """Workspace isolation strategy for a DAG node."""
    SHARED = "shared"       # Share the run's work_dir (default, current behavior)
    WORKTREE = "worktree"   # Git worktree per node
    COPY = "copy"           # File copy per node (non-git fallback)


class NodeWorkspace(BaseModel):
    """Workspace information for a DAG node.

    Created by BackendManager.setup_node(), consumed by DAGExecutionEngine
    and evaluator to route file operations to the correct directory.
    """
    node_id: str
    strategy: NodeWorkspaceStrategy = NodeWorkspaceStrategy.SHARED
    base_path: str = ""             # The run's shared work_dir
    workspace_path: str = ""        # This node's isolated workspace (same as base_path if SHARED)
    baseline_commit: str = ""       # Git commit SHA at workspace creation time (for delta lint)


class NodeWorkspaceResult(BaseModel):
    """Result of node execution in its workspace.

    Collected by BackendManager after node completes, used for merging
    node outputs back into the shared workspace.
    """
    node_id: str
    changed_files: list[str] = Field(default_factory=list)
    patch_content: str = ""         # Unified diff patch of node's changes
    merge_status: Literal["pending", "merged", "conflict"] = "pending"
    conflicts: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# File ownership contract models (#272)
# ---------------------------------------------------------------------------

class FileAccessPolicy(str, Enum):
    """File access classification for ownership contracts."""
    OWNED = "owned"          # This node exclusively creates/writes this file
    FORBIDDEN = "forbidden"  # Another node owns this file; read-only for this node
    SHARED = "shared"        # Multiple nodes may coordinate (requires merge node or serialization)


class ConflictResolution(str, Enum):
    """How to resolve a parallel write conflict."""
    SERIALIZE = "serialize"     # Add implicit edge to serialize the conflicting nodes
    MERGE_NODE = "merge_node"   # Insert a merge node that reconciles outputs
    ERROR = "error"             # Raise PlanValidationError -- manual fix required
    REASSIGN = "reassign"       # Reassign file ownership to a single node


class FileOwnershipContract(BaseModel):
    """Declares which files a DAG node intends to create or modify.

    Populated by the planner in each node's task definition, validated
    by PlanValidator for conflicts, and enforced at execution time by
    the DAG engine and tool registry.
    """
    node_id: str
    owned_files: list[str] = Field(default_factory=list)
    forbidden_files: list[str] = Field(default_factory=list)
    shared_files: list[str] = Field(default_factory=list)
    access_policy: dict[str, FileAccessPolicy] = Field(default_factory=dict)


class DAGNode(BaseModel):
    """
    A single node in the execution DAG = one agent task.
    """
    id: str
    agent_type: str           # References AgentCapability.id
    task_description: str
    status: NodeStatus = NodeStatus.PENDING
    result: dict[str, Any] = Field(default_factory=dict)
    error: str = ""
    output_artifacts: list[str] = Field(default_factory=list)
    success_criteria: list[str | SuccessCriterion] = Field(default_factory=list)
    eval_feedback: str = ""  # Evaluator feedback, passed back on retry
    auto_eval_result: dict[str, Any] | None = None  # Auto-eval result for downstream agents (#145)
    max_retries: int = 3
    retry_count: int = 0
    workspace_strategy: NodeWorkspaceStrategy = NodeWorkspaceStrategy.SHARED
    backend: str = "builtin"  # M4.0: AgentBackend name ("builtin", "external", etc.)
    owned_files: list[str] = Field(
        default_factory=list,
    )  # Files this node exclusively creates (#272)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    token_usage: dict[str, int] = Field(
        default_factory=dict,
        description="M4.2: Token usage {input_tokens, output_tokens, total_tokens}",
    )
    # M4.6: Token budget for node context
    token_budget: int = Field(
        default=8192,
        description="M4.6: Maximum allowed tokens for this node's context",
    )
    estimated_tokens: int = Field(
        default=0,
        description="M4.6: Pre-execution token estimate (set by TokenEstimator)",
    )
    actual_tokens: int = Field(
        default=0,
        description="M4.6: Actual tokens consumed during execution",
    )

    @field_validator("success_criteria", mode="before")
    @classmethod
    def _normalize_criteria(cls, v: list) -> list:
        """Accept list[str], list[dict], or list[SuccessCriterion].

        Dicts with a recognized 'type' key are parsed into SuccessCriterion.
        Unrecognized types (e.g. legacy "command") are downgraded to CUSTOM.
        JSON strings that look like structured criteria are also parsed
        for backward compatibility with previously serialized data.
        """
        result: list[str | SuccessCriterion] = []
        for item in v:
            if isinstance(item, SuccessCriterion):
                result.append(item)
            elif isinstance(item, dict) and "type" in item:
                result.append(cls._safe_parse_criterion(item))
            elif isinstance(item, str):
                if item.startswith("{"):
                    try:
                        import json as _json
                        data = _json.loads(item)
                        if isinstance(data, dict) and "type" in data:
                            result.append(cls._safe_parse_criterion(data))
                            continue
                    except (_json.JSONDecodeError, Exception):
                        pass
                result.append(item)
            elif isinstance(item, dict):
                result.append(str(item))
            else:
                result.append(str(item))
        return result

    @staticmethod
    def _safe_parse_criterion(data: dict) -> SuccessCriterion:
        """Parse dict into SuccessCriterion, downgrading unknown types to CUSTOM."""
        try:
            return SuccessCriterion(**data)
        except Exception:
            safe = {
                k: v for k, v in data.items()
                if k in ("description", "path", "target", "test_path")
            }
            safe["type"] = "custom"
            return SuccessCriterion(**safe)

    # M2.0: Heartbeat fields
    health_status: NodeHealth = NodeHealth.HEALTHY  # Current health
    last_heartbeat_at: datetime | None = None       # Last heartbeat timestamp
    heartbeat_count: int = 0                         # Total heartbeats received
    missed_heartbeats: int = 0                       # Consecutive missed beats

    def record_heartbeat(self) -> None:
        """Record a heartbeat from the executing agent."""
        self.last_heartbeat_at = datetime.now(timezone.utc)
        self.heartbeat_count += 1
        if self.missed_heartbeats > 0:
            self.missed_heartbeats = 0  # Reset on successful heartbeat
        if self.health_status in (NodeHealth.MISSED, NodeHealth.UNHEALTHY):
            self.health_status = NodeHealth.HEALTHY  # Recovery

    def check_health(self, heartbeat_interval_sec: float = 5.0,
                     miss_threshold: int = 3) -> NodeHealth:
        """
        Check current health based on last heartbeat.

        Returns:
            HEALTHY: Last heartbeat within interval
            MISSED: 1+ missed beats but below threshold
            UNHEALTHY: miss_threshold exceeded -> should be killed
        """
        if self.status != NodeStatus.RUNNING:
            return self.health_status  # Only check running nodes

        if self.last_heartbeat_at is None:
            # Never sent heartbeat since starting
            if self.started_at is None:
                return self.health_status
            elapsed = (datetime.now(timezone.utc) - self.started_at).total_seconds()
        else:
            elapsed = (datetime.now(timezone.utc) - self.last_heartbeat_at).total_seconds()

        missed = int(elapsed / heartbeat_interval_sec)
        self.missed_heartbeats = max(self.missed_heartbeats, missed)

        if missed >= miss_threshold:
            self.health_status = NodeHealth.UNHEALTHY
        elif missed >= 1:
            self.health_status = NodeHealth.MISSED
        else:
            self.health_status = NodeHealth.HEALTHY

        return self.health_status

    def model_post_init(self, __context: Any) -> None:
        if not self.id:
            self.id = f"node_{uuid.uuid4().hex[:8]}"


class DependencyType(str, Enum):
    """Dependency semantics for DAG edges (#271)."""
    HARD = "hard"  # upstream FAILED -> downstream SKIP
    SOFT = "soft"  # upstream FAILED -> downstream continues with warning


class DAGEdge(BaseModel):
    """A directed edge from one node to another."""
    from_node: str
    to_node: str
    dependency_type: DependencyType = DependencyType.HARD


class DAG(BaseModel):
    """
    Directed Acyclic Graph = execution plan.
    Generated by IntelligentOrchestrator, executed by DAGExecutionEngine.
    """
    nodes: dict[str, DAGNode] = Field(default_factory=dict)
    edges: list[DAGEdge] = Field(default_factory=list)
    reasoning: str = ""  # Orchestrator's reasoning for this plan

    def add_node(self, node: DAGNode) -> None:
        self.nodes[node.id] = node

    def update_node(self, node_id: str, **updates) -> DAGNode:
        """Create a new DAGNode with updated fields and replace in nodes dict.

        Uses model_copy for immutability — original node is not modified (#486).
        Returns the new node for convenience.
        """
        old_node = self.nodes[node_id]
        new_node = old_node.model_copy(update=updates)
        self.nodes[node_id] = new_node
        return new_node

    def add_edge(self, from_id: str, to_id: str,
                 dependency_type: DependencyType = DependencyType.HARD) -> None:
        self.edges.append(DAGEdge(
            from_node=from_id, to_node=to_id,
            dependency_type=dependency_type,
        ))

    def get_dependencies(self, node_id: str) -> list[str]:
        """Get all predecessor nodes."""
        return [e.from_node for e in self.edges if e.to_node == node_id]

    def get_hard_dependencies(self, node_id: str) -> list[str]:
        """Get predecessor nodes connected by HARD edges only (#271)."""
        return [
            e.from_node for e in self.edges
            if e.to_node == node_id and e.dependency_type == DependencyType.HARD
        ]

    def get_soft_dependencies(self, node_id: str) -> list[str]:
        """Get predecessor nodes connected by SOFT edges only (#271)."""
        return [
            e.from_node for e in self.edges
            if e.to_node == node_id and e.dependency_type == DependencyType.SOFT
        ]

    def get_dependents(self, node_id: str) -> list[str]:
        """Get all successor nodes."""
        return [e.to_node for e in self.edges if e.from_node == node_id]

    @property
    def total_token_budget(self) -> int:
        """Sum of all node token budgets."""
        return sum(n.token_budget for n in self.nodes.values())

    def topological_levels(self) -> list[list[str]]:
        """
        Return topological sort as levels.
        Nodes in the same level have no dependencies between each other
        and can be executed in parallel.
        """
        in_degree = {nid: 0 for nid in self.nodes}
        adj = {nid: [] for nid in self.nodes}

        for edge in self.edges:
            adj[edge.from_node].append(edge.to_node)
            in_degree[edge.to_node] += 1

        levels = []
        remaining = set(self.nodes.keys())

        while remaining:
            # Find all nodes with in-degree 0
            current_level = [
                nid for nid in remaining
                if in_degree[nid] == 0
            ]
            if not current_level:
                raise ValueError("Cycle detected in DAG")

            levels.append(current_level)
            remaining -= set(current_level)

            # Remove current level from graph
            for nid in current_level:
                for dependent in adj[nid]:
                    in_degree[dependent] -= 1

        return levels

    # Terminal states: the node has finished (success or failure).
    _TERMINAL_STATES = frozenset({
        NodeStatus.SUCCESS,
        NodeStatus.PARTIAL_PASS,
        NodeStatus.WARNED,
        NodeStatus.FAILED,
        NodeStatus.SKIPPED,
    })

    _TERMINAL_SUCCESS_STATES = frozenset({
        NodeStatus.SUCCESS,
        NodeStatus.PARTIAL_PASS,
        NodeStatus.WARNED,
    })

    def get_ready_nodes(self) -> list[str]:
        """Get nodes ready to execute.

        Hard deps must be terminal success (SUCCESS/PARTIAL_PASS/WARNED).
        Soft deps must be terminal (any finished state) so upstream isn't
        still running when downstream starts (#271).
        """
        ready = []
        for nid, node in self.nodes.items():
            if node.status != NodeStatus.PENDING:
                continue
            hard_deps = self.get_hard_dependencies(nid)
            hard_ok = all(
                self.nodes[d].status in self._TERMINAL_SUCCESS_STATES
                for d in hard_deps
            )
            if not hard_ok:
                continue
            soft_deps = self.get_soft_dependencies(nid)
            soft_done = all(
                self.nodes[d].status in self._TERMINAL_STATES
                for d in soft_deps
            )
            if soft_done:
                ready.append(nid)
        return ready


class ExecutionEvent(BaseModel):
    """An event during DAG execution."""
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    node_id: str
    event_type: Literal[
        "started", "completed", "failed", "retrying", "skipped",
        "heartbeat", "heartbeat_missed", "unhealthy_killed", "health_recovered",
        "health_alert", "failure_decision", "upstream_retry",
        "approval_required", "degeneration_recovered",
        "trace",
    ]
    details: dict[str, Any] = Field(default_factory=dict)


class FailureDecision(BaseModel):
    """Orchestrator's decision on how to handle a failed node."""
    action: Literal["retry", "skip", "abort", "replan"]
    reasoning: str = ""
    # For replan: new DAG or modifications
    modifications: dict[str, Any] = Field(default_factory=dict)


class OrchestratorPlan(BaseModel):
    """Output of the orchestrator agent's planning phase."""
    reasoning: str = ""
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, str]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _infer_edges_from_dependencies(self) -> OrchestratorPlan:
        """Infer edges from node dependencies when edges are missing (#561).

        When the LLM response is truncated, the ``edges`` field may be
        missing or empty. Each node's ``dependencies`` list contains the
        IDs of upstream nodes, so we can reconstruct edges from that.
        """
        if self.edges:
            return self

        node_ids = {n.get("id", "") for n in self.nodes}
        inferred: list[dict[str, str]] = []
        for node in self.nodes:
            for dep in node.get("dependencies", []):
                if dep in node_ids:
                    inferred.append({
                        "from": dep,
                        "to": node["id"],
                    })

        if inferred:
            object.__setattr__(self, "edges", inferred)
        return self


class HandoffArtifact(BaseModel):
    """
    Structured handoff between agents.
    Each agent's output becomes the next agent's input.
    """
    from_agent: str
    to_agent: str
    content: str = ""                    # Human-readable summary
    file_paths: list[str] = Field(default_factory=list)  # Generated files
    metadata: dict[str, Any] = Field(default_factory=dict)  # Structured data
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# -- Structured output models for DAG generation (#505) ----------------------


class DAGNodeModel(BaseModel):
    """Structured output model for a single DAG node from LLM planning.

    Used with Anthropic's tool_use structured output to enforce valid DAG
    generation, eliminating JSON parse failures.
    """
    id: str = Field(description="Unique node identifier (e.g. 'gen_auth')")
    agent_type: str = Field(description="Agent type: planner, generator, or evaluator")
    task_description: str = Field(description="What this agent should accomplish")
    dependencies: list[str] = Field(
        default_factory=list,
        description="IDs of nodes this depends on",
    )
    backend: str = Field(
        default="builtin",
        description="Backend name: builtin (default), external, etc.",
    )


class DAGOutputModel(BaseModel):
    """Structured output model for complete DAG plan from LLM.

    The LLM generates this via tool_use mode, ensuring valid structure
    without fragile JSON parsing.
    """
    nodes: list[DAGNodeModel] = Field(description="List of DAG nodes")
    reasoning: str = Field(description="Why this plan structure was chosen")
