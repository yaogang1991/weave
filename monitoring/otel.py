"""OpenTelemetry integration for Weave (#509).

Provides optional OTel tracing for LLM calls and DAG execution.
Gracefully degrades to no-op when opentelemetry is not installed.

Uses GenAI Semantic Conventions:
https://opentelemetry.io/docs/specs/semconv/gen-ai/
"""
from __future__ import annotations

import logging
from typing import Union

logger = logging.getLogger(__name__)

# Lazy imports — OTel is optional
_tracer = None
_setup_done = False


def _try_import_tracer():
    """Attempt to get an OTel tracer, returning None if not available."""
    global _tracer, _setup_done
    if _setup_done:
        return _tracer
    _setup_done = True
    try:
        from opentelemetry import trace  # noqa: F401
        _tracer = trace.get_tracer("weave")
    except ImportError:
        logger.debug(
            "opentelemetry-api not installed — tracing disabled (#509). "
            "Install with: pip install opentelemetry-api opentelemetry-sdk"
        )
    return _tracer


def setup_telemetry(
    service_name: str = "weave",
    endpoint: str | None = None,
) -> bool:
    """Configure OpenTelemetry tracing provider (#509).

    When endpoint is provided, exports spans via OTLP gRPC.
    Otherwise, uses a simple TracerProvider (no export).

    Returns True if setup succeeded, False if OTel not installed.
    """
    global _tracer, _setup_done
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.resources import Resource
    except ImportError:
        logger.info("opentelemetry not installed, skipping telemetry setup")
        return False

    resource = Resource({"service.name": service_name})
    provider = TracerProvider(resource=resource)

    if endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )
            provider.add_span_processor(
                BatchSpanProcessor(OTLPSpanExporter(endpoint))
            )
            logger.info("OTel tracing enabled with OTLP endpoint: %s", endpoint)
        except ImportError:
            logger.info(
                "opentelemetry-exporter-otlp not installed, "
                "using simple provider without export"
            )

    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer("weave")
    _setup_done = True
    return True


def get_tracer():
    """Get the OTel tracer, or None if not available."""
    return _try_import_tracer()


class NoOpSpan:
    """Fallback span when OTel is not installed."""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def set_attributes(self, *args, **kwargs):
        pass

    def set_attribute(self, *args, **kwargs):
        pass

    def record_exception(self, *args, **kwargs):
        pass


def start_span(name: str, attributes: dict | None = None):
    """Start an OTel span, with graceful fallback to no-op."""
    tracer = get_tracer()
    if tracer is None:
        return NoOpSpan()
    span = tracer.start_as_current_span(name)
    if attributes:
        for key, value in attributes.items():
            span.set_attribute(key, value)
    return span


# -- M5.1: Typed span helpers for 4-layer tracing --


SpanLike = Union["opentelemetry.trace.Span", NoOpSpan]


def start_run_span(run_id: str, requirement: str) -> SpanLike:
    """Create a Run-level OTel span (M5.1)."""
    return start_span("weave.run", {
        "weave.run.id": run_id,
        "weave.run.requirement": requirement[:200],
    })


def start_node_span(run_id: str, node_id: str, agent_type: str) -> SpanLike:
    """Create a Node-level OTel span using GenAI Semantic Conventions (#936)."""
    return start_span("weave.node", {
        "gen_ai.operation.name": "invoke_agent",
        "weave.run.id": run_id,
        "weave.node.id": node_id,
        "weave.node.agent_type": agent_type,
    })


def start_llm_turn_span(
    node_id: str, model: str, provider: str = "unknown",
) -> SpanLike:
    """Create an LLM Turn-level OTel span using GenAI Semantic Conventions (#936)."""
    return start_span(f"chat {model}", {
        "gen_ai.operation.name": "chat",
        "gen_ai.system": provider,
        "gen_ai.request.model": model,
        "weave.node.id": node_id,
    })


def start_tool_call_span(node_id: str, tool_name: str) -> SpanLike:
    """Create a Tool Call-level OTel span using GenAI Semantic Conventions (#936)."""
    return start_span(f"execute_tool {tool_name}", {
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.name": tool_name,
        "gen_ai.tool.type": "function",
        "weave.node.id": node_id,
    })


def set_llm_usage_attributes(
    span: SpanLike,
    *,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    finish_reasons: list[str] | None = None,
) -> None:
    """Set GenAI usage attributes on an LLM span after response (#936)."""
    if isinstance(span, NoOpSpan):
        return
    if input_tokens is not None:
        span.set_attribute("gen_ai.usage.input_tokens", input_tokens)
    if output_tokens is not None:
        span.set_attribute("gen_ai.usage.output_tokens", output_tokens)
    if finish_reasons is not None:
        span.set_attribute("gen_ai.response.finish_reasons", finish_reasons)
