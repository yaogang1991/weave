"""
Agent Worker: the "dumb loop" that calls the LLM and executes tools.
All intelligence lives in the model. Harness just orchestrates.
"""

from __future__ import annotations

from typing import Iterator

from core.models import AgentMessage, ToolCall, ToolResult, EventType
from core.config import LLMConfig
from core.llm_client import LLMClient
from session.store import SessionStore


class AgentWorker:
    """
    Minimal harness loop:
    while has_tool_calls:
        call LLM with messages
        execute tool calls
        feed results back
    """

    def __init__(self, config: LLMConfig, session_store: SessionStore):
        self.config = config
        self.session_store = session_store
        self.llm = LLMClient(config)

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

        for iteration in range(max_iterations):
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
