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

    def __init__(self) -> None:
        self._messages: list[StreamMessage] = []

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
        return msg

    @property
    def messages(self) -> list[StreamMessage]:
        return list(self._messages)
