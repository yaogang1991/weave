# PR Review: #944 — feat: add structured logging with trace correlation (#937)

**Reviewed**: 2026-05-26
**Author**: yaogang1991 (姚罡)
**Branch**: feat/937-structured-logging → main
**Decision**: APPROVE with comments

## Summary
Well-structured addition of optional JSON structured logging with OpenTelemetry trace context injection. Clean implementation, backward-compatible (text default), no new dependencies. Four MEDIUM-level improvement suggestions that don't block merging.

## Findings

### CRITICAL
None

### HIGH
None

### MEDIUM

1. **TraceContextFilter hot-path performance when OTel not installed**
   - File: `monitoring/logging_config.py:30-42`
   - `from opentelemetry import trace, context` is inside `filter()`, called for every log record. When OTel is not installed (common case), every log call raises and catches `ImportError` — expensive on hot path.
   - **Fix**: Move import to module level with try/except, cache result:
     ```python
     _otel_trace = _otel_context = None
     try:
         from opentelemetry import trace as _otel_trace, context as _otel_context
     except ImportError:
         pass
     ```

2. **Missing test: exception formatting in JsonFormatter**
   - File: `tests/test_structured_logging.py`
   - `JsonFormatter.format()` handles exceptions (lines 82-87) but no test verifies this.

3. **Missing test: environment variable handling**
   - File: `tests/test_structured_logging.py`
   - `setup_logging()` reads `WEAVE_LOG_FORMAT`/`WEAVE_LOG_LEVEL` from env vars but all tests pass explicit args. No test verifies env var fallback.

4. **Silent fallback on invalid log level**
   - File: `monitoring/logging_config.py:106`
   - `getattr(logging, lvl.upper(), logging.INFO)` silently falls back to INFO for invalid levels. Consider logging a warning.

### LOW

1. **Timestamp construction could use `datetime.isoformat()`** (`monitoring/logging_config.py:66-68`)
   - `time.strftime` + manual milliseconds works but `datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec='milliseconds')` is cleaner.

2. **`root.handlers.clear()` note** (`monitoring/logging_config.py:108`)
   - Fine for CLI entry point, but docstring should note it removes all existing handlers (relevant if called from library context).

## Validation Results

| Check | Result |
|---|---|
| Type check | Skipped (no mypy/pyright config) |
| Lint | Skipped (files on PR branch) |
| Tests | Skipped (files on PR branch; local branch has merge conflict) |
| Build | Skipped (Python project) |

## Files Reviewed
- `monitoring/logging_config.py` (Added - 116 lines)
- `main.py` (Modified - 4 lines added)
- `tests/test_structured_logging.py` (Added - 121 lines)
