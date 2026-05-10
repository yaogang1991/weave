"""
Configuration management for the Harness.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field


def _load_claude_settings() -> dict[str, str]:
    """Load env vars from ~/.claude/settings-kimi.json if present."""
    settings_path = Path.home() / ".claude" / "settings-kimi.json"
    if settings_path.exists():
        try:
            with open(settings_path, "r") as f:
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


class SandboxConfig(BaseModel):
    enabled: bool = True
    runtime: str = "docker"  # docker, bubblewrap, direct
    image: str = "python:3.11-slim"
    network_mode: str = "none"  # none, bridge
    memory_limit: str = "512m"
    cpu_limit: float = 1.0
    timeout: int = 300
    credential_proxy: bool = True


class MCPConfig(BaseModel):
    servers: list[dict[str, Any]] = Field(default_factory=list)
    auto_discover: bool = False


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
        with open(path, "r") as f:
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


class HarnessConfig(BaseModel):
    model_config = ConfigDict(validate_default=True)

    llm: LLMConfig = Field(default_factory=LLMConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)
    event_store_path: str = "./data/events"
    artifact_path: str = "./data/artifacts"
    checkpoint_interval: int = 10  # events
    max_context_messages: int = 50
    agent_timeout: int = 120  # seconds per agent execution
    max_context_tokens: int = 100000  # token threshold for context truncation
    log_level: str = "INFO"

    # M2.2: Isolation dimensions (replaces old BackendType)
    workspace_isolation: str = Field(
        default_factory=lambda: os.getenv(
            "HARNESS_WORKSPACE_ISOLATION",
            os.getenv("HARNESS_DEFAULT_BACKEND", "local"),  # legacy fallback
        )
    )
    execution_sandbox: str = Field(
        default_factory=lambda: os.getenv(
            "HARNESS_EXECUTION_SANDBOX", "local"
        )
    )
    backend_base_path: str = Field(
        default_factory=lambda: os.getenv(
            "HARNESS_BACKEND_BASE_PATH", "./data/backends"
        )
    )
    workspace_isolation_by_risk: dict[str, str] = Field(
        default_factory=lambda: {
            "low": os.getenv("HARNESS_WORKSPACE_LOW", os.getenv("HARNESS_BACKEND_LOW", "local")),
            "medium": os.getenv("HARNESS_WORKSPACE_MEDIUM", os.getenv("HARNESS_BACKEND_MEDIUM", "local")),
            "high": os.getenv("HARNESS_WORKSPACE_HIGH", os.getenv("HARNESS_BACKEND_HIGH", "worktree")),
            "critical": os.getenv(
                "HARNESS_WORKSPACE_CRITICAL",
                os.getenv("HARNESS_BACKEND_CRITICAL", "worktree"),
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

    # M2: Cleanup policy for execution backends
    # "always" = always cleanup, "on_success" = preserve on failure, "never" = always preserve
    cleanup_policy: str = Field(
        default_factory=lambda: os.getenv(
            "HARNESS_CLEANUP_POLICY", "on_success"
        ),
        pattern=r"^(always|on_success|never)$",
    )

    # M3.1: Multi-model routing
    model_routing: ModelRoutingConfig = Field(default_factory=ModelRoutingConfig)

    @classmethod
    def from_yaml(cls, path: str | Path) -> HarnessConfig:
        with open(path, "r") as f:
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
            agent_timeout=int(os.getenv("HARNESS_AGENT_TIMEOUT", "120")),
            max_context_tokens=int(os.getenv("HARNESS_MAX_CONTEXT_TOKENS", "100000")),
            non_interactive=os.getenv("HARNESS_NON_INTERACTIVE", "").lower()
            in ("true", "1", "yes"),
            approval_timeout_sec=int(os.getenv("HARNESS_APPROVAL_TIMEOUT_SEC", "300")),
            cleanup_policy=os.getenv("HARNESS_CLEANUP_POLICY", "on_success"),
            model_routing=ModelRoutingConfig.from_env(),
        )
