"""
Configuration management for the Weave.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import ClassVar

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


import logging
import warnings

logger = logging.getLogger(__name__)


def _get_non_interactive_env() -> str:
    """Read non-interactive env var with backwards compatibility (#543).

    Supports both WEAVE_NON_INTERACTIVE (current) and
    HARNESS_NON_INTERACTIVE (deprecated). Emits DeprecationWarning
    when only the old name is set.
    """
    new_val = os.environ.get("WEAVE_NON_INTERACTIVE")
    old_val = os.environ.get("HARNESS_NON_INTERACTIVE")

    if new_val is not None:
        return new_val
    if old_val is not None:
        warnings.warn(
            "HARNESS_NON_INTERACTIVE is deprecated, use WEAVE_NON_INTERACTIVE",
            DeprecationWarning,
            stacklevel=3,
        )
        return old_val
    return ""


def _load_claude_settings() -> dict[str, str]:
    """Load env vars from ~/.claude/settings-kimi.json if present."""
    settings_path = Path.home() / ".claude" / "settings-kimi.json"
    if settings_path.exists():
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("env", {})
        except Exception as exc:
            logger.debug("Failed to load claude settings: %s", exc)
    return {}


# Cache so we don't re-read the file for every field default.
_CLAUDE_ENV = _load_claude_settings()


def infer_provider(model: str, default: str = "anthropic") -> str:
    """Infer LLM provider from model name.

    Handles known prefixes: gpt*, chatgpt*, o-series (o1, o3, o4, etc.),
    and claude*.
    """
    if model.startswith("gpt") or model.startswith("chatgpt"):
        return "openai"
    # o-series models: o1, o3, o4, etc. (starts with 'o' followed by digit)
    if len(model) > 1 and model[0] == "o" and model[1].isdigit():
        return "openai"
    if model.startswith("claude"):
        return "anthropic"
    return default


class LLMConfig(BaseModel):
    provider: str = "anthropic"  # anthropic, openai
    model: str = "claude-sonnet-4-6"
    api_key: str = Field(
        default_factory=lambda: os.getenv(
            "ANTHROPIC_API_KEY",
            os.getenv("ANTHROPIC_AUTH_TOKEN", _CLAUDE_ENV.get("ANTHROPIC_AUTH_TOKEN", "")),
        )
    )
    base_url: str = Field(
        default_factory=lambda: os.getenv(
            "ANTHROPIC_BASE_URL",
            _CLAUDE_ENV.get("ANTHROPIC_BASE_URL", ""),
        )
    )
    max_tokens: int = 4096
    temperature: float = 0.3
    timeout: int = 120
    # Maximum concurrent API calls across all parallel nodes (#300).
    # When unset (0/None), no limit.  Set to 3-5 for rate-limited APIs.
    max_concurrent_api: int = Field(
        default_factory=lambda: int(os.getenv("WEAVE_MAX_CONCURRENT_API", "0"))
    )


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


class AgentWatchdogOverride(BaseModel):
    """Per-agent-type heartbeat override for the watchdog."""

    heartbeat_interval_sec: float | None = None
    heartbeat_miss_threshold: int | None = None


# Sensible defaults: generator tasks (editing existing files) naturally take
# much longer than planner/evaluator tasks, especially when writing 6+ test
# files (#275).
_DEFAULT_AGENT_WATCHDOG_OVERRIDES: dict[str, AgentWatchdogOverride] = {
    "generator": AgentWatchdogOverride(
        heartbeat_interval_sec=90.0,
        heartbeat_miss_threshold=20,
    ),
}


class WatchdogConfig(BaseModel):
    """Configuration for the DAG node watchdog."""

    enabled: bool = True
    heartbeat_interval_sec: float = 30.0
    heartbeat_miss_threshold: int = 12  # was 5→8; raised to reduce false kills (#275)
    # Fraction of miss_threshold at which heartbeat_missed events are
    # emitted.  With threshold=8 and fraction=0.5, alerts fire at
    # missed_count >= 4 instead of the old hardcoded 2 — reducing noise
    # for slow but healthy LLM API responses.
    alert_threshold_fraction: float = Field(default=0.5, ge=0.0, le=1.0)
    agent_overrides: dict[str, AgentWatchdogOverride] = Field(
        default_factory=lambda: dict(_DEFAULT_AGENT_WATCHDOG_OVERRIDES),
    )

    def settings_for(self, agent_type: str) -> tuple[float, int]:
        """Return (interval_sec, miss_threshold) for *agent_type*."""
        override = self.agent_overrides.get(agent_type)
        interval = (
            override.heartbeat_interval_sec
            if override and override.heartbeat_interval_sec is not None
            else self.heartbeat_interval_sec
        )
        threshold = (
            override.heartbeat_miss_threshold
            if override and override.heartbeat_miss_threshold is not None
            else self.heartbeat_miss_threshold
        )
        return interval, threshold

    def alert_threshold_for(self, agent_type: str) -> int:
        """Minimum missed_count to emit heartbeat_missed event."""
        _, threshold = self.settings_for(agent_type)
        return max(2, int(threshold * self.alert_threshold_fraction))

    @classmethod
    def from_env(cls) -> WatchdogConfig:
        """Create from WEAVE_WATCHDOG_* environment variables."""
        overrides: dict[str, AgentWatchdogOverride] = dict(
            _DEFAULT_AGENT_WATCHDOG_OVERRIDES,
        )
        # Allow per-agent override via WEAVE_WATCHDOG_<TYPE>_INTERVAL /
        # WEAVE_WATCHDOG_<TYPE>_THRESHOLD (e.g. WEAVE_WATCHDOG_GENERATOR_INTERVAL)
        for agent_type in ("planner", "generator", "evaluator"):
            iv = os.getenv(f"WEAVE_WATCHDOG_{agent_type.upper()}_INTERVAL")
            tv = os.getenv(f"WEAVE_WATCHDOG_{agent_type.upper()}_THRESHOLD")
            if iv or tv:
                overrides[agent_type] = AgentWatchdogOverride(
                    heartbeat_interval_sec=float(iv) if iv else None,
                    heartbeat_miss_threshold=int(tv) if tv else None,
                )
        return cls(
            enabled=os.getenv("WEAVE_WATCHDOG_ENABLED", "true").lower()
            not in ("false", "0", "no"),
            heartbeat_interval_sec=float(
                os.getenv("WEAVE_WATCHDOG_INTERVAL", "30.0")
            ),
            heartbeat_miss_threshold=int(
                os.getenv("WEAVE_WATCHDOG_THRESHOLD", "8")
            ),
            alert_threshold_fraction=float(
                os.getenv("WEAVE_WATCHDOG_ALERT_FRACTION", "0.5")
            ),
            agent_overrides=overrides,
        )


class ModelRoute(BaseModel):
    """Model assignment for a specific agent type or role."""

    provider: str = ""
    model: str = ""
    temperature: float | None = None
    max_tokens: int | None = None


class ModelRoutingConfig(BaseModel):
    """Configuration for per-agent-type model selection.

    When routing is empty, all agents use the default model from LLMConfig.
    """

    routing: dict[str, ModelRoute] = Field(default_factory=dict)
    fallback_chain: list[str] = Field(
        default_factory=lambda: ["claude-sonnet-4-6"]
    )

    @classmethod
    def from_env(cls) -> ModelRoutingConfig:
        """Create routing config from WEAVE_*_MODEL environment variables."""
        routing: dict[str, ModelRoute] = {}
        for agent_type, env_var in [
            ("planner", "WEAVE_PLANNER_MODEL"),
            ("generator", "WEAVE_GENERATOR_MODEL"),
            ("evaluator", "WEAVE_EVALUATOR_MODEL"),
            ("orchestrator", "WEAVE_ORCHESTRATOR_MODEL"),
        ]:
            model = os.getenv(env_var, "")
            if model:
                routing[agent_type] = ModelRoute(
                    provider=infer_provider(model), model=model
                )

        fallback_str = os.getenv(
            "WEAVE_MODEL_FALLBACK", "claude-sonnet-4-6"
        )
        fallback_chain = [m.strip() for m in fallback_str.split(",") if m.strip()]

        return cls(routing=routing, fallback_chain=fallback_chain)

    @classmethod
    def from_yaml(cls, path: str | Path) -> ModelRoutingConfig:
        """Load routing config from a YAML file."""
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        routing = {}
        for key, val in data.get("routing", {}).items():
            if isinstance(val, dict):
                routing[key] = ModelRoute(**val)
            elif isinstance(val, str):
                provider = infer_provider(val)
                routing[key] = ModelRoute(provider=provider, model=val)
        return cls(
            routing=routing,
            fallback_chain=data.get("fallback_chain", ["claude-sonnet-4-6"]),
        )


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


class EvalTimeoutScaleConfig(BaseModel):
    """Dynamic evaluator timeout scaling based on project size (#621).

    When enabled, the evaluator timeout is calculated as:
        base_timeout + (file_count * per_file_seconds)
    This prevents timeout failures on large generated projects.
    """

    enabled: bool = Field(
        default=os.getenv(
            "WEAVE_EVAL_TIMEOUT_SCALE", "true",
        ).lower() not in ("false", "0", "no"),
        description="Enable dynamic evaluator timeout scaling",
    )
    per_file_seconds: int = Field(
        default=int(os.getenv(
            "WEAVE_EVAL_TIMEOUT_PER_FILE", "5",
        )),
        ge=1,
        description="Additional seconds per output file for evaluator timeout",
    )
    max_timeout: int = Field(
        default=int(os.getenv(
            "WEAVE_EVAL_TIMEOUT_MAX", "1200",
        )),
        ge=1,
        description="Upper cap for dynamically scaled evaluator timeout",
    )


class NodeTimeoutConfig(BaseModel):
    """Per-agent-type node execution timeout (#360 PR2).

    Replaces the former agent_timeout (flat int) and watchdog_overrides
    with a unified configuration.  Timeout enforcement lives in
    dag_engine._execute_with_timeout, NOT in agent_pool.
    """

    default_timeout: int = Field(
        default=int(os.getenv(
            "WEAVE_NODE_TIMEOUT",
            os.getenv("WEAVE_AGENT_TIMEOUT", "300"),
        )),
        description="Default node execution timeout in seconds",
    )
    overrides: dict[str, int] = Field(
        default_factory=lambda: {
            "generator": int(os.getenv(
                "WEAVE_NODE_TIMEOUT_GENERATOR", "600",
            )),
            "evaluator": int(os.getenv(
                "WEAVE_NODE_TIMEOUT_EVALUATOR", "480",
            )),
        },
        description="Per-agent-type timeout overrides (agent_type -> seconds)",
    )
    eval_scale: EvalTimeoutScaleConfig = Field(
        default_factory=EvalTimeoutScaleConfig,
        description="Dynamic evaluator timeout scaling (#621)",
    )

    def timeout_for(
        self, agent_type: str, artifact_count: int = 0,
    ) -> int:
        """Return timeout for the given agent type.

        For evaluator nodes with eval_scale enabled, dynamically adjusts
        the timeout based on the number of output artifacts from upstream
        nodes (proxy for project size).
        """
        base = self.overrides.get(agent_type, self.default_timeout)

        if (
            agent_type == "evaluator"
            and self.eval_scale.enabled
            and artifact_count > 0
        ):
            scaled = base + artifact_count * self.eval_scale.per_file_seconds
            return min(scaled, self.eval_scale.max_timeout)

        return base

    @property
    def min_timeout(self) -> int:
        values = [self.default_timeout, *self.overrides.values()]
        return min(values)

    @property
    def max_timeout(self) -> int:
        values = [self.default_timeout, *self.overrides.values()]
        if self.eval_scale.enabled:
            values.append(self.eval_scale.max_timeout)
        return max(values)


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


class WeaveConfig(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)
    event_store_path: str = "./data/events"
    artifact_path: str = "./data/artifacts"
    checkpoint_interval: int = 10  # events
    max_context_messages: int = 50
    # M2 agent_timeout kept for backward compat; superseded by node_timeout (#360)
    agent_timeout: int = Field(
        default_factory=lambda: int(os.getenv("WEAVE_AGENT_TIMEOUT", "300")),
        description="Legacy — use node_timeout instead",
    )
    node_timeout: NodeTimeoutConfig = Field(default_factory=NodeTimeoutConfig)
    max_context_tokens: int = 100000  # token threshold for context truncation
    log_level: str = "INFO"

    # M2.2: Backend configuration
    default_backend: str = Field(
        default_factory=lambda: os.getenv(
            "WEAVE_DEFAULT_BACKEND",
            os.getenv("WEAVE_WORKSPACE_ISOLATION", "local"),
        )
    )
    backend_base_path: str = Field(
        default_factory=lambda: os.getenv(
            "WEAVE_BACKEND_BASE_PATH", "./data/backends"
        )
    )
    risk_backend_map: dict[str, str] = Field(
        default_factory=lambda: {
            "low": os.getenv("WEAVE_BACKEND_LOW", "local"),
            "medium": os.getenv("WEAVE_BACKEND_MEDIUM", "local"),
            "high": os.getenv("WEAVE_BACKEND_HIGH", "worktree"),
            "critical": os.getenv(
                "WEAVE_BACKEND_CRITICAL", "worktree"
            ),
        }
    )

    # M1.1: Non-interactive mode configuration
    non_interactive: bool = Field(
        default_factory=lambda: _get_non_interactive_env().lower()
        in ("true", "1", "yes")
    )
    approval_timeout_sec: int = Field(
        default_factory=lambda: int(os.getenv("WEAVE_APPROVAL_TIMEOUT_SEC", "300"))
    )
    cleanup_policy: str = Field(
        default_factory=lambda: os.getenv("WEAVE_CLEANUP_POLICY", "on_success"),
        pattern=r"^(on_success|always|never)$",
    )

    # M3.1: Multi-model routing
    model_routing: ModelRoutingConfig = Field(default_factory=ModelRoutingConfig)

    # M3.2: Agent Memory
    memory: MemoryConfig = Field(default_factory=MemoryConfig)

    # M3.3: Self-Learning
    learning: LearningConfig = Field(default_factory=LearningConfig)

    # M3.5: Impact Analysis
    impact: ImpactConfig = Field(default_factory=ImpactConfig)

    # M4.2: Token Budget
    budget: BudgetConfig = Field(default_factory=BudgetConfig)

    # M4.4: Codex Backend
    codex: CodexBackendConfig = Field(default_factory=CodexBackendConfig)

    # M4.1: Claude Code Backend
    claude_code: ClaudeCodeConfig = Field(default_factory=ClaudeCodeConfig)

    # M2.0: Watchdog
    watchdog: WatchdogConfig = Field(default_factory=WatchdogConfig)

    # Evaluation: auto-format before lint (opt-in, #206)
    auto_format_before_eval: bool = Field(
        default_factory=lambda: os.getenv(
            "WEAVE_AUTO_FORMAT_BEFORE_EVAL", ""
        ).lower() in ("true", "1", "yes")
    )

    # Evaluation: default pass threshold (#316).
    # When set, score >= threshold means overall pass even if some soft
    # criteria fail (e.g. lint), as long as all hard criteria pass
    # (file_exists, tests_pass).  Prevents trivial lint issues from
    # blocking the entire DAG.  CLI --pass-threshold overrides this.
    pass_threshold: float = Field(
        default_factory=lambda: float(os.getenv(
            "WEAVE_PASS_THRESHOLD", "7.0"
        ))
    )

    # Per-run wall-clock timeout in seconds (#324).
    # Controls how long a single run attempt may take before being killed.
    # CLI --timeout overrides this.  Previous default was 600 (10 min) which
    # was too short for complex tasks on slower LLM backends.
    run_timeout_sec: int = Field(
        default_factory=lambda: int(os.getenv(
            "WEAVE_RUN_TIMEOUT_SEC", "1800"
        ))
    )

    @classmethod
    def from_yaml(cls, path: str | Path) -> WeaveConfig:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        instance = cls(**data)
        instance.warn_on_timeout_issues()
        return instance

    def validate_timeout_inequality(self) -> list[str]:
        """Validate the timeout ordering constraint (#360 PR4).

        Returns a list of warning/error messages. Empty list means all OK.

        Required: llm.timeout < node_timeout_min < run_timeout_sec
        """
        issues: list[str] = []
        llm_timeout = self.llm.timeout
        node_min = self.node_timeout.min_timeout
        node_max = self.node_timeout.max_timeout
        run_timeout = self.run_timeout_sec

        # Use >= (not >) so that equal values also trigger a warning:
        # if HTTP timeout == node timeout, a slow LLM call will consume
        # the entire node budget without making progress.
        if llm_timeout >= node_min:
            issues.append(
                f"HTTP timeout ({llm_timeout}s) >= min node timeout "
                f"({node_min}s) — LLM calls may time out before node budget "
                f"is used. Reduce WEAVE_LLM_TIMEOUT or increase "
                f"WEAVE_NODE_TIMEOUT."
            )
        if node_max >= run_timeout:
            issues.append(
                f"Max node timeout ({node_max}s) >= run timeout "
                f"({run_timeout}s) — nodes may exceed run budget. "
                f"Increase WEAVE_RUN_TIMEOUT_SEC or reduce "
                f"WEAVE_NODE_TIMEOUT_GENERATOR."
            )
        return issues

    def warn_on_timeout_issues(self) -> None:
        """Log warnings for timeout inequality violations (#360 PR4)."""
        import logging
        issues = self.validate_timeout_inequality()
        for issue in issues:
            logging.getLogger(__name__).warning(
                "Timeout config issue: %s", issue,
            )

    @classmethod
    def from_env(cls) -> WeaveConfig:
        """Create config from environment variables (with ~/.claude/settings-kimi.json fallback)."""
        instance = cls(
            llm=LLMConfig(
                api_key=os.getenv(
                    "ANTHROPIC_API_KEY",
                    os.getenv("ANTHROPIC_AUTH_TOKEN", _CLAUDE_ENV.get("ANTHROPIC_AUTH_TOKEN", "")),
                ),
                model=os.getenv(
                    "WEAVE_MODEL",
                    os.getenv(
                        "ANTHROPIC_DEFAULT_SONNET_MODEL",
                        _CLAUDE_ENV.get("ANTHROPIC_DEFAULT_SONNET_MODEL", "claude-sonnet-4-6"),
                    ),
                ),
                base_url=os.getenv("ANTHROPIC_BASE_URL", _CLAUDE_ENV.get("ANTHROPIC_BASE_URL", "")),
            ),
            event_store_path=os.getenv("WEAVE_EVENT_STORE", "./data/events"),
            artifact_path=os.getenv("WEAVE_ARTIFACT_PATH", "./data/artifacts"),
            agent_timeout=int(os.getenv("WEAVE_AGENT_TIMEOUT", "300")),
            max_context_tokens=int(os.getenv("WEAVE_MAX_CONTEXT_TOKENS", "100000")),
            non_interactive=_get_non_interactive_env().lower()
            in ("true", "1", "yes"),
            approval_timeout_sec=int(os.getenv("WEAVE_APPROVAL_TIMEOUT_SEC", "300")),
            cleanup_policy=os.getenv("WEAVE_CLEANUP_POLICY", "on_success"),
            model_routing=ModelRoutingConfig.from_env(),
            memory=MemoryConfig(
                enabled=os.getenv("WEAVE_MEMORY_ENABLED", "true").lower()
                not in ("false", "0", "no"),
                base_path=os.getenv("WEAVE_MEMORY_PATH", "./data/memory"),
                max_entries_per_agent=int(os.getenv("WEAVE_MEMORY_MAX_ENTRIES", "500")),
                max_content_length=int(os.getenv("WEAVE_MEMORY_MAX_LENGTH", "1000")),
                default_ttl_days=int(os.getenv("WEAVE_MEMORY_TTL_DAYS", "90")),
                retrieval_limit=int(os.getenv("WEAVE_MEMORY_RETRIEVAL_LIMIT", "10")),
                decay_half_life_days=float(os.getenv("WEAVE_MEMORY_DECAY_DAYS", "30")),
            ),
            learning=LearningConfig(
                enabled=os.getenv("WEAVE_LEARNING_ENABLED", "true").lower()
                not in ("false", "0", "no"),
                analysis_interval_hours=float(
                    os.getenv("WEAVE_LEARNING_INTERVAL_HOURS", "6.0")
                ),
                min_samples=int(os.getenv("WEAVE_LEARNING_MIN_SAMPLES", "5")),
                max_insights=int(os.getenv("WEAVE_LEARNING_MAX_INSIGHTS", "100")),
                confidence_threshold=float(
                    os.getenv("WEAVE_LEARNING_CONFIDENCE", "0.7")
                ),
                base_path=os.getenv("WEAVE_LEARNING_PATH", "./data/learning"),
            ),
            impact=ImpactConfig(
                enabled=os.getenv("WEAVE_IMPACT_ENABLED", "true").lower()
                not in ("false", "0", "no"),
                base_path=os.getenv("WEAVE_IMPACT_PATH", "./data/impact"),
                coverage_threshold=float(
                    os.getenv("WEAVE_IMPACT_COVERAGE_THRESHOLD", "0.7")
                ),
                max_predicted_files=int(
                    os.getenv("WEAVE_IMPACT_MAX_FILES", "50")
                ),
                confidence_threshold=float(
                    os.getenv("WEAVE_IMPACT_CONFIDENCE", "0.5")
                ),
            ),
            watchdog=WatchdogConfig.from_env(),
            budget=BudgetConfig.from_env(),
            mcp=MCPConfig(
                auto_discover=(
                    os.getenv("WEAVE_MCP_AUTO_DISCOVER", "false").lower()
                    in ("true", "1", "yes")
                ),
                connection_timeout=int(os.getenv("WEAVE_MCP_CONNECTION_TIMEOUT", "30")),
            ),
        )
        instance.warn_on_timeout_issues()
        return instance
