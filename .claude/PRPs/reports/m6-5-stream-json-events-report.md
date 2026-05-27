# Implementation Report: M6.5 Stream-JSON Events for ClaudeCodeBackend

## Summary
将 ClaudeCodeBackend CLI 路径从一次性 `--output-format json` 改为 `--output-format stream-json`，
新增 StreamMessage 模型和 StreamParser 解析器，通过 event_callback 桥接到 SessionStore，
通过 progress_callback 推送到 ProgressTracker。

## Assessment vs Reality

| Metric | Predicted (Plan) | Actual |
|---|---|---|
| Complexity | Medium | Medium |
| Confidence | High | High |
| Files Changed | 6 files (2 new, 4 modify) | 7 files (2 new, 5 modify) |

## Tasks Completed

| # | Task | Status | Notes |
|---|---|---|---|
| 1 | StreamMessage + StreamParser | Complete | `agent/backends/stream_parser.py` |
| 2 | BackendContext event_callback | Complete | `core/backend_models.py` |
| 3 | BackendResult messages | Complete | `core/backend_models.py` |
| 4 | ClaudeCodeBackend stream exec | Complete | Rewrote `_execute_via_cli`, added methods |
| 5 | NodeExecutor event_callback | Complete | `session_store` param chain |
| 6 | SDK path messages | Complete | `_parse_sdk_result` includes messages |
| 7 | Tests | Complete | 25 tests |

## Files Changed

| File | Action | Description |
|---|---|---|
| `agent/backends/stream_parser.py` | CREATED | StreamMessage + StreamParser |
| `agent/backends/claude_code.py` | UPDATED | Stream CLI path |
| `core/backend_models.py` | UPDATED | event_callback + messages |
| `core/node_executor.py` | UPDATED | session_store + event_callback |
| `core/dag_engine.py` | UPDATED | session_store passthrough |
| `control_plane/execution_factory.py` | UPDATED | session_store passthrough |
| `tests/test_m6_5_stream_json.py` | CREATED | 25 tests |

## Deviations from Plan
- NodeExecutor needed `session_store` constructor param (plan flagged as GOTCHA, confirmed)
- Removed `json` import from `claude_code.py` (moved to StreamParser)
- Removed `_parse_cli_output()` (replaced by `_build_stream_result()`)

## Tests Written

| Test File | Tests | Coverage |
|---|---|---|
| `tests/test_m6_5_stream_json.py` | 25 | Full M6.5 coverage |
