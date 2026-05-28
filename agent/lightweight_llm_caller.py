"""
LightweightLLMCaller: single-shot LLM call for planner/evaluator nodes (M6.3).

No tool loop, no stuck detection, no context management, no artifacts.
Just: build messages -> call LLM -> record event -> return text.

Preserves: LLMClient, SessionStore event recording, token tracking.
"""

from __future__ import annotations

import logging
import threading

from core.config import LLMConfig
from core.llm_client import LLMClient
from core.event_models import EventType
from session.store import SessionStore

logger = logging.getLogger(__name__)


class LightweightLLMCaller:
    """Single-shot LLM caller for nodes that need one call, not a tool loop."""

    def __init__(self, config: LLMConfig, session_store: SessionStore):
        self.llm = LLMClient(config)
        self.session_store = session_store
        self.token_usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}

    async def call(
        self,
        system_prompt: str,
        user_message: str,
        session_id: str,
        cancel_event: threading.Event | None = None,
    ) -> str:
        """Make a single LLM call and return the text response.

        Args:
            system_prompt: System instructions for the LLM.
            user_message: User/task message for the LLM.
            session_id: Session for event recording.
            cancel_event: Cooperative cancellation (aborts on set).

        Returns:
            The assistant's text response.
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        response = self.llm.call(messages, cancel_event=cancel_event)

        # Track token usage
        usage = response.get("usage", {})
        self.token_usage["input_tokens"] += usage.get("input_tokens", 0)
        self.token_usage["output_tokens"] += usage.get("output_tokens", 0)

        # Emit AGENT_MESSAGE event to session log
        self.session_store.emit_event(
            session_id,
            EventType.AGENT_MESSAGE,
            response,
        )

        return response.get("content", "")
