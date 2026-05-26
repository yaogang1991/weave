"""OTel GenAI metrics instrumentation (#938).

Provides metric instruments following the OpenTelemetry GenAI Metrics Conventions:
- gen_ai.client.token.usage — token counts by model and type
- gen_ai.client.operation.duration — LLM call latency

Plus Weave-specific metrics:
- weave.tool.call.duration — tool call latency
- weave.tool.call.total — tool call count (success/failure)

Gracefully degrades to no-op when opentelemetry-sdk is not installed.
Existing MetricsCollector JSON/Markdown output is preserved.
"""
from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# Lazy-initialized instruments
_meter = None
_setup_done = False

# Instrument references (populated on first use)
_token_usage_hist = None
_operation_duration_hist = None
_tool_call_duration_hist = None
_tool_call_counter = None


def _try_setup_meter():
    """Attempt to get an OTel meter, returning None if not available."""
    global _meter, _setup_done
    global _token_usage_hist, _operation_duration_hist
    global _tool_call_duration_hist, _tool_call_counter

    if _setup_done:
        return _meter
    _setup_done = True

    try:
        from opentelemetry import metrics
        _meter = metrics.get_meter("weave")

        # GenAI standard metrics
        _token_usage_hist = _meter.create_histogram(
            name="gen_ai.client.token.usage",
            description="Measures number of input and output tokens used",
            unit="{token}",
        )
        _operation_duration_hist = _meter.create_histogram(
            name="gen_ai.client.operation.duration",
            description="Measures duration of LLM operations",
            unit="s",
        )

        # Weave-specific metrics
        _tool_call_duration_hist = _meter.create_histogram(
            name="weave.tool.call.duration",
            description="Measures duration of tool calls",
            unit="s",
        )
        _tool_call_counter = _meter.create_counter(
            name="weave.tool.call.total",
            description="Total number of tool calls",
            unit="{call}",
        )

        logger.debug("OTel metrics instruments created")
    except ImportError:
        logger.debug(
            "opentelemetry-sdk not installed — metrics disabled (#938). "
            "Install with: pip install opentelemetry-sdk"
        )
    return _meter


def record_token_usage(
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    provider: str = "unknown",
) -> None:
    """Record LLM token usage metrics (#938)."""
    _try_setup_meter()
    if _token_usage_hist is None:
        return
    attrs = {
        "gen_ai.request.model": model,
        "gen_ai.system": provider,
    }
    if input_tokens > 0:
        _token_usage_hist.record(input_tokens, {**attrs, "gen_ai.token.type": "input"})
    if output_tokens > 0:
        _token_usage_hist.record(output_tokens, {**attrs, "gen_ai.token.type": "output"})


def record_llm_duration(
    duration_s: float,
    model: str,
    provider: str = "unknown",
) -> None:
    """Record LLM operation duration (#938)."""
    _try_setup_meter()
    if _operation_duration_hist is None:
        return
    _operation_duration_hist.record(
        duration_s,
        {"gen_ai.request.model": model, "gen_ai.system": provider},
    )


def record_tool_call(
    tool_name: str,
    duration_s: float,
    success: bool = True,
) -> None:
    """Record tool call metrics (#938)."""
    _try_setup_meter()
    attrs: dict[str, Any] = {"gen_ai.tool.name": tool_name}
    if _tool_call_duration_hist is not None:
        _tool_call_duration_hist.record(duration_s, attrs)
    if _tool_call_counter is not None:
        status = "success" if success else "failure"
        _tool_call_counter.add(1, {**attrs, "weave.tool.call.status": status})


class LLMDurationTracker:
    """Context manager to track LLM call duration (#938).

    Usage::

        with LLMDurationTracker(model="claude-sonnet-4-6", provider="anthropic") as t:
            result = call_llm(...)
        # t.duration is automatically recorded
    """

    def __init__(self, model: str, provider: str = "unknown") -> None:
        self.model = model
        self.provider = provider
        self.duration: float = 0.0
        self._start: float = 0.0

    def __enter__(self) -> LLMDurationTracker:
        self._start = time.monotonic()
        return self

    def __exit__(self, *args: Any) -> None:
        self.duration = time.monotonic() - self._start
        record_llm_duration(self.duration, self.model, self.provider)


class ToolDurationTracker:
    """Context manager to track tool call duration (#938).

    Usage::

        with ToolDurationTracker("write_file") as t:
            result = run_tool(...)
        # t.duration is automatically recorded
    """

    def __init__(self, tool_name: str) -> None:
        self.tool_name = tool_name
        self.duration: float = 0.0
        self._start: float = 0.0
        self.success: bool = True

    def __enter__(self) -> ToolDurationTracker:
        self._start = time.monotonic()
        return self

    def __exit__(self, *args: Any) -> None:
        self.duration = time.monotonic() - self._start
        if args[0] is not None:
            self.success = False
        record_tool_call(self.tool_name, self.duration, self.success)
