"""Multi-model routing configuration."""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from core.config.env import infer_provider


class ModelRoute(BaseModel):
    """Model assignment for a specific agent type or role."""
    provider: str = ""
    model: str = ""
    temperature: float | None = None
    max_tokens: int | None = None


class ModelRoutingConfig(BaseModel):
    """Configuration for per-agent-type model selection."""
    routing: dict[str, ModelRoute] = Field(default_factory=dict)
    fallback_chain: list[str] = Field(
        default_factory=lambda: ["claude-sonnet-4-6"]
    )

    @classmethod
    def from_env(cls) -> ModelRoutingConfig:
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
        fallback_str = os.getenv("WEAVE_MODEL_FALLBACK", "claude-sonnet-4-6")
        fallback_chain = [m.strip() for m in fallback_str.split(",") if m.strip()]
        return cls(routing=routing, fallback_chain=fallback_chain)

    @classmethod
    def from_yaml(cls, path: str | Path) -> ModelRoutingConfig:
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
