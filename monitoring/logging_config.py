"""Structured logging with trace correlation (#937).

Provides optional JSON structured logging with automatic injection of
trace_id and span_id from the active OpenTelemetry context.

Enabled via ``WEAVE_LOG_FORMAT=json`` environment variable (default: text).
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

# Cache OTel imports at module level to avoid repeated ImportError
# on every log record when OTel is not installed.
_otel_trace = None
_otel_context = None
try:
    from opentelemetry import (  # type: ignore[no-redef]
        trace as _otel_trace,
        context as _otel_context,
    )
except ImportError:
    pass


class TraceContextFilter(logging.Filter):
    """Inject trace_id and span_id into every log record (#937).

    When an OTel span is active, its trace_id and span_id are added
    as attributes on the LogRecord. When no trace is active (or OTel
    is not installed), the fields are set to empty strings.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = ""  # type: ignore[attr-defined]
        record.span_id = ""  # type: ignore[attr-defined]
        if _otel_trace is not None:
            ctx = _otel_context.get_current()
            span = _otel_trace.get_current_span(ctx)
            if span != _otel_trace.INVALID_SPAN:
                sc = span.get_span_context()
                if sc.is_valid:
                    record.trace_id = format(sc.trace_id, "032x")  # type: ignore[attr-defined]
                    record.span_id = format(sc.span_id, "016x")  # type: ignore[attr-defined]
        return True


class JsonFormatter(logging.Formatter):
    """Format log records as JSON lines (#937).

    Output fields: timestamp, level, logger, message, trace_id, span_id,
    plus any extra fields passed via ``logger.info(..., extra={})``.
    """

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict[str, Any] = {
            "timestamp": time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.gmtime(record.created),
            ) + f".{int(record.created % 1 * 1000):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        if getattr(record, "trace_id", ""):
            log_entry["trace_id"] = record.trace_id
        if getattr(record, "span_id", ""):
            log_entry["span_id"] = record.span_id

        # Include any extra fields from the log call
        reserved = {
            "name", "msg", "args", "created", "relativeCreated",
            "exc_info", "exc_text", "stack_info", "lineno", "funcName",
            "pathname", "filename", "module", "thread", "threadName",
            "process", "processName", "levelname", "levelno", "message",
            "msecs", "taskName", "trace_id", "span_id",
        }
        for key, value in record.__dict__.items():
            if key not in reserved and not key.startswith("_"):
                log_entry[key] = value

        if record.exc_info and record.exc_text is None:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            log_entry["exception"] = record.exc_text

        return json.dumps(log_entry, default=str, ensure_ascii=False)


_TEXT_FORMAT = "%(asctime)s %(levelname)-8s [%(name)s] %(message)s"
_JSON_LOG_FORMAT = "json"


def setup_logging(
    level: str | None = None,
    log_format: str | None = None,
) -> None:
    """Configure the root logger with optional JSON formatting (#937).

    Removes all existing handlers on the root logger before configuring.

    Args:
        level: Log level (default: from WEAVE_LOG_LEVEL env var or INFO).
        log_format: "json" or "text" (default: from WEAVE_LOG_FORMAT env var or text).
    """
    fmt = log_format or os.getenv("WEAVE_LOG_FORMAT", "text")
    lvl = level or os.getenv("WEAVE_LOG_LEVEL", "INFO")

    root = logging.getLogger()
    resolved_level = getattr(logging, lvl.upper(), None)
    if resolved_level is None:
        logging.warning("Invalid log level %r, falling back to INFO", lvl)
        resolved_level = logging.INFO
    root.setLevel(resolved_level)

    # Remove existing handlers to avoid duplicates
    root.handlers.clear()

    handler = logging.StreamHandler()

    if fmt.lower() == _JSON_LOG_FORMAT:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(_TEXT_FORMAT))

    # Always attach the trace context filter
    handler.addFilter(TraceContextFilter())
    root.addHandler(handler)
