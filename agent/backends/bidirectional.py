"""Bidirectional communication protocol for CLI backends.

Defines the message format for stdin/stdout communication with
CLI agents that support --input-format stream-json mode.

Status: Protocol definition only. Implementation deferred to future milestone.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class InitializeRequest(BaseModel):
    """Initialize request sent via stdin when using --input-format stream-json."""

    type: str = "initialize"
    agents: list[dict[str, Any]] = Field(default_factory=list)
    hooks: dict[str, Any] = Field(default_factory=dict)
    mcp_servers: dict[str, Any] = Field(default_factory=dict)


class ToolResultMessage(BaseModel):
    """Tool result message sent via stdin in response to tool_use requests."""

    type: str = "tool_result"
    tool_use_id: str = ""
    content: str = ""


class BidirectionalConfig(BaseModel):
    """Configuration for bidirectional communication mode."""

    enabled: bool = False
    input_format: str = "stream-json"
    supports_tool_result: bool = False
    supports_initialize: bool = False
