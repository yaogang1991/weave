"""Tests for M5.1 observability: trace events, token reporter, OTel spans."""
import pytest

from core.event_models import EventType, Event
from monitoring.token_reporter import TokenReporter
from monitoring.otel import (
    NoOpSpan,
    start_span,
    start_run_span,
    start_node_span,
    start_llm_turn_span,
    start_tool_call_span,
)


class TestTraceEventTypes:
    def test_all_trace_types_exist(self):
        assert EventType.TRACE_RUN_START == "trace.run_start"
        assert EventType.TRACE_RUN_END == "trace.run_end"
        assert EventType.TRACE_NODE_START == "trace.node_start"
        assert EventType.TRACE_NODE_END == "trace.node_end"
        assert EventType.TRACE_LLM_TURN == "trace.llm_turn"
        assert EventType.TRACE_TOOL_CALL == "trace.tool_call"

    def test_trace_event_creation(self):
        ev = Event(
            type=EventType.TRACE_RUN_START,
            session_id="s1",
            payload={"run_id": "r1"},
        )
        assert ev.type == EventType.TRACE_RUN_START
        assert ev.payload["run_id"] == "r1"


class TestTokenReporter:
    def test_empty_events_returns_zero_summary(self):
        reporter = TokenReporter()
        summary = reporter.summarize_run([])
        assert summary.total_input_tokens == 0
        assert summary.total_output_tokens == 0
        assert summary.node_summaries == []

    def test_single_node_trace(self):
        events = [
            Event(type=EventType.TRACE_RUN_START, session_id="s1",
                  payload={"run_id": "r1"}),
            Event(type=EventType.TRACE_NODE_END, session_id="s1",
                  payload={"node_id": "gen_1", "agent_type": "generator",
                           "input_tokens": 5000, "output_tokens": 2000,
                           "duration_ms": 12000}),
            Event(type=EventType.TRACE_LLM_TURN, session_id="s1",
                  payload={"node_id": "gen_1", "input_tokens": 3000,
                           "output_tokens": 1500}),
            Event(type=EventType.TRACE_TOOL_CALL, session_id="s1",
                  payload={"node_id": "gen_1", "tool_name": "edit"}),
            Event(type=EventType.TRACE_TOOL_CALL, session_id="s1",
                  payload={"node_id": "gen_1", "tool_name": "bash"}),
            Event(type=EventType.TRACE_RUN_END, session_id="s1",
                  payload={"duration_ms": 15000}),
        ]
        reporter = TokenReporter()
        summary = reporter.summarize_run(events)

        assert summary.run_id == "r1"
        assert summary.total_input_tokens == 8000
        assert summary.total_output_tokens == 3500
        assert summary.total_duration_ms == 15000
        assert len(summary.node_summaries) == 1
        node = summary.node_summaries[0]
        assert node.node_id == "gen_1"
        assert node.tool_call_count == 2

    def test_multi_node_trace(self):
        events = [
            Event(type=EventType.TRACE_RUN_START, session_id="s1",
                  payload={"run_id": "r2"}),
            Event(type=EventType.TRACE_NODE_END, session_id="s1",
                  payload={"node_id": "plan", "agent_type": "planner",
                           "input_tokens": 1000, "output_tokens": 500,
                           "duration_ms": 5000}),
            Event(type=EventType.TRACE_NODE_END, session_id="s1",
                  payload={"node_id": "impl", "agent_type": "generator",
                           "input_tokens": 8000, "output_tokens": 4000,
                           "duration_ms": 60000}),
            Event(type=EventType.TRACE_RUN_END, session_id="s1",
                  payload={"duration_ms": 65000}),
        ]
        reporter = TokenReporter()
        summary = reporter.summarize_run(events)

        assert summary.run_id == "r2"
        assert summary.total_input_tokens == 9000
        assert summary.total_output_tokens == 4500
        assert len(summary.node_summaries) == 2


class TestOTelSpanHelpers:
    def test_start_span_returns_noop_without_otel(self):
        span = start_span("test")
        assert isinstance(span, NoOpSpan)

    def test_run_span_returns_span(self):
        span = start_run_span("r1", "build api")
        assert isinstance(span, NoOpSpan)

    def test_node_span_returns_span(self):
        span = start_node_span("r1", "n1", "generator")
        assert isinstance(span, NoOpSpan)

    def test_llm_turn_span_returns_span(self):
        span = start_llm_turn_span("n1", "claude-sonnet-4-6")
        assert isinstance(span, NoOpSpan)

    def test_tool_call_span_returns_span(self):
        span = start_tool_call_span("n1", "edit")
        assert isinstance(span, NoOpSpan)

    def test_noop_span_context_manager(self):
        with NoOpSpan() as span:
            span.set_attribute("key", "value")
            span.set_attributes({"a": 1})
            span.record_exception(ValueError("test"))
