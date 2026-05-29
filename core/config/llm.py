"""LLM configuration: LLMConfig, ModelRoute, ModelRoutingConfig."""

from __future__ import annotations

import os
from pathlib import Path
import yaml
from pydantic import BaseModel, Field

from core.config.env import _CLAUDE_ENV, infer_provider


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
