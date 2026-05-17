"""A2A discovery endpoint handler.

Serves the Agent Card at ``/.well-known/agent-card.json`` for
A2A protocol discovery. Designed to be integrated into any
ASGI/WSGI framework (FastAPI, Starlette, aiohttp, etc.).
"""

from __future__ import annotations

import logging
from typing import Any

from a2a.agent_card import build_weave_agent_card
from a2a.models import A2ACard

logger = logging.getLogger(__name__)

# Well-known path per A2A spec
AGENT_CARD_PATH = "/.well-known/agent-card.json"


class AgentCardEndpoint:
    """Serves the A2A Agent Card as JSON.

    Usage with FastAPI::

        from a2a.discovery import AgentCardEndpoint, AGENT_CARD_PATH

        endpoint = AgentCardEndpoint()
        app.get(AGENT_CARD_PATH)(endpoint.handle)
    """

    def __init__(
        self,
        card: A2ACard | None = None,
        base_url: str | None = None,
    ) -> None:
        self._card = card
        self._base_url = base_url
        self._cached_json: str | None = None

    def _get_card(self) -> A2ACard:
        """Return the Agent Card, building it lazily if needed."""
        if self._card is None:
            self._card = build_weave_agent_card(base_url=self._base_url)
        return self._card

    def get_json(self) -> str:
        """Return the Agent Card as a JSON string.

        Caches the result for subsequent calls. Rebuilds if the
        card is replaced via ``set_card()``.
        """
        if self._cached_json is None:
            self._cached_json = self._get_card().model_dump_json(indent=2)
        return self._cached_json

    def get_dict(self) -> dict[str, Any]:
        """Return the Agent Card as a dictionary."""
        return self._get_card().model_dump(mode="json")

    def set_card(self, card: A2ACard) -> None:
        """Replace the Agent Card (invalidates cache)."""
        self._card = card
        self._cached_json = None

    def handle(self) -> dict[str, Any]:
        """Return the Agent Card dict for HTTP handler integration.

        Returns the card as a plain dict suitable for JSON serialization
        by any web framework.
        """
        logger.debug("Serving A2A Agent Card at %s", AGENT_CARD_PATH)
        return self.get_dict()


def build_well_known_response() -> dict[str, Any]:
    """Convenience function: build and return the Agent Card dict.

    Useful for simple integrations that don't need endpoint state.
    """
    card = build_weave_agent_card()
    return card.model_dump(mode="json")
