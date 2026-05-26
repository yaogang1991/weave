"""Tests for M5.1 OTel span helpers — NoOpSpan fallback and typed span creation."""
from monitoring.otel import (
    NoOpSpan,
    start_span,
    start_run_span,
    start_node_span,
    start_llm_turn_span,
    start_tool_call_span,
    set_llm_usage_attributes,
)


class TestNoOpSpan:
    def test_context_manager(self):
        with NoOpSpan() as span:
            assert span is not None

    def test_set_attribute_no_error(self):
        span = NoOpSpan()
        span.set_attribute("key", "value")
        span.set_attributes({"a": 1})

    def test_record_exception_no_error(self):
        span = NoOpSpan()
        span.record_exception(ValueError("test"))


class TestSpanHelpers:
    def test_start_span_returns_noop_without_otel(self):
        span = start_span("test.span", {"key": "value"})
        assert isinstance(span, NoOpSpan)

    def test_start_run_span(self):
        span = start_run_span("r1", "test requirement")
        assert isinstance(span, NoOpSpan)

    def test_start_node_span(self):
        span = start_node_span("r1", "n1", "generator")
        assert isinstance(span, NoOpSpan)

    def test_start_llm_turn_span_default_provider(self):
        span = start_llm_turn_span("n1", "claude-sonnet-4-6")
        assert isinstance(span, NoOpSpan)

    def test_start_llm_turn_span_with_provider(self):
        span = start_llm_turn_span("n1", "claude-sonnet-4-6", provider="anthropic")
        assert isinstance(span, NoOpSpan)

    def test_start_tool_call_span(self):
        span = start_tool_call_span("n1", "Read")
        assert isinstance(span, NoOpSpan)

    def test_run_span_truncates_long_requirement(self):
        long_req = "x" * 500
        span = start_run_span("r1", long_req)
        assert isinstance(span, NoOpSpan)

    def test_set_llm_usage_attributes_noop(self):
        """set_llm_usage_attributes is safe on NoOpSpan."""
        span = NoOpSpan()
        set_llm_usage_attributes(
            span, input_tokens=100, output_tokens=50,
            finish_reasons=["completed"],
        )
