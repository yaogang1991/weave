"""A2A protocol models (Agent Card, Skills, Capabilities).

Based on the A2A v1.0 specification:
https://a2a-protocol.org/latest/specification/

The Agent Card is the discovery document that describes an A2A-compatible
agent's identity, capabilities, skills, and connection details. It is
served at the well-known endpoint ``/.well-known/agent-card.json``.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class A2ATaskState(str, Enum):
    """Lifecycle states for an A2A task."""

    SUBMITTED = "submitted"
    WORKING = "working"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    INPUT_REQUIRED = "input_required"
    REJECTED = "rejected"


class A2ASkill(BaseModel):
    """A skill advertised by an agent in its Agent Card.

    Skills describe discrete capabilities that external agents can invoke
    via the A2A protocol.
    """

    id: str
    name: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)


class A2ACapabilities(BaseModel):
    """Capability flags advertised in the Agent Card.

    These signal which optional A2A features the agent supports.
    """

    streaming: bool = False
    push_notifications: bool = False
    extended_agent_card: bool = False


class A2AInterface(BaseModel):
    """A protocol binding supported by the agent.

    Each interface declares a URL, binding type (e.g. "json-rpc"),
    and protocol version.
    """

    url: str
    protocol_binding: str = "json-rpc"
    protocol_version: str = "1.0"


class A2AProvider(BaseModel):
    """Organization or entity providing the agent."""

    name: str
    url: str = ""


class A2ASecurityScheme(BaseModel):
    """Security scheme definition for agent authentication."""

    scheme_type: str
    description: str = ""


class A2ACard(BaseModel):
    """Agent Card — the A2A discovery document.

    Describes an agent's identity, capabilities, skills, and how to
    connect to it. Served at ``/.well-known/agent-card.json``.

    Reference: A2A v1.0 specification, Section 4 (Agent Card).
    """

    name: str
    description: str
    version: str = "1.0.0"
    provider: A2AProvider | None = None
    supported_interfaces: list[A2AInterface] = Field(default_factory=list)
    capabilities: A2ACapabilities = Field(default_factory=A2ACapabilities)
    default_input_modes: list[str] = Field(
        default_factory=lambda: ["text/plain"]
    )
    default_output_modes: list[str] = Field(
        default_factory=lambda: ["text/plain"]
    )
    skills: list[A2ASkill] = Field(default_factory=list)
    security_schemes: dict[str, A2ASecurityScheme] = Field(
        default_factory=dict
    )
    metadata: dict[str, Any] = Field(default_factory=dict)
