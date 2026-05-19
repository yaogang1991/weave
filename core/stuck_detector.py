"""M4.2: Stuck pattern detection for agent loops.

Extracts and extends the inline stuck detection from agent/worker.py
(empty args, degenerate args) into a composable, testable class.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class StuckPattern(str, Enum):
    EMPTY_ARGS = "empty_args"
    DEGENERATE_ARGS = "degenerate_args"
    REPEAT_CONTENT = "repeat_content"


@dataclass
class StuckResult:
    is_stuck: bool
    pattern: StuckPattern | None = None
    consecutive_count: int = 0
    threshold: int = 0
    message: str = ""


class StuckDetector:
    """Detects when an agent loop is stuck in an unrecoverable pattern.

    Replaces the inline counters in agent/worker.py (EMPTY_TOOL_CALL_LIMIT,
    DEGENERATE_CALL_LIMIT) with a composable, configurable, testable class.

    Create one instance per ``run()`` invocation and discard afterward.
    """

    def __init__(
        self,
        empty_call_limit: int = 10,
        degenerate_call_limit: int = 3,
        repeat_content_limit: int = 5,
    ) -> None:
        self._empty_call_limit = empty_call_limit
        self._degenerate_call_limit = degenerate_call_limit
        self._repeat_content_limit = repeat_content_limit

        self._consecutive_empty: int = 0
        self._consecutive_degenerate: int = 0
        self._consecutive_repeat: int = 0
        self._last_content: str = ""

    def observe(
        self,
        assistant_message: dict[str, Any],
        any_tool_executed: bool,
    ) -> StuckResult:
        """Observe one LLM response. Call once per iteration.

        Args:
            assistant_message: The raw dict from LLMClient.call().
            any_tool_executed: Whether at least one tool call was successfully
                executed (passed validation).

        Returns:
            StuckResult with is_stuck=True when a threshold is exceeded.
        """
        tool_calls = assistant_message.get("tool_calls", [])
        content = assistant_message.get("content", "")

        # Check repeat content (no tool calls, same text)
        if not tool_calls and content and content == self._last_content:
            self._consecutive_repeat += 1
            if self._consecutive_repeat >= self._repeat_content_limit:
                return StuckResult(
                    is_stuck=True,
                    pattern=StuckPattern.REPEAT_CONTENT,
                    consecutive_count=self._consecutive_repeat,
                    threshold=self._repeat_content_limit,
                    message=(
                        f"Agent produced identical text {self._consecutive_repeat} "
                        f"times without tool calls."
                    ),
                )
        else:
            self._consecutive_repeat = 0
        self._last_content = content if not tool_calls else ""

        # No tool calls or all invalid -> track empty iterations
        all_invalid = bool(tool_calls) and not any_tool_executed
        if all_invalid:
            self._consecutive_empty += 1
        else:
            self._consecutive_empty = 0
            self._consecutive_degenerate = 0
            return StuckResult(is_stuck=False)

        # Check degenerate args (all tool calls have empty dict args)
        all_empty_dict = all(
            tc.get("arguments") == {} for tc in tool_calls
        )
        if all_empty_dict:
            self._consecutive_degenerate += 1
            if self._consecutive_degenerate >= self._degenerate_call_limit:
                return StuckResult(
                    is_stuck=True,
                    pattern=StuckPattern.DEGENERATE_ARGS,
                    consecutive_count=self._consecutive_degenerate,
                    threshold=self._degenerate_call_limit,
                    message=(
                        f"Agent loop stuck: {self._consecutive_degenerate} "
                        f"consecutive iterations with completely empty tool "
                        f"call args."
                    ),
                )
        else:
            self._consecutive_degenerate = 0

        # Check empty args threshold (broader circuit breaker)
        if self._consecutive_empty >= self._empty_call_limit:
            return StuckResult(
                is_stuck=True,
                pattern=StuckPattern.EMPTY_ARGS,
                consecutive_count=self._consecutive_empty,
                threshold=self._empty_call_limit,
                message=(
                    f"Agent loop stuck: {self._consecutive_empty} consecutive "
                    f"iterations with invalid tool call args."
                ),
            )

        return StuckResult(is_stuck=False)

    def reset(self) -> None:
        """Reset all counters. Call between node executions."""
        self._consecutive_empty = 0
        self._consecutive_degenerate = 0
        self._consecutive_repeat = 0
        self._last_content = ""

    @property
    def state(self) -> dict[str, int]:
        """Current counter states for logging/debugging."""
        return {
            "consecutive_empty": self._consecutive_empty,
            "consecutive_degenerate": self._consecutive_degenerate,
            "consecutive_repeat": self._consecutive_repeat,
        }
