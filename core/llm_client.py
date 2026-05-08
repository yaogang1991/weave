"""
LLMClient: Unified LLM API wrapper for Anthropic and OpenAI providers.

Extracted from AgentWorker so that both AgentWorker and IntelligentOrchestrator
can share the same LLM calling logic without the orchestrator depending on
AgentWorker internals.
"""

from __future__ import annotations

import json

import anthropic
from openai import OpenAI

from core.config import LLMConfig


class LLMClient:
    """
    Thin wrapper around Anthropic / OpenAI SDK.

    Provides a unified ``call(messages, tools) -> dict`` interface so callers
    don't need to know which provider is in use.
    """

    def __init__(self, config: LLMConfig):
        self.config = config
        self._client = self._create_client()

    def _create_client(self):
        if self.config.provider == "anthropic":
            return anthropic.Anthropic(
                api_key=self.config.api_key,
                base_url=self.config.base_url or None,
                timeout=self.config.timeout,
            )
        else:
            return OpenAI(
                api_key=self.config.api_key,
                base_url=self.config.base_url or None,
                timeout=self.config.timeout,
            )

    def call(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        """
        Call the configured LLM.

        Args:
            messages: Chat messages in OpenAI-style format.
                System messages are extracted for Anthropic automatically.
            tools: Tool schemas (OpenAI format). Pass ``None`` or ``[]`` to omit.

        Returns:
            dict with keys: ``role`` ("assistant"), ``content`` (str),
            and optionally ``tool_calls`` (list of dicts).
        """
        if self.config.provider == "anthropic":
            return self._call_anthropic(messages, tools or [])
        else:
            return self._call_openai(messages, tools or [])

    # -- Anthropic --------------------------------------------------------

    def _call_anthropic(self, messages: list[dict], tools: list[dict]) -> dict:
        """Call Anthropic API with proper message format conversion."""
        system_prompt = None
        anthropic_messages = []

        # Group consecutive tool_result messages into a single user turn
        # as required by the Anthropic Messages API.
        pending_tool_results: list[dict] = []

        def flush_tool_results():
            nonlocal pending_tool_results
            if pending_tool_results:
                anthropic_messages.append({
                    "role": "user",
                    "content": pending_tool_results,
                })
                pending_tool_results = []

        for msg in messages:
            if msg.get("role") == "system":
                system_prompt = msg.get("content", "")
                continue

            if msg.get("role") == "assistant":
                flush_tool_results()
                content_blocks = []
                text_content = msg.get("content", "")
                if text_content:
                    content_blocks.append({"type": "text", "text": text_content})
                for tc in msg.get("tool_calls", []):
                    content_blocks.append({
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": tc["name"],
                        "input": tc["arguments"],
                    })
                anthropic_messages.append({"role": "assistant", "content": content_blocks})

            elif msg.get("role") == "tool":
                # Accumulate tool results; they will be flushed when we hit
                # a non-tool message or end of iteration.
                pending_tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": msg["tool_call_id"],
                    "content": msg["content"],
                })

            else:
                flush_tool_results()
                anthropic_messages.append(msg)

        flush_tool_results()

        kwargs = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
            "messages": anthropic_messages,
        }
        if system_prompt:
            kwargs["system"] = system_prompt
        if tools:
            kwargs["tools"] = tools

        response = self._client.messages.create(**kwargs)

        msg: dict = {"role": "assistant", "content": ""}

        tool_calls = []
        for block in response.content:
            if block.type == "text":
                msg["content"] += block.text
            elif block.type == "tool_use":
                tool_calls.append({
                    "id": block.id,
                    "name": block.name,
                    "arguments": block.input,
                })

        if tool_calls:
            msg["tool_calls"] = tool_calls

        return msg

    # -- OpenAI -----------------------------------------------------------

    def _call_openai(self, messages: list[dict], tools: list[dict]) -> dict:
        response = self._client.chat.completions.create(
            model=self.config.model,
            messages=messages,
            tools=tools if tools else None,
            max_tokens=self.config.max_tokens,
            temperature=self.config.temperature,
        )

        choice = response.choices[0]
        msg: dict = {
            "role": "assistant",
            "content": choice.message.content or "",
        }

        if choice.message.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": json.loads(tc.function.arguments),
                }
                for tc in choice.message.tool_calls
            ]

        return msg
