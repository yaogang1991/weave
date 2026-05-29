"""MCP (Model Context Protocol) configuration."""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class MCPServerConfig(BaseModel):
    """Configuration for a single MCP server connection."""
    name: str
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    default_risk_level: str = "medium"

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
    """Configuration for MCP integration."""
    servers: list[MCPServerConfig] = Field(default_factory=list)
    auto_discover: bool = False
    connection_timeout: int = 30
