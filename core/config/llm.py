"""LLM configuration models."""
from __future__ import annotations

import os

from pydantic import BaseModel, Field

from core.config.env import _CLAUDE_ENV


class LLMConfig(BaseModel):
    provider: str = "anthropic"
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
    max_concurrent_api: int = Field(
        default_factory=lambda: int(os.getenv("WEAVE_MAX_CONCURRENT_API", "0"))
    )
