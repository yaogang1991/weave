"""
Configuration management for the Harness.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class LLMConfig(BaseModel):
    provider: str = "anthropic"  # anthropic, openai
    model: str = "claude-sonnet-4-6"
    api_key: str = Field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    base_url: str = ""
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


class HarnessConfig(BaseModel):
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

    # M2.2: Backend configuration
    default_backend: str = Field(
        default_factory=lambda: os.getenv(
            "HARNESS_DEFAULT_BACKEND", "local"
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

    @classmethod
    def from_yaml(cls, path: str | Path) -> HarnessConfig:
        with open(path, "r") as f:
            data = yaml.safe_load(f)
        return cls(**data)

    @classmethod
    def from_env(cls) -> HarnessConfig:
        """Create config from environment variables."""
        return cls(
            llm=LLMConfig(
                api_key=os.getenv("ANTHROPIC_API_KEY", os.getenv("ANTHROPIC_AUTH_TOKEN", "")),
                model=os.getenv("HARNESS_MODEL", os.getenv("ANTHROPIC_DEFAULT_SONNET_MODEL", "claude-sonnet-4-6")),
                base_url=os.getenv("ANTHROPIC_BASE_URL", ""),
            ),
            event_store_path=os.getenv("HARNESS_EVENT_STORE", "./data/events"),
            artifact_path=os.getenv("HARNESS_ARTIFACT_PATH", "./data/artifacts"),
            agent_timeout=int(os.getenv("HARNESS_AGENT_TIMEOUT", "120")),
            max_context_tokens=int(os.getenv("HARNESS_MAX_CONTEXT_TOKENS", "100000")),
        )
