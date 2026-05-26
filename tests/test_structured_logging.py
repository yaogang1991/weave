"""Tests for #937: structured logging with trace correlation."""
import json
import logging

from monitoring.logging_config import (
    JsonFormatter,
    TraceContextFilter,
    setup_logging,
)


class TestTraceContextFilter:
    def test_injects_empty_fields_without_trace(self):
        """Filter adds empty trace_id/span_id when no trace is active."""
        filt = TraceContextFilter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="hello", args=(), exc_info=None,
        )
        result = filt.filter(record)
        assert result is True
        assert record.trace_id == ""
        assert record.span_id == ""

    def test_filter_always_passes(self):
        """Filter never blocks log records."""
        filt = TraceContextFilter()
        record = logging.LogRecord(
            name="test", level=logging.WARNING, pathname="", lineno=0,
            msg="msg", args=(), exc_info=None,
        )
        assert filt.filter(record) is True


class TestJsonFormatter:
    def test_basic_json_output(self):
        """Formatter produces valid JSON with required fields."""
        fmt = JsonFormatter()
        record = logging.LogRecord(
            name="test.logger", level=logging.INFO, pathname="", lineno=0,
            msg="hello world", args=(), exc_info=None,
        )
        output = fmt.format(record)
        parsed = json.loads(output)
        assert parsed["level"] == "INFO"
        assert parsed["logger"] == "test.logger"
        assert parsed["message"] == "hello world"
        assert "timestamp" in parsed

    def test_omits_empty_trace_fields(self):
        """Empty trace_id/span_id are not included in output."""
        fmt = JsonFormatter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="msg", args=(), exc_info=None,
        )
        record.trace_id = ""
        record.span_id = ""
        output = fmt.format(record)
        parsed = json.loads(output)
        assert "trace_id" not in parsed
        assert "span_id" not in parsed

    def test_includes_trace_fields_when_present(self):
        """Non-empty trace_id/span_id are included in output."""
        fmt = JsonFormatter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="msg", args=(), exc_info=None,
        )
        record.trace_id = "abc123"
        record.span_id = "def456"
        output = fmt.format(record)
        parsed = json.loads(output)
        assert parsed["trace_id"] == "abc123"
        assert parsed["span_id"] == "def456"

    def test_includes_extra_fields(self):
        """Extra fields from the log call are included in output."""
        fmt = JsonFormatter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="msg", args=(), exc_info=None,
        )
        record.custom_key = "custom_value"
        record.event = "tool_selected"
        output = fmt.format(record)
        parsed = json.loads(output)
        assert parsed["custom_key"] == "custom_value"
        assert parsed["event"] == "tool_selected"


class TestSetupLogging:
    def test_text_format_is_default(self):
        """Default format is text (backward compatible)."""
        root = logging.getLogger()
        setup_logging(log_format="text", level="INFO")
        assert root.level == logging.INFO
        assert len(root.handlers) == 1
        assert not isinstance(root.handlers[0].formatter, JsonFormatter)

    def test_json_format_configures_json_formatter(self):
        """json format activates JsonFormatter."""
        root = logging.getLogger()
        setup_logging(log_format="json", level="DEBUG")
        assert root.level == logging.DEBUG
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, JsonFormatter)

    def test_trace_filter_always_attached(self):
        """TraceContextFilter is attached regardless of format."""
        root = logging.getLogger()
        setup_logging(log_format="text", level="INFO")
        assert any(
            isinstance(f, TraceContextFilter)
            for f in root.handlers[0].filters
        )

    def teardown_method(self):
        """Reset root logger after each test."""
        logging.getLogger().handlers.clear()
