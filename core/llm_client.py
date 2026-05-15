"""
LLMClient: Unified LLM API wrapper for Anthropic and OpenAI providers.

Extracted from AgentWorker so that both AgentWorker and IntelligentOrchestrator
can share the same LLM calling logic without the orchestrator depending on
AgentWorker internals.

Includes built-in retry with exponential backoff for transient errors
(rate limits, timeouts, connection issues) — transparent to all callers.

For parallel DAG nodes sharing one process, a global semaphore limits
concurrent API calls to prevent rate-limiting (#300).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone

import anthropic
from openai import OpenAI

from core.config import LLMConfig
from core.exceptions import RateLimitError

logger = logging.getLogger(__name__)

# Transient error name fragments that warrant retry
_TRANSIENT_MARKERS = frozenset(
    ("rate", "timeout", "connection", "overload", "429", "503", "502", "ratelimit")
)

# Process-global semaphore to limit concurrent API calls across all
# parallel DAG nodes (#300).  Initialized on first use.
_global_api_semaphore: threading.Semaphore | None = None
_semaphore_lock = threading.Lock()


def _get_api_semaphore(max_concurrent: int) -> threading.Semaphore | None:
    """Get or create the global API semaphore.

    Returns None when max_concurrent <= 0 (no limit).
    Thread-safe: only creates once.
    """
    global _global_api_semaphore
    if max_concurrent <= 0:
        return None
    if _global_api_semaphore is not None:
        return _global_api_semaphore
    with _semaphore_lock:
        if _global_api_semaphore is None:
            _global_api_semaphore = threading.Semaphore(max_concurrent)
            logger.info(
                "API concurrency limit: %d concurrent requests (#300)",
                max_concurrent,
            )
    return _global_api_semaphore


class LLMClient:
    """
    Thin wrapper around Anthropic / OpenAI SDK.

    Provides a unified ``call(messages, tools) -> dict`` interface so callers
    don't need to know which provider is in use.
    """

    def __init__(self, config: LLMConfig, max_retries: int = 3):
        self.config = config
        self.max_retries = max_retries
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

    def call(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_retries: int | None = None,
    ) -> dict:
        """
        Call the configured LLM with automatic retry for transient errors.

        Retries on rate limits, timeouts, and connection issues with
        exponential backoff. For 429 errors, parses the reset time from
        the error message and waits until then instead of short backoff.

        Args:
            messages: Chat messages in OpenAI-style format.
            tools: Tool schemas (OpenAI format).
            max_retries: Override default retry count for this call.

        Returns:
            dict with keys: ``role``, ``content``, and optionally
            ``tool_calls``.
        """
        retries = max_retries if max_retries is not None else self.max_retries
        for attempt in range(retries + 1):
            try:
                return self._call_once(messages, tools)
            except Exception as e:
                if attempt == retries:
                    # Rate-limit exhausted all retries → RateLimitError (#360).
                    # This signals upstream layers that the failure was due to
                    # rate-limiting, so retry budgets should NOT be consumed.
                    error_lower = str(e).lower()
                    if "429" in error_lower or "rate" in error_lower:
                        raise RateLimitError(
                            provider=self.config.provider,
                            model=self.config.model,
                            retries=retries,
                        ) from e
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
                        time.sleep(min(wait_sec + 1, 60))  # cap at 60s (#360)
                        continue

                # Generic transient: exponential backoff
                time.sleep(2 ** attempt)

    def _call_once(
        self, messages: list[dict], tools: list[dict] | None = None
    ) -> dict:
        """Single LLM call — no retry. Acquires global semaphore if configured (#300)."""
        sem = _get_api_semaphore(self.config.max_concurrent_api)
        if sem is not None:
            logger.debug("Acquiring API semaphore (limit=%d)", self.config.max_concurrent_api)
            sem.acquire()
            try:
                return self._do_call(messages, tools)
            finally:
                sem.release()
        return self._do_call(messages, tools)

    def _do_call(
        self, messages: list[dict], tools: list[dict] | None = None
    ) -> dict:
        """Dispatch to provider-specific call."""
        if self.config.provider == "anthropic":
            return self._call_anthropic(messages, tools or [])
        else:
            return self._call_openai(messages, tools or [])

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
                reset_dt = datetime.strptime(
                    dt_match.group(1), "%Y-%m-%d %H:%M:%S"
                )
                reset_dt = reset_dt.replace(tzinfo=timezone.utc)
                wait = (reset_dt - datetime.now(timezone.utc)).total_seconds()
                return max(wait, 0)
            except ValueError:
                pass

        # Pattern: "retry after N seconds" or "retry-after: N"
        num_match = re.search(
            r"retry.?(?:after|in)\s*:?\s*(\d+)", error_msg, re.IGNORECASE
        )
        if num_match:
            return float(num_match.group(1))

        # Pattern: bare number after "retry" (e.g., "retry in 30s")
        s_match = re.search(
            r"(\d+)\s*s(?:econds?)?", error_msg, re.IGNORECASE
        )
        if s_match and "retry" in error_msg.lower():
            return float(s_match.group(1))

        return None

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
