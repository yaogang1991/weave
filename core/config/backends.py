"""CLI backend configurations (Codex, Claude Code)."""
from __future__ import annotations

import os
from typing import Any, ClassVar

from pydantic import BaseModel, Field, field_validator


class CodexBackendConfig(BaseModel):
    """M4.4: Configuration for the Codex CLI backend."""
    enabled: bool = Field(
        default_factory=lambda: os.getenv("WEAVE_CODEX_ENABLED", "false").lower()
        in ("true", "1", "yes"),
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
    timeout: int = Field(default=600)
    mcp_config: Any = Field(default=None)

    VALID_SANDBOX_MODES: ClassVar[frozenset[str]] = frozenset({
        "workspace-write", "workspace-read", "full-access",
        "none", "readOnly", "dangerFullAccess",
    })

    @field_validator("sandbox_mode")
    @classmethod
    def _validate_sandbox_mode(cls, v: str) -> str:
        if v not in cls.VALID_SANDBOX_MODES:
            raise ValueError(f"sandbox_mode must be one of {cls.VALID_SANDBOX_MODES}, got {v!r}")
        return v


class ClaudeCodeConfig(BaseModel):
    """M4.1: Configuration for ClaudeCodeBackend."""
    enabled: bool = Field(
        default_factory=lambda: os.getenv("WEAVE_CLAUDE_CODE_ENABLED", "false").lower()
        in ("true", "1", "yes"),
    )
    cli_path: str = Field(default_factory=lambda: os.getenv("WEAVE_CLAUDE_CODE_PATH", "claude"))
    model: str = Field(default="")
    max_turns: int = Field(default=0, ge=0)
    permission_mode: str = Field(default="default")
    allowed_tools: list[str] = Field(default_factory=list)
    system_prompt_append: str = Field(default="")
    max_budget_usd: float = Field(default=0.0, ge=0.0)
    timeout_override: int = Field(default=0, ge=0)

    @field_validator("permission_mode")
    @classmethod
    def validate_permission_mode(cls, v: str) -> str:
        allowed = {"default", "plan", "bypassPermissions"}
        if v not in allowed:
            raise ValueError(f"permission_mode must be one of {allowed}, got '{v}'")
        return v

    @field_validator("cli_path")
    @classmethod
    def validate_cli_path(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("cli_path must not be empty")
        return v.strip()
