"""
LLMClient: Unified LLM API wrapper for Anthropic and OpenAI providers.

Extracted from AgentWorker so that both AgentWorker and IntelligentOrchestrator
can share the same LLM calling logic without the orchestrator depending on
AgentWorker internals.

Includes built-in retry with exponential backoff for transient errors
(rate limits, timeouts, connection issues) — transparent to all callers.

For parallel DAG nodes sharing one process, a global semaphore limits
concurrent API calls to prevent rate-limiting (#300).

Hard per-call timeout (#401): wraps each SDK call in a thread with a
wall-clock deadline. This catches cases where the SDK's own HTTP timeout
fails to fire (e.g., silently dropped TCP connections where the socket
read blocks indefinitely).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone

import anthropic
import httpx
from openai import OpenAI

from core.config import LLMConfig
from core.exceptions import RateLimitError
from monitoring.otel import start_span  # noqa: E402 — optional OTel (#509)

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
        # Hard per-call wall-clock timeout (#401).  If the SDK's own httpx
        # read timeout fails to fire (silently dropped TCP connection), this
        # acts as a safety net so the call always returns within a bounded
        # time.  Set to 2× the configured timeout + 30s buffer.
        self._hard_timeout = config.timeout * 2 + 30

    @staticmethod
    def _parse_tool_arguments(raw: str | None) -> dict:
        """Safely parse tool call arguments from LLM response (#381).

        Handles GLM/other backends that return empty strings, None,
        or malformed JSON as arguments.
        """
        if raw is None or not raw.strip():
            return {}
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            logger.warning("Malformed tool arguments, defaulting to {}: %s", raw[:200])
            return {}

    def _create_client(self):
        # Explicit httpx.Timeout with separate connect/read/write/pool
        # (#401, #367). A plain int timeout can fail to fire when a TCP
        # connection is silently dropped — the socket read blocks indefinitely
        # because no timeout applies at the socket level.  Setting connect and
        # read separately ensures each phase has its own deadline.
        timeout = httpx.Timeout(
            connect=30.0,
            read=float(self.config.timeout),
            write=30.0,
            pool=30.0,
        )
        if self.config.provider == "anthropic":
            return anthropic.Anthropic(
                api_key=self.config.api_key,
                base_url=self.config.base_url or None,
                timeout=timeout,
            )
        else:
            return OpenAI(
                api_key=self.config.api_key,
                base_url=self.config.base_url or None,
                timeout=timeout,
            )

    def call(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        max_retries: int | None = None,
        agent_timeout: float | None = None,
        tool_choice: dict | None = None,
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
            agent_timeout: Node-level timeout in seconds. When provided,
                cumulative rate-limit sleep is tracked and the call bails
                early with RateLimitError if sleep exceeds 50% of this
                budget (#432).

        Returns:
            dict with keys: ``role``, ``content``, and optionally
            ``tool_calls``.
        """
        retries = max_retries if max_retries is not None else self.max_retries
        # #432: Track cumulative sleep to bail before consuming too much
        # of the agent's wall-clock timeout budget.
        sleep_budget = (agent_timeout * 0.5) if agent_timeout else None
        cumulative_sleep = 0.0

        for attempt in range(retries + 1):
            try:
                return self._call_once(messages, tools, tool_choice)
            except Exception as e:
                if attempt == retries:
                    # Rate-limit exhausted all retries → RateLimitError (#360).
                    # This signals upstream layers that the failure was due to
                    # rate-limiting, so retry budgets should NOT be consumed.
                    error_lower = str(e).lower()
                    if (
                        "429" in error_lower
                        or "rate_limit" in error_lower
                        or "ratelimit" in error_lower
                    ):
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
                if (
                    "429" in error_lower
                    or "rate_limit" in error_lower
                    or "ratelimit" in error_lower
                ):
                    # #432: Bail early if cumulative sleep already ate
                    # more than 50% of the agent timeout budget.
                    if sleep_budget is not None and cumulative_sleep >= sleep_budget:
                        raise RateLimitError(
                            provider=self.config.provider,
                            model=self.config.model,
                            retries=retries,
                        ) from e

                    wait_sec = self._parse_rate_limit_wait(error_msg)
                    if wait_sec is not None:
                        actual_sleep = min(wait_sec + 1, 60)
                        time.sleep(actual_sleep)
                        cumulative_sleep += actual_sleep
                        continue

                # Generic transient: exponential backoff
                backoff = 2 ** attempt
                time.sleep(backoff)
                cumulative_sleep += backoff

    def _call_once(
        self, messages: list[dict], tools: list[dict] | None = None,
        tool_choice: dict | None = None,
    ) -> dict:
        """Single LLM call with hard wall-clock timeout (#401, #367).

        Wraps the actual call in a separate thread with a deadline.  If the
        SDK's own httpx timeout fails to fire (silently dropped TCP), the
        hard timeout ensures we don't block indefinitely.  The stuck thread
        becomes a daemon that cleans up when the process exits.

        Semaphore is acquired in the main thread so the permit is released
        immediately on hard timeout, preventing permit leak and deadlock (#367).
        """
        result: dict | None = None
        exc: Exception | None = None

        def _target():
            nonlocal result, exc
            try:
                result = self._do_call(messages, tools, tool_choice)
            except Exception as e:
                exc = e

        sem = _get_api_semaphore(self.config.max_concurrent_api)
        if sem is not None:
            logger.debug("Acquiring API semaphore (limit=%d)", self.config.max_concurrent_api)
            sem.acquire()
        try:
            thread = threading.Thread(target=_target, daemon=True)
            thread.start()
            thread.join(timeout=self._hard_timeout)
            if thread.is_alive():
                logger.error(
                    "LLM call exceeded hard timeout (%ds). "
                    "SDK timeout (%ds) did not fire — possible hung connection (#401). "
                    "provider=%s model=%s",
                    self._hard_timeout, self.config.timeout,
                    self.config.provider, self.config.model,
                )
                raise TimeoutError(
                    f"LLM call exceeded hard timeout of {self._hard_timeout}s "
                    f"(SDK timeout: {self.config.timeout}s). "
                    f"Provider: {self.config.provider}, Model: {self.config.model}"
                )
        finally:
            if sem is not None:
                sem.release()
        if exc is not None:
            raise exc
        if result is None:
            raise RuntimeError("LLM call returned no result")
        return result

    def _do_call(
        self, messages: list[dict], tools: list[dict] | None = None,
        tool_choice: dict | None = None,
    ) -> dict:
        """Dispatch to provider-specific call with OTel span (#509)."""
        with start_span("llm.call", {
            "gen_ai.system": self.config.provider,
            "gen_ai.request.model": self.config.model,
        }) as span:
            try:
                if self.config.provider == "anthropic":
                    result = self._call_anthropic(messages, tools or [], tool_choice)
                else:
                    result = self._call_openai(messages, tools or [], tool_choice)
                span.set_attribute("gen_ai.response.finish_reason", "completed")
                return result
            except Exception as e:
                span.set_attribute("gen_ai.response.finish_reason", "error")
                span.record_exception(e)
                raise

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

    def _call_anthropic(
        self, messages: list[dict], tools: list[dict],
        tool_choice: dict | None = None,
    ) -> dict:
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
            # Mark system prompt for prompt caching (#503).
            # Anthropic's prompt caching uses cache_control markers on
            # content blocks. The system field accepts a list of blocks
            # with cache_control to enable prefix caching.
            kwargs["system"] = [
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                },
            ]
        if tools:
            kwargs["tools"] = tools
        if tool_choice:
            kwargs["tool_choice"] = tool_choice

        response = self._client.messages.create(**kwargs)

        # Log cache usage stats from response (#503)
        if hasattr(response, "usage") and response.usage:
            cache_read = getattr(response.usage, "cache_read_input_tokens", 0) or 0
            cache_creation = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
            if cache_read or cache_creation:
                logger.info(
                    "Prompt cache stats: %d tokens read from cache, "
                    "%d tokens written to cache (#503)",
                    cache_read, cache_creation,
                )

        msg: dict = {"role": "assistant", "content": ""}

        tool_calls = []
        for block in response.content:
            if block.type == "text":
                msg["content"] += block.text
            elif block.type == "tool_use":
                tool_calls.append({
                    "id": block.id,
                    "name": block.name,
                    "arguments": block.input if isinstance(block.input, dict) else {},
                })

        if tool_calls:
            msg["tool_calls"] = tool_calls

        return msg

    # -- OpenAI -----------------------------------------------------------

    def _call_openai(
        self, messages: list[dict], tools: list[dict],
        tool_choice: dict | None = None,
    ) -> dict:
        response = self._client.chat.completions.create(
            model=self.config.model,
            messages=messages,
            tools=tools if tools else None,
            tool_choice=tool_choice if tool_choice else ("auto" if tools else None),
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
                    "arguments": LLMClient._parse_tool_arguments(tc.function.arguments),
                }
                for tc in choice.message.tool_calls
            ]

        return msg
