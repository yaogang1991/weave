"""A2A (Agent-to-Agent) protocol support for Weave.

Implements the A2A v1.0 specification for cross-framework agent interop.
https://a2a-protocol.org/latest/specification/

Phase 1 (P0): Agent Card models and discovery endpoint.
"""

from a2a.models import (
    A2ACapabilities,
    A2ACard,
    A2AInterface,
    A2ASkill,
)

__all__ = [
    "A2ACapabilities",
    "A2ACard",
    "A2AInterface",
    "A2ASkill",
]
