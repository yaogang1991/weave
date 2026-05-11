"""
Agent Worker: the "dumb loop" that calls the LLM and executes tools.
All intelligence lives in the model. Harness just orchestrates.

Enhanced with:
- Context window management (token estimation + message truncation)
- API retry with exponential backoff for transient errors
- Artifact tracking (files created/modified via write/edit tools)
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Iterator

from core.models import AgentMessage, ToolCall, ToolResult, EventType
from core.config import LLMConfig
from core.llm_client import LLMClient
from session.store import SessionStore

# Transient error name fragments that warrant retry
_TRANSIENT_MARKERS = ("rate", "timeout", "connection", "overload", "429", "503", "502")


class AgentWorker:
    """
    Minimal harness loop:
    while has_tool_calls:
        call LLM with messages
        execute tool calls
        feed results back
    """

    def __init__(
        self,
        config: LLMConfig,
        session_store: SessionStore,
        max_context_tokens: int = 100_000,
    ):
        self.config = config
        self.session_store = session_store
        self.llm = LLMClient(config)
        self.max_context_tokens = max_context_tokens
        self.artifacts: list[str] = []

    # -- Public interface ---------------------------------------------------

    def run(
        self,
        session_id: str,
        system_prompt: str,
        user_message: str,
        tools: list[dict],
        tool_executor,
        max_iterations: int = 50,
    ) -> Iterator[AgentMessage]:
        """
        Run the agent loop until no more tool calls or max iterations reached.
        Yields each assistant message for streaming/real-time observation.
        """
        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        self.artifacts = []

        for iteration in range(max_iterations):
            # Truncate context if exceeding token budget
            messages = self._truncate_messages(messages, self.max_context_tokens)

            # Call LLM with retry for transient errors
            assistant_message = self._call_with_retry(messages, tools)

            self.session_store.emit_event(
                session_id,
                EventType.AGENT_MESSAGE,
                assistant_message,
            )

            if "tool_calls" not in assistant_message or not assistant_message["tool_calls"]:
                yield AgentMessage(role="assistant", content=assistant_message.get("content", ""))
                break

            yield AgentMessage(
                role="assistant",
                content=assistant_message.get("content", ""),
                tool_calls=[ToolCall(**tc) for tc in assistant_message["tool_calls"]],
            )

            # Execute tool calls
            tool_results = []
            for tc in assistant_message["tool_calls"]:
                self.session_store.emit_event(
                    session_id,
                    EventType.AGENT_TOOL_USE,
                    tc,
                )

                result = tool_executor.execute(tc["name"], tc.get("arguments", {}))
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result.output if result.success else f"Error: {result.error}",
                })

                # Track artifacts from successful write/edit calls
                if result.success:
                    self._track_artifact(tc["name"], tc.get("arguments", {}))

                self.session_store.emit_event(
                    session_id,
                    EventType.AGENT_TOOL_RESULT,
                    {
                        "tool_call_id": tc["id"],
                        "success": result.success,
                        "output": result.output,
                        "error": result.error,
                        "duration_ms": result.duration_ms,
                    },
                )

            messages.append(assistant_message)
            messages.extend(tool_results)

    # -- Context window management ------------------------------------------

    @staticmethod
    def _estimate_tokens(messages: list[dict]) -> int:
        """Rough token estimation: ~4 chars per token for English/code."""
        total_chars = 0
        for m in messages:
            total_chars += len(m.get("content", ""))
            for tc in m.get("tool_calls", []):
                total_chars += len(str(tc.get("arguments", {})))
        return total_chars // 4

    def _truncate_messages(
        self, messages: list[dict], max_tokens: int
    ) -> list[dict]:
        """Truncate oldest messages, keeping system prompt + last N exchanges."""
        if self._estimate_tokens(messages) <= max_tokens:
            return messages

        system = [m for m in messages if m.get("role") == "system"]
        non_system = [m for m in messages if m.get("role") != "system"]

        # Keep last 20 messages (roughly 10 tool exchanges)
        keep_tail = 20
        if len(non_system) > keep_tail:
            non_system = non_system[-keep_tail:]

        return system + non_system

    # -- API retry with backoff ---------------------------------------------

    def _call_with_retry(
        self, messages: list[dict], tools: list[dict], max_retries: int = 3
    ) -> dict:
        """Call LLM with exponential backoff for transient errors.

        For 429 rate-limit errors, parses the reset time from the error
        message and sleeps until then instead of using short backoff.
        """
        for attempt in range(max_retries + 1):
            try:
                return self.llm.call(messages, tools)
            except Exception as e:
                if attempt == max_retries:
                    raise
                error_name = type(e).__name__.lower()
                error_msg = str(e)
                error_lower = error_msg.lower()

                is_transient = any(
                    t in error_name or t in error_lower
                    for t in _TRANSIENT_MARKERS
                )
                if not is_transient:
                    raise

                # For 429 / rate-limit: parse reset time and wait
                if "429" in error_lower or "rate" in error_lower:
                    wait_sec = self._parse_rate_limit_wait(error_msg)
                    if wait_sec is not None:
                        time.sleep(min(wait_sec + 1, 300))  # cap at 5 min
                        continue

                # Generic transient: exponential backoff
                time.sleep(2 ** attempt)

    @staticmethod
    def _parse_rate_limit_wait(error_msg: str) -> float | None:
        """Parse wait duration from rate limit error messages.

        Supports patterns like:
        - "will reset at 2026-05-12 03:02:22"
        - "retry after 30 seconds"
        - "retry-after: 60"
        """
        # Pattern: datetime reset time
        dt_match = re.search(
            r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})", error_msg
        )
        if dt_match:
            try:
                reset_dt = datetime.strptime(dt_match.group(1), "%Y-%m-%d %H:%M:%S")
                reset_dt = reset_dt.replace(tzinfo=timezone.utc)
                wait = (reset_dt - datetime.now(timezone.utc)).total_seconds()
                return max(wait, 0)
            except ValueError:
                pass

        # Pattern: "retry after N seconds" or "retry-after: N"
        num_match = re.search(r"retry.?(?:after|in)\s*:?\s*(\d+)", error_msg, re.IGNORECASE)
        if num_match:
            return float(num_match.group(1))

        # Pattern: bare number after "retry" (e.g., "retry in 30s")
        s_match = re.search(r"(\d+)\s*s(?:econds?)?", error_msg, re.IGNORECASE)
        if s_match and "retry" in error_msg.lower():
            return float(s_match.group(1))

        return None

    # -- Artifact tracking --------------------------------------------------

    def _track_artifact(self, tool_name: str, arguments: dict) -> None:
        """Track file paths from successful write/edit tool calls."""
        if tool_name in ("write", "edit") and "file_path" in arguments:
            path = arguments["file_path"]
            if path not in self.artifacts:
                self.artifacts.append(path)
