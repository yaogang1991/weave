"""StreamMessage model and StreamParser for NDJSON event streams.

Parses line-delimited JSON from CLI backends (claude --output-format stream-json)
into typed StreamMessage objects.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

__all__ = ["StreamMessage", "StreamParser"]


class StreamMessage(BaseModel):
    """A single parsed message from an NDJSON event stream."""

    raw_type: str  # "assistant" / "user" / "result" / "system"
    data: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class StreamParser:
    """Incremental NDJSON line parser for CLI event streams."""

    # Event types that indicate productive model output — receiving one
    # resets the thinking-token streak (#1137).
    _PRODUCTIVE_TYPES: set[str] = {
        "assistant", "user", "result", "tool_use", "tool_result",
    }

    def __init__(self) -> None:
        self._messages: list[StreamMessage] = []
        # #1137: consecutive thinking-token events with no productive output.
        self._thinking_streak = 0

    # All known stream-json event types from Claude CLI.
    _KNOWN_TYPES: set[str] = {
        "assistant", "user", "result", "system",
        "content_block_start", "content_block_delta", "content_block_stop",
        "tool_use", "tool_result",
    }

    def feed_line(self, raw: str) -> StreamMessage | None:
        line = raw.strip()
        if not line:
            return None
        try:
            data = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            logger.debug("stream-json: skipping non-JSON line: %s", line[:100])
            return None
        raw_type = data.get("type", "")
        if not raw_type:
            return None
        if raw_type not in self._KNOWN_TYPES:
            logger.debug("stream-json: unknown type %s", raw_type)
            return None
        msg = StreamMessage(raw_type=raw_type, data=data)
        self._messages.append(msg)
        # #1137: track thinking-token floods for fast-fail detection.
        if self._is_thinking_tokens(msg):
            self._thinking_streak += 1
        elif msg.raw_type in self._PRODUCTIVE_TYPES:
            self._thinking_streak = 0
        return msg

    @staticmethod
    def _is_thinking_tokens(msg: StreamMessage) -> bool:
        """A system event carrying the thinking_tokens subtype."""
        return (
            msg.raw_type == "system"
            and msg.data.get("subtype") == "thinking_tokens"
        )

    @property
    def thinking_streak(self) -> int:
        """Consecutive thinking-token events since last productive output (#1137)."""
        return self._thinking_streak

    @property
    def messages(self) -> list[StreamMessage]:
        return list(self._messages)
