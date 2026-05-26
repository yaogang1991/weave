"""Tests for #938: OTel GenAI metrics instrumentation."""
from monitoring.otel_metrics import (
    record_token_usage,
    record_llm_duration,
    record_tool_call,
    LLMDurationTracker,
    ToolDurationTracker,
)


class TestMetricsFunctions:
    """Record functions are safe no-ops when OTel SDK not installed."""

    def test_record_token_usage_no_error(self):
        record_token_usage("claude-sonnet-4-6", input_tokens=100, output_tokens=50)

    def test_record_llm_duration_no_error(self):
        record_llm_duration(1.5, "claude-sonnet-4-6", "anthropic")

    def test_record_tool_call_no_error(self):
        record_tool_call("write_file", 0.3, success=True)
        record_tool_call("bash", 1.2, success=False)

    def test_record_token_usage_zero_tokens(self):
        record_token_usage("gpt-4", input_tokens=0, output_tokens=0)


class TestLLMDurationTracker:
    def test_tracks_duration(self):
        import time
        with LLMDurationTracker(model="test-model", provider="test") as t:
            time.sleep(0.01)
        assert t.duration > 0

    def test_default_duration_zero(self):
        t = LLMDurationTracker(model="test")
        assert t.duration == 0.0


class TestToolDurationTracker:
    def test_tracks_duration(self):
        import time
        with ToolDurationTracker("read_file") as t:
            time.sleep(0.01)
        assert t.duration > 0
        assert t.success is True

    def test_marks_failure_on_exception(self):
        t = ToolDurationTracker("bad_tool")
        try:
            with t:
                raise ValueError("boom")
        except ValueError:
            pass
        assert t.success is False
        assert t.duration > 0
