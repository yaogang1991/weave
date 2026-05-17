"""MCP client and skills system models (M3.6)."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class MCPServerStatus(str, Enum):
    """Lifecycle status of an MCP server connection."""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"


class MCPToolInfo(BaseModel):
    """Metadata about a tool discovered from an MCP server."""
    prefixed_name: str          # e.g. "mcp__github__create_issue"
    original_name: str          # e.g. "create_issue"
    server_name: str            # e.g. "github"
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)


class SkillVariable(BaseModel):
    """A single variable definition in a skill."""
    default: str = ""
    description: str = ""
    required: bool = False


class Skill(BaseModel):
    """A reusable prompt template skill definition."""
    name: str
    description: str
    prompt: str
    variables: dict[str, SkillVariable] = Field(default_factory=dict)
    agent_types: list[str] = Field(default_factory=list)  # empty = all agents
    tool_allowlist: list[str] = Field(default_factory=list)
    context_files: list[str] = Field(default_factory=list)
    version: str = "1.0"
