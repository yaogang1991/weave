"""
Agent Worker: the "dumb loop" that calls the LLM and executes tools.
All intelligence lives in the model. Harness just orchestrates.

Enhanced with:
- Context window management (token estimation + message truncation)
- API retry with exponential backoff for transient errors
- Artifact tracking (files created/modified via write/edit tools)
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import json
import logging

from core.models import AgentMessage, ToolCall, ToolResult, EventType
from core.config import LLMConfig
from core.llm_client import LLMClient
from session.store import SessionStore

logger = logging.getLogger(__name__)

# Required arguments for known tools — used to validate LLM tool calls
# before execution, catching empty/malformed arguments from some models (#215).
TOOL_REQUIRED_ARGS: dict[str, list[str]] = {
    "write": ["file_path", "content"],
    "edit": ["file_path", "old_string", "new_string"],
    "read": ["file_path"],
    "bash": ["command"],
    "glob": ["pattern"],
    "grep": ["pattern"],
}


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
        base_cwd: str | None = None,
    ):
        self.config = config
        self.session_store = session_store
        self.llm = LLMClient(config)
        self.max_context_tokens = max_context_tokens
        self.artifacts: list[str] = []
        self._base_cwd = Path(base_cwd).resolve() if base_cwd else None

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

            # Call LLM with retry for transient errors (handled in LLMClient)
            assistant_message = self.llm.call(messages, tools)

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
                # Defensive: ensure tool_call_id is present (#169)
                tool_call_id = tc.get("id") or ""
                if not tool_call_id.strip():
                    import uuid
                    tool_call_id = f"tool_{uuid.uuid4().hex[:8]}"
                    tc["id"] = tool_call_id

                self.session_store.emit_event(
                    session_id,
                    EventType.AGENT_TOOL_USE,
                    tc,
                )

                # Validate required arguments before execution (#215)
                tool_name = tc["name"]
                args = tc.get("arguments", {})
                required = TOOL_REQUIRED_ARGS.get(tool_name, [])
                missing = [k for k in required if k not in args or not args[k]]

                if missing:
                    tool_results.append({
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": (
                            f"Error: '{tool_name}' tool is missing required argument(s): "
                            f"{', '.join(missing)}. "
                            f"Your call had arguments: {json.dumps(args)}. "
                            f"Please retry with all required arguments."
                        ),
                    })
                    logger.warning("Tool %s called with missing args: %s", tool_name, missing)
                    continue

                result = tool_executor.execute(tool_name, args)
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": result.output if result.success else f"Error: {result.error}",
                })

                # Track artifacts from successful write/edit calls
                if result.success:
                    self._track_artifact(tc["name"], tc.get("arguments", {}))

                self.session_store.emit_event(
                    session_id,
                    EventType.AGENT_TOOL_RESULT,
                    {
                        "tool_call_id": tool_call_id,
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

    # -- Artifact tracking --------------------------------------------------

    def _track_artifact(self, tool_name: str, arguments: dict) -> None:
        """Track file paths from successful write/edit tool calls.

        Verifies the file actually exists on disk before recording,
        preventing false-positive artifact claims (#158).
        """
        if tool_name in ("write", "edit") and "file_path" in arguments:
            path = arguments["file_path"]
            try:
                p = Path(path)
                if not p.is_absolute() and self._base_cwd:
                    p = self._base_cwd / p
                if not p.is_file() or p.stat().st_size == 0:
                    return  # Missing or empty file — do not claim (#158)
            except OSError:
                return
            if path not in self.artifacts:
                self.artifacts.append(path)
