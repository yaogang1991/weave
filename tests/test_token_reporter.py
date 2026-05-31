"""Tests for M5.1 TokenReporter — token summary generation from trace events."""

from core.event_models import EventType
from monitoring.token_reporter import TokenReporter


def _make_event(event_type: EventType, payload: dict) -> object:
    return type("Event", (), {"type": event_type, "payload": payload})()


class TestTokenReporter:
    def test_empty_events_returns_zero_summary(self):
        reporter = TokenReporter()
        summary = reporter.summarize_run([])
        assert summary.total_input_tokens == 0
        assert summary.total_output_tokens == 0
        assert summary.node_summaries == []

    def test_run_start_end_events(self):
        events = [
            _make_event(EventType.TRACE_RUN_START, {"run_id": "r1"}),
            _make_event(EventType.TRACE_RUN_END, {
                "duration_ms": 5000,
                "total_input_tokens": 1000,
                "total_output_tokens": 500,
                "total_nodes": 3,
            }),
        ]
        reporter = TokenReporter()
        summary = reporter.summarize_run(events)
        assert summary.run_id == "r1"
        assert summary.total_input_tokens == 1000
        assert summary.total_output_tokens == 500
        assert summary.total_duration_ms == 5000

    def test_node_end_aggregation(self):
        events = [
            _make_event(EventType.TRACE_RUN_START, {"run_id": "r2"}),
            _make_event(EventType.TRACE_NODE_END, {
                "node_id": "plan",
                "agent_type": "planner",
                "input_tokens": 200,
                "output_tokens": 100,
                "duration_ms": 3000,
            }),
            _make_event(EventType.TRACE_RUN_END, {
                "duration_ms": 3000,
                "total_input_tokens": 200,
                "total_output_tokens": 100,
                "total_nodes": 1,
            }),
        ]
        reporter = TokenReporter()
        summary = reporter.summarize_run(events)
        assert len(summary.node_summaries) == 1
        assert summary.node_summaries[0].agent_type == "planner"
        assert summary.node_summaries[0].input_tokens == 200

    def test_llm_turn_accumulation(self):
        events = [
            _make_event(EventType.TRACE_RUN_START, {"run_id": "r3"}),
            _make_event(EventType.TRACE_LLM_TURN, {
                "node_id": "impl",
                "input_tokens": 500,
                "output_tokens": 200,
            }),
            _make_event(EventType.TRACE_LLM_TURN, {
                "node_id": "impl",
                "input_tokens": 300,
                "output_tokens": 100,
            }),
        ]
        reporter = TokenReporter()
        summary = reporter.summarize_run(events)
        assert len(summary.node_summaries) == 1
        assert summary.node_summaries[0].input_tokens == 800

    def test_tool_call_counting(self):
        events = [
            _make_event(EventType.TRACE_TOOL_CALL, {"node_id": "impl", "tool_name": "Read"}),
            _make_event(EventType.TRACE_TOOL_CALL, {"node_id": "impl", "tool_name": "Edit"}),
        ]
        reporter = TokenReporter()
        summary = reporter.summarize_run(events)
        assert len(summary.node_summaries) == 1
        assert summary.node_summaries[0].tool_call_count == 2
