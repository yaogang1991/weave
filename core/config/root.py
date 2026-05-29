"""Root WeaveConfig — aggregates all sub-configurations."""
from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from core.config.env import _get_non_interactive_env, _CLAUDE_ENV
from core.config.llm import LLMConfig
from core.config.sandbox import SandboxConfig
from core.config.mcp import MCPConfig
from core.config.watchdog import WatchdogConfig
from core.config.routing import ModelRoutingConfig
from core.config.memory import MemoryConfig
from core.config.learning import LearningConfig
from core.config.analysis import ImpactConfig
from core.config.timeout import NodeTimeoutConfig
from core.config.budget import BudgetConfig, TokenEstimationConfig
from core.config.backends import CodexBackendConfig, ClaudeCodeConfig
from core.config.observability import ObservabilityConfig

logger = logging.getLogger(__name__)


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
    default_agent_backend: str = Field(
        default_factory=lambda: os.getenv(
            "WEAVE_DEFAULT_AGENT_BACKEND", "claude_code"
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

    # M4.6: Token Estimation
    token_estimation: TokenEstimationConfig = Field(default_factory=TokenEstimationConfig)

    # M4.4: Codex Backend
    codex: CodexBackendConfig = Field(default_factory=CodexBackendConfig)

    # M4.1: Claude Code Backend
    claude_code: ClaudeCodeConfig = Field(default_factory=ClaudeCodeConfig)

    # M5.1: Observability
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig.from_env)

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
