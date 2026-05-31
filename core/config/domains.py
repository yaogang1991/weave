"""Domain config models: sandbox, MCP, memory, learning, impact, budget,
token estimation, backends, observability."""

from __future__ import annotations

import os
from typing import Any, ClassVar

from pydantic import BaseModel, Field, field_validator, model_validator


class SandboxConfig(BaseModel):
    enabled: bool = True
    runtime: str = "local"  # local, docker (docker not yet implemented)
    image: str = "python:3.11-slim"
    network_mode: str = "none"  # none, bridge
    memory_limit: str = "512m"
    cpu_limit: float = 1.0
    timeout: int = 300
    credential_proxy: bool = True


class MCPServerConfig(BaseModel):
    """Configuration for a single MCP server connection."""
    name: str
    command: str                                      # e.g. "npx", "python"
    args: list[str] = Field(
        default_factory=list,
    )  # e.g. ["-y", "@modelcontextprotocol/server-github"]
    env: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    default_risk_level: str = "medium"                # LOW, MEDIUM, HIGH, CRITICAL

    @field_validator("default_risk_level")
    @classmethod
    def _validate_risk_level(cls, v: str) -> str:
        valid = {"low", "medium", "high", "critical"}
        if v.lower() not in valid:
            raise ValueError(
                f"Invalid default_risk_level '{v}', must be one of {valid}"
            )
        return v.lower()


class MCPConfig(BaseModel):
    """Configuration for MCP (Model Context Protocol) integration."""
    servers: list[MCPServerConfig] = Field(default_factory=list)
    auto_discover: bool = False
    connection_timeout: int = 30  # seconds to wait for server startup


class MemoryConfig(BaseModel):
    """Configuration for the M3.2 Agent Memory system."""
    enabled: bool = True
    base_path: str = Field(
        default_factory=lambda: os.getenv("WEAVE_MEMORY_PATH", "./data/memory")
    )
    max_entries_per_agent: int = Field(default=500, ge=1)
    max_content_length: int = Field(default=1000, ge=100)       # Characters per entry
    default_ttl_days: int = Field(default=90, ge=1)             # Default expiry for entries
    retrieval_limit: int = Field(default=10, ge=1)              # Max memories injected per prompt
    decay_half_life_days: float = Field(default=30.0, ge=1.0)   # Relevance score decay rate
    auto_store: bool = True             # Automatically store learnings after task
    embedding_provider: str = Field(    # #508 P2: semantic retrieval provider
        default="local",
        description="Embedding provider: local (default) or openai",
    )
    semantic_search_enabled: bool = Field(  # #508 P2: enable semantic retrieval
        default=True,
        description="Use semantic search for memory retrieval",
    )

    @model_validator(mode="after")
    def _validate_memory_config(self) -> "MemoryConfig":
        if self.retrieval_limit > self.max_entries_per_agent:
            raise ValueError("retrieval_limit cannot exceed max_entries_per_agent")
        return self


class LearningConfig(BaseModel):
    """Configuration for the M3.3 Self-Learning system."""
    enabled: bool = True
    analysis_interval_hours: float = Field(default=6.0, ge=0.0)
    min_samples: int = Field(default=5, ge=1)                # Min executions before analysis
    max_insights: int = Field(default=100, ge=1)
    confidence_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    base_path: str = Field(
        default_factory=lambda: os.getenv("WEAVE_LEARNING_PATH", "./data/learning")
    )

    # Analysis thresholds (#468)
    failure_rate_threshold: float = Field(default=50.0, ge=0.0, le=100.0)
    success_rate_threshold: float = Field(default=80.0, ge=0.0, le=100.0)
    low_agent_rate_threshold: float = Field(default=40.0, ge=0.0, le=100.0)
    min_error_samples: int = Field(default=3, ge=1)
    min_trend_samples: int = Field(default=5, ge=1)
    retry_rate_threshold: float = Field(default=30.0, ge=0.0, le=100.0)
    duration_variance_ratio: float = Field(default=3.0, ge=1.0)


class ImpactConfig(BaseModel):
    """Configuration for the M3.5 Impact Analysis system."""
    enabled: bool = True
    coverage_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    max_predicted_files: int = Field(default=50, ge=1)
    confidence_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    base_path: str = Field(
        default_factory=lambda: os.getenv(
            "WEAVE_IMPACT_PATH", "./data/impact"
        )
    )


class BudgetConfig(BaseModel):
    """M4.2: Token budget configuration for cost control."""

    enabled: bool = True
    total_tokens: int = Field(
        default=0, ge=0,
        description="Total token budget for a run. 0 means unlimited.",
    )
    warning_threshold: float = Field(
        default=0.8, ge=0.0, le=1.0,
        description="Emit BUDGET_WARNING when usage reaches this fraction.",
    )
    per_node_token_limit: int = Field(
        default=0, ge=0,
        description="Per-node token limit. 0 means unlimited.",
    )

    @property
    def is_unlimited(self) -> bool:
        return self.total_tokens == 0

    @classmethod
    def from_env(cls) -> BudgetConfig:
        return cls(
            enabled=os.getenv("WEAVE_BUDGET_ENABLED", "true").lower()
            not in ("false", "0", "no"),
            total_tokens=int(os.getenv("WEAVE_BUDGET_TOKENS", "0")),
            warning_threshold=float(
                os.getenv("WEAVE_BUDGET_WARNING_THRESHOLD", "0.8")
            ),
            per_node_token_limit=int(
                os.getenv("WEAVE_BUDGET_PER_NODE_TOKENS", "0")
            ),
        )


class TokenEstimationConfig(BaseModel):
    """M4.6: Token estimation configuration for pre-execution planning."""

    enabled: bool = Field(
        default=True,
        description="Use Anthropic count_tokens() API for estimation",
    )
    fallback_to_heuristic: bool = Field(
        default=True,
        description="Fall back to char/3.5 heuristic on API failure",
    )
    target_budget: int = Field(
        default=8192,
        description="Default token budget per node",
    )
    overhead_margins: dict[str, int] = Field(
        default_factory=lambda: {
            "generator": 2200,
            "evaluator": 900,
            "planner": 550,
        },
        description="Per-agent-type overhead (system prompt + tools), measured + 5% buffer",
    )
    max_estimation_concurrency: int = Field(
        default=10,
        description="Max parallel count_tokens() calls",
    )
    cache_ttl_seconds: int = Field(
        default=300,
        description="Cache token estimation results for N seconds",
    )


class CodexBackendConfig(BaseModel):
    """M4.4: Configuration for the Codex CLI backend."""
    enabled: bool = Field(
        default_factory=lambda: os.getenv(
            "WEAVE_CODEX_ENABLED", "false"
        ).lower() in ("true", "1", "yes"),
    )
    binary_path: str = Field(
        default_factory=lambda: os.getenv("WEAVE_CODEX_BINARY_PATH", "codex"),
    )
    model: str = Field(
        default_factory=lambda: os.getenv("WEAVE_CODEX_MODEL", "codex-mini"),
    )
    sandbox_mode: str = Field(
        default_factory=lambda: os.getenv("WEAVE_CODEX_SANDBOX", "workspace-write"),
    )
    timeout: int = Field(default=600, description="Per-invocation timeout in seconds.")
    mcp_config: Any = Field(
        default=None,
        description="MCPConfig for tool passing to Codex subprocess (M6.8)",
    )

    # Allowed sandbox modes — single source of truth (#619 #4).
    VALID_SANDBOX_MODES: ClassVar[frozenset[str]] = frozenset({
        "workspace-write", "workspace-read", "full-access",
        "none", "readOnly", "dangerFullAccess",
    })

    @field_validator("sandbox_mode")
    @classmethod
    def _validate_sandbox_mode(cls, v: str) -> str:
        if v not in cls.VALID_SANDBOX_MODES:
            raise ValueError(
                f"sandbox_mode must be one of {cls.VALID_SANDBOX_MODES}, got {v!r}"
            )
        return v


class ClaudeCodeConfig(BaseModel):
    """M4.1: Configuration for ClaudeCodeBackend."""

    enabled: bool = Field(
        default_factory=lambda: os.getenv(
            "WEAVE_CLAUDE_CODE_ENABLED", "false"
        ).lower() in ("true", "1", "yes"),
    )
    cli_path: str = Field(
        default_factory=lambda: os.getenv("WEAVE_CLAUDE_CODE_PATH", "claude"),
        description="Path to claude CLI binary",
    )
    model: str = Field(
        default="",
        description="Model override for Claude Code (empty = default)",
    )
    max_turns: int = Field(
        default=0, ge=0,
        description="Max conversation turns (0 = unlimited)",
    )
    permission_mode: str = Field(
        default="default",
        description=(
            "Claude Code permission mode. "
            "Must be 'default', 'plan', or 'bypassPermissions' "
            "(requires explicit opt-in)."
        ),
    )
    allowed_tools: list[str] = Field(
        default_factory=list,
        description="Tool names to allow (empty = all default)",
    )
    system_prompt_append: str = Field(
        default="",
        description="Additional system prompt",
    )
    max_budget_usd: float = Field(
        default=0.0, ge=0.0,
        description="Max USD budget per node (0 = unlimited)",
    )
    timeout_override: int = Field(
        default=0, ge=0,
        description="Per-node timeout override in seconds (0 = use node_timeout)",
    )

    @field_validator("permission_mode")
    @classmethod
    def validate_permission_mode(cls, v: str) -> str:
        allowed = {"default", "plan", "bypassPermissions"}
        if v not in allowed:
            raise ValueError(
                f"permission_mode must be one of {allowed}, got '{v}'"
            )
        return v

    @field_validator("cli_path")
    @classmethod
    def validate_cli_path(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("cli_path must not be empty")
        return v.strip()


class ObservabilityConfig(BaseModel):
    """M5.1: Observability configuration for tracing and token reporting."""
    enabled: bool = True
    otlp_endpoint: str | None = None

    @classmethod
    def from_env(cls) -> ObservabilityConfig:
        return cls(
            enabled=os.getenv("WEAVE_OBSERVABILITY_ENABLED", "true").lower()
            not in ("false", "0"),
            otlp_endpoint=os.getenv("WEAVE_OTLP_ENDPOINT") or None,
        )
