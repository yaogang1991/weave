"""
Configuration management for the Harness.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator


def _load_claude_settings() -> dict[str, str]:
    """Load env vars from ~/.claude/settings-kimi.json if present."""
    settings_path = Path.home() / ".claude" / "settings-kimi.json"
    if settings_path.exists():
        try:
            with open(settings_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("env", {})
        except Exception:
            pass
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
        default_factory=lambda: int(os.getenv("HARNESS_MAX_CONCURRENT_API", "0"))
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


class MCPConfig(BaseModel):
    servers: list[dict[str, Any]] = Field(default_factory=list)
    auto_discover: bool = False


class AgentWatchdogOverride(BaseModel):
    """Per-agent-type heartbeat override for the watchdog."""

    heartbeat_interval_sec: float | None = None
    heartbeat_miss_threshold: int | None = None


# Sensible defaults: generator tasks (editing existing files) naturally take
# much longer than planner/evaluator tasks.
_DEFAULT_AGENT_WATCHDOG_OVERRIDES: dict[str, AgentWatchdogOverride] = {
    "generator": AgentWatchdogOverride(
        heartbeat_interval_sec=60.0,
        heartbeat_miss_threshold=10,
    ),
}


class WatchdogConfig(BaseModel):
    """Configuration for the DAG node watchdog."""

    enabled: bool = True
    heartbeat_interval_sec: float = 30.0
    heartbeat_miss_threshold: int = 8  # was 5; raised to reduce false kills
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
        """Create from HARNESS_WATCHDOG_* environment variables."""
        overrides: dict[str, AgentWatchdogOverride] = dict(
            _DEFAULT_AGENT_WATCHDOG_OVERRIDES,
        )
        # Allow per-agent override via HARNESS_WATCHDOG_<TYPE>_INTERVAL /
        # HARNESS_WATCHDOG_<TYPE>_THRESHOLD (e.g. HARNESS_WATCHDOG_GENERATOR_INTERVAL)
        for agent_type in ("planner", "generator", "evaluator"):
            iv = os.getenv(f"HARNESS_WATCHDOG_{agent_type.upper()}_INTERVAL")
            tv = os.getenv(f"HARNESS_WATCHDOG_{agent_type.upper()}_THRESHOLD")
            if iv or tv:
                overrides[agent_type] = AgentWatchdogOverride(
                    heartbeat_interval_sec=float(iv) if iv else None,
                    heartbeat_miss_threshold=int(tv) if tv else None,
                )
        return cls(
            enabled=os.getenv("HARNESS_WATCHDOG_ENABLED", "true").lower()
            not in ("false", "0", "no"),
            heartbeat_interval_sec=float(
                os.getenv("HARNESS_WATCHDOG_INTERVAL", "30.0")
            ),
            heartbeat_miss_threshold=int(
                os.getenv("HARNESS_WATCHDOG_THRESHOLD", "8")
            ),
            alert_threshold_fraction=float(
                os.getenv("HARNESS_WATCHDOG_ALERT_FRACTION", "0.5")
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
        """Create routing config from HARNESS_*_MODEL environment variables."""
        routing: dict[str, ModelRoute] = {}
        for agent_type, env_var in [
            ("planner", "HARNESS_PLANNER_MODEL"),
            ("generator", "HARNESS_GENERATOR_MODEL"),
            ("evaluator", "HARNESS_EVALUATOR_MODEL"),
            ("orchestrator", "HARNESS_ORCHESTRATOR_MODEL"),
        ]:
            model = os.getenv(env_var, "")
            if model:
                routing[agent_type] = ModelRoute(
                    provider=infer_provider(model), model=model
                )

        fallback_str = os.getenv(
            "HARNESS_MODEL_FALLBACK", "claude-sonnet-4-6"
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
        default_factory=lambda: os.getenv("HARNESS_MEMORY_PATH", "./data/memory")
    )
    max_entries_per_agent: int = Field(default=500, ge=1)
    max_content_length: int = Field(default=1000, ge=100)       # Characters per entry
    default_ttl_days: int = Field(default=90, ge=1)             # Default expiry for entries
    retrieval_limit: int = Field(default=10, ge=1)              # Max memories injected per prompt
    decay_half_life_days: float = Field(default=30.0, ge=1.0)   # Relevance score decay rate
    auto_store: bool = True             # Automatically store learnings after task

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
        default_factory=lambda: os.getenv("HARNESS_LEARNING_PATH", "./data/learning")
    )


class ImpactConfig(BaseModel):
    """Configuration for the M3.5 Impact Analysis system."""
    enabled: bool = True
    coverage_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    max_predicted_files: int = Field(default=50, ge=1)
    confidence_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    base_path: str = Field(
        default_factory=lambda: os.getenv(
            "HARNESS_IMPACT_PATH", "./data/impact"
        )
    )


class HarnessConfig(BaseModel):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)
    event_store_path: str = "./data/events"
    artifact_path: str = "./data/artifacts"
    checkpoint_interval: int = 10  # events
    max_context_messages: int = 50
    agent_timeout: int = 300  # seconds per agent execution
    max_context_tokens: int = 100000  # token threshold for context truncation
    log_level: str = "INFO"

    # M2.2: Backend configuration
    default_backend: str = Field(
        default_factory=lambda: os.getenv(
            "HARNESS_DEFAULT_BACKEND",
            os.getenv("HARNESS_WORKSPACE_ISOLATION", "local"),
        )
    )
    backend_base_path: str = Field(
        default_factory=lambda: os.getenv(
            "HARNESS_BACKEND_BASE_PATH", "./data/backends"
        )
    )
    risk_backend_map: dict[str, str] = Field(
        default_factory=lambda: {
            "low": os.getenv("HARNESS_BACKEND_LOW", "local"),
            "medium": os.getenv("HARNESS_BACKEND_MEDIUM", "local"),
            "high": os.getenv("HARNESS_BACKEND_HIGH", "worktree"),
            "critical": os.getenv(
                "HARNESS_BACKEND_CRITICAL", "worktree"
            ),
        }
    )

    # M1.1: Non-interactive mode configuration
    non_interactive: bool = Field(
        default_factory=lambda: os.getenv("HARNESS_NON_INTERACTIVE", "").lower()
        in ("true", "1", "yes")
    )
    approval_timeout_sec: int = Field(
        default_factory=lambda: int(os.getenv("HARNESS_APPROVAL_TIMEOUT_SEC", "300"))
    )
    cleanup_policy: str = Field(
        default_factory=lambda: os.getenv("HARNESS_CLEANUP_POLICY", "on_success"),
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

    # M2.0: Watchdog
    watchdog: WatchdogConfig = Field(default_factory=WatchdogConfig)

    # Evaluation: auto-format before lint (opt-in, #206)
    auto_format_before_eval: bool = Field(
        default_factory=lambda: os.getenv(
            "HARNESS_AUTO_FORMAT_BEFORE_EVAL", ""
        ).lower() in ("true", "1", "yes")
    )

    # Evaluation: default pass threshold (#316).
    # When set, score >= threshold means overall pass even if some soft
    # criteria fail (e.g. lint), as long as all hard criteria pass
    # (file_exists, tests_pass).  Prevents trivial lint issues from
    # blocking the entire DAG.  CLI --pass-threshold overrides this.
    pass_threshold: float = Field(
        default_factory=lambda: float(os.getenv(
            "HARNESS_PASS_THRESHOLD", "7.0"
        ))
    )

    # Per-run wall-clock timeout in seconds (#324).
    # Controls how long a single run attempt may take before being killed.
    # CLI --timeout overrides this.  Previous default was 600 (10 min) which
    # was too short for complex tasks on slower LLM backends.
    run_timeout_sec: int = Field(
        default_factory=lambda: int(os.getenv(
            "HARNESS_RUN_TIMEOUT_SEC", "1800"
        ))
    )

    @classmethod
    def from_yaml(cls, path: str | Path) -> HarnessConfig:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return cls(**data)

    @classmethod
    def from_env(cls) -> HarnessConfig:
        """Create config from environment variables (with ~/.claude/settings-kimi.json fallback)."""
        return cls(
            llm=LLMConfig(
                api_key=os.getenv(
                    "ANTHROPIC_API_KEY",
                    os.getenv("ANTHROPIC_AUTH_TOKEN", _CLAUDE_ENV.get("ANTHROPIC_AUTH_TOKEN", "")),
                ),
                model=os.getenv(
                    "HARNESS_MODEL",
                    os.getenv("ANTHROPIC_DEFAULT_SONNET_MODEL", _CLAUDE_ENV.get("ANTHROPIC_DEFAULT_SONNET_MODEL", "claude-sonnet-4-6")),
                ),
                base_url=os.getenv("ANTHROPIC_BASE_URL", _CLAUDE_ENV.get("ANTHROPIC_BASE_URL", "")),
            ),
            event_store_path=os.getenv("HARNESS_EVENT_STORE", "./data/events"),
            artifact_path=os.getenv("HARNESS_ARTIFACT_PATH", "./data/artifacts"),
            agent_timeout=int(os.getenv("HARNESS_AGENT_TIMEOUT", "300")),
            max_context_tokens=int(os.getenv("HARNESS_MAX_CONTEXT_TOKENS", "100000")),
            non_interactive=os.getenv("HARNESS_NON_INTERACTIVE", "").lower()
            in ("true", "1", "yes"),
            approval_timeout_sec=int(os.getenv("HARNESS_APPROVAL_TIMEOUT_SEC", "300")),
            cleanup_policy=os.getenv("HARNESS_CLEANUP_POLICY", "on_success"),
            model_routing=ModelRoutingConfig.from_env(),
            memory=MemoryConfig(
                enabled=os.getenv("HARNESS_MEMORY_ENABLED", "true").lower()
                not in ("false", "0", "no"),
                base_path=os.getenv("HARNESS_MEMORY_PATH", "./data/memory"),
                max_entries_per_agent=int(os.getenv("HARNESS_MEMORY_MAX_ENTRIES", "500")),
                max_content_length=int(os.getenv("HARNESS_MEMORY_MAX_LENGTH", "1000")),
                default_ttl_days=int(os.getenv("HARNESS_MEMORY_TTL_DAYS", "90")),
                retrieval_limit=int(os.getenv("HARNESS_MEMORY_RETRIEVAL_LIMIT", "10")),
                decay_half_life_days=float(os.getenv("HARNESS_MEMORY_DECAY_DAYS", "30")),
            ),
            learning=LearningConfig(
                enabled=os.getenv("HARNESS_LEARNING_ENABLED", "true").lower()
                not in ("false", "0", "no"),
                analysis_interval_hours=float(
                    os.getenv("HARNESS_LEARNING_INTERVAL_HOURS", "6.0")
                ),
                min_samples=int(os.getenv("HARNESS_LEARNING_MIN_SAMPLES", "5")),
                max_insights=int(os.getenv("HARNESS_LEARNING_MAX_INSIGHTS", "100")),
                confidence_threshold=float(
                    os.getenv("HARNESS_LEARNING_CONFIDENCE", "0.7")
                ),
                base_path=os.getenv("HARNESS_LEARNING_PATH", "./data/learning"),
            ),
            impact=ImpactConfig(
                enabled=os.getenv("HARNESS_IMPACT_ENABLED", "true").lower()
                not in ("false", "0", "no"),
                base_path=os.getenv("HARNESS_IMPACT_PATH", "./data/impact"),
                coverage_threshold=float(
                    os.getenv("HARNESS_IMPACT_COVERAGE_THRESHOLD", "0.7")
                ),
                max_predicted_files=int(
                    os.getenv("HARNESS_IMPACT_MAX_FILES", "50")
                ),
                confidence_threshold=float(
                    os.getenv("HARNESS_IMPACT_CONFIDENCE", "0.5")
                ),
            ),
            watchdog=WatchdogConfig.from_env(),
        )
