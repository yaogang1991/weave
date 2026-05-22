# Plan: M5.1 Observability — 4-Layer Trace + Token Report

## Summary
为 Weave DAG 执行添加 4 层可观测性追踪（Run → Node → LLM Turn → Tool Call），每层记录 token 计数和耗时。扩展 EventType 枚举、OTel span 系统、改造 ClaudeCodeBackend 为流式消费，并在 Visualizer 添加 Token 面板。

## User Story
As a Weave maintainer, I want to see exactly what happened during a DAG execution (which nodes ran, how many tokens each consumed, which tools were called), so that I can diagnose failures and understand token usage.

## Problem → Solution
**Before**: 执行失败只能看到 `node.failed: timeout`，无法知道哪个 LLM call 或 tool call 消耗了多少 token。
**After**: 完整 4 层 trace 树记录在 JSONL event log 中，Visualizer 展示 token 分布。

## Metadata
- **Complexity**: Large
- **Source PRD**: `.claude/PRPs/prds/m5-production-orchestration.prd.md`
- **PRD Phase**: M5.1 Observability
- **Estimated Files**: 12

---

## UX Design

### Before
```
Run #42 result:
  Node "analyze" → completed
  Node "implement" → failed: timeout
  Token usage: ???
```

### After
```
Run #42 (fix/750-replan-loop)
├── Node "analyze" (12s, input=3200, output=1800)
│   ├── LLM Turn #1 (3s, input=2800, output=900, cache_hit=true)
│   ├── Tool: Read x 5 (2s)
│   └── Tool: Grep "replan" (1s, 12 matches)
├── Node "implement" (FAILED, 180s, input=28000, output=17000)
│   ├── LLM Turn #1 (8s, input=5000, output=2000)
│   ├── Tool: Edit dag_engine.py (2s, OK)
│   ├── LLM Turn #2 (15s, input=8000, output=3500)
│   ├── Tool: Edit orchestrator.py (3s, OK)
│   ├── Tool: Bash "pytest" (60s, FAILED: 3 failures)
│   └── Stall detected: no progress for 60s
└── Total: 192s, input=31200, output=18800
```

### Interaction Changes
| Touchpoint | Before | After | Notes |
|---|---|---|---|
| CLI output | Only node status | Node status + token summary | Per-node token counts |
| Visualizer | No token info | Token panel with distribution | New /api/runs/{id}/tokens |
| JSONL events | No trace events | 6 new TRACE event types | Run/Node/LLM/Tool layers |

---

## Mandatory Reading

| Priority | File | Lines | Why |
|---|---|---|---|
| P0 | `monitoring/otel.py` | all | Current OTel infrastructure to extend |
| P0 | `core/event_models.py` | all | EventType enum to extend |
| P0 | `core/dag_engine.py` | 318-748 | Where to inject Run/Node spans |
| P0 | `core/node_executor.py` | 288-644 | Where to inject LLM Turn spans |
| P1 | `agent/backends/claude_code.py` | 195-274 | SDK execution to refactor for streaming |
| P1 | `core/evaluation_pipeline.py` | 171-196 | Token recording pattern |
| P1 | `session/store.py` | 63-90 | Event emission pattern |
| P2 | `visualizer/server.py` | 42-166 | API endpoint pattern |
| P2 | `tests/test_dag_engine.py` | all | Test pattern to follow |
| P2 | `core/backend_models.py` | all | BackendContext/BackendResult models |

## External Documentation

| Topic | Source | Key Takeaway |
|---|---|---|
| OTel GenAI Semantic Conventions | opentelemetry.io | Use `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` attributes |
| Claude Code SDK query() | SDK source | Returns AsyncIterator[Message] with per-message usage and tool_use/tool_result blocks |

---

## Patterns to Mirror

### NAMING_CONVENTION
// SOURCE: `core/event_models.py:1-60`
```python
class EventType(str, Enum):
    TRACE_RUN_START = "trace.run_start"       # {domain}.{action}
    TRACE_RUN_END = "trace.run_end"
    TRACE_NODE_START = "trace.node_start"
    TRACE_NODE_END = "trace.node_end"
    TRACE_LLM_TURN = "trace.llm_turn"
    TRACE_TOOL_CALL = "trace.tool_call"
```

### ERROR_HANDLING
// SOURCE: `monitoring/otel.py:87-104`
```python
class NoOpSpan:
    """No-op fallback when OTel not installed."""
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def set_attribute(self, key, value): pass
    def add_event(self, name, attributes=None): pass
    def record_exception(self, exception): pass
```
Graceful degradation — never raise when OTel unavailable.

### EVENT_EMISSION
// SOURCE: `session/store.py:63-90`
```python
async def emit_event(self, event_type: EventType, session_id: str,
                     payload: dict | None = None, metadata: dict | None = None):
    event = Event(type=event_type, session_id=session_id,
                  payload=payload or {}, metadata=metadata or {})
    self._append_event(event)
```

### CONFIG_PATTERN
// SOURCE: `core/config.py`
```python
class ObservabilityConfig(BaseModel):
    enabled: bool = True
    otlp_endpoint: str | None = None
    
    @classmethod
    def from_env(cls):
        return cls(
            enabled=os.getenv("WEAVE_OBSERVABILITY_ENABLED", "true").lower() not in ("false", "0"),
            otlp_endpoint=os.getenv("WEAVE_OTLP_ENDPOINT", None),
        )
```
Pattern: `WEAVE_{FEATURE}_{SETTING}` env vars, `from_env()` classmethod.

### BACKEND_RESULT_TOKEN_USAGE
// SOURCE: `agent/backends/claude_code.py:483-496`
```python
@staticmethod
def _extract_token_usage(usage: dict | None) -> dict[str, int]:
    if not usage:
        return {"input_tokens": 0, "output_tokens": 0}
    return {
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
    }
```

### VISUALIZER_API_PATTERN
// SOURCE: `visualizer/server.py:154-166`
```python
@app.get("/api/sessions")
async def api_list_sessions():
    return {"sessions": [...]}
```

### TEST_PATTERN
// SOURCE: `tests/test_dag_engine.py`
```python
@pytest.mark.asyncio
async def test_single_node(self):
    dag = _make_linear_dag()
    engine = DAGExecutionEngine(_noop_executor, _noop_failure_handler)
    result = await engine.execute(dag)
    assert result.nodes["a"].status == NodeStatus.SUCCESS
```

---

## Files to Change

| File | Action | Justification |
|---|---|---|
| `core/event_models.py` | UPDATE | Add 6 TRACE EventType values |
| `monitoring/otel.py` | UPDATE | Extend with 4-layer span creation helpers |
| `monitoring/token_reporter.py` | CREATE | Per-Run token summary generation |
| `core/dag_engine.py` | UPDATE | Add Run/Node span tracing around execution |
| `core/node_executor.py` | UPDATE | Add LLM Turn span tracing |
| `agent/backends/claude_code.py` | UPDATE | Capture tool call data from SDK result |
| `visualizer/server.py` | UPDATE | Add /api/runs/{id}/tokens endpoint |
| `visualizer/static/index.html` | UPDATE | Add token distribution panel |
| `core/config.py` | UPDATE | Add ObservabilityConfig |
| `tests/test_trace_events.py` | CREATE | Tests for trace event emission |
| `tests/test_token_reporter.py` | CREATE | Tests for token reporter |
| `tests/test_otel_spans.py` | CREATE | Tests for OTel span creation |

## NOT Building

- OTLP gRPC export configuration — only span creation + JSONL storage; export stays optional as-is
- Per-LLM-turn cost (USD) tracking — only token counts
- Visualizer redesign — only adding token panel to existing page
- ClaudeCodeBackend full `query()` streaming refactor — only capture token data from `run()` result
- Hierarchical sub-DAG tracing (M5.4 scope)

---

## Step-by-Step Tasks

### Task 1: Extend EventType with TRACE values
- **ACTION**: Add 6 new event types to EventType enum
- **IMPLEMENT**: Add to EventType enum in `core/event_models.py`:
  ```python
  # Trace events (M5.1)
  TRACE_RUN_START = "trace.run_start"
  TRACE_RUN_END = "trace.run_end"
  TRACE_NODE_START = "trace.node_start"
  TRACE_NODE_END = "trace.node_end"
  TRACE_LLM_TURN = "trace.llm_turn"
  TRACE_TOOL_CALL = "trace.tool_call"
  ```
- **MIRROR**: Existing `{domain}.{action}` pattern
- **IMPORTS**: None new
- **GOTCHA**: EventType is `str, Enum` — values must be unique strings
- **VALIDATE**: `python -c "from core.event_models import EventType; print([e for e in EventType if e.value.startswith('trace.')])"` shows 6 entries

### Task 2: Add ObservabilityConfig to config.py
- **ACTION**: Create config class for observability settings
- **IMPLEMENT**:
  ```python
  class ObservabilityConfig(BaseModel):
      enabled: bool = True
      otlp_endpoint: str | None = None
      trace_to_events: bool = True
      
      @classmethod
      def from_env(cls) -> ObservabilityConfig:
          return cls(
              enabled=os.getenv("WEAVE_OBSERVABILITY_ENABLED", "true").lower()
                      not in ("false", "0"),
              otlp_endpoint=os.getenv("WEAVE_OTLP_ENDPOINT") or None,
              trace_to_events=os.getenv("WEAVE_TRACE_TO_EVENTS", "true").lower()
                             not in ("false", "0"),
          )
  ```
  Add field to WeaveConfig: `observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig.from_env)`
- **MIRROR**: BudgetConfig pattern at `core/config.py:547-575`
- **IMPORTS**: `from pydantic import BaseModel, Field`, `import os`
- **GOTCHA**: Don't create circular imports — keep config self-contained
- **VALIDATE**: `python -c "from core.config import WeaveConfig; c = WeaveConfig(); print(c.observability)"` works

### Task 3: Extend monitoring/otel.py with span helpers
- **ACTION**: Add typed span creation functions for each trace layer
- **IMPLEMENT**: Add 4 new functions:
  ```python
  def start_run_span(run_id: str, requirement: str) -> Span:
      return start_span("weave.run", {
          "weave.run.id": run_id,
          "weave.run.requirement": requirement[:200],
      })
  
  def start_node_span(run_id: str, node_id: str, agent_type: str) -> Span:
      return start_span("weave.node", {
          "weave.run.id": run_id,
          "weave.node.id": node_id,
          "weave.node.agent_type": agent_type,
      })
  
  def start_llm_turn_span(node_id: str, model: str) -> Span:
      return start_span("weave.llm_turn", {
          "weave.node.id": node_id,
          "gen_ai.request.model": model,
      })
  
  def start_tool_call_span(node_id: str, tool_name: str) -> Span:
      return start_span("weave.tool_call", {
          "weave.node.id": node_id,
          "weave.tool.name": tool_name,
      })
  ```
- **MIRROR**: Existing `start_span()` at `monitoring/otel.py:106-115`, use GenAI conventions
- **IMPORTS**: Same as existing otel.py
- **GOTCHA**: Each helper must return NoOpSpan when OTel unavailable
- **VALIDATE**: `python -c "from monitoring.otel import start_run_span; s = start_run_span('r1', 'test'); print(type(s))"` works with and without opentelemetry

### Task 4: Add Run/Node tracing to dag_engine.py
- **ACTION**: Wrap DAG execution in Run span, each node in Node span; emit TRACE events
- **IMPLEMENT**: In `DAGExecutionEngine._execute_inner()`:
  - Before execution loop: create Run span, emit `TRACE_RUN_START`
  - Around `execute_node()`: create Node span, emit `TRACE_NODE_START`
  - After node completion: set token attributes on Node span, emit `TRACE_NODE_END` with `{node_id, status, input_tokens, output_tokens, duration_ms}`
  - After all nodes: emit `TRACE_RUN_END` with totals
  - Use try/finally to ensure Run span ends even on failure
- **MIRROR**: Existing `_emit()` at `core/dag_engine.py:230-239`
- **IMPORTS**: `from monitoring.otel import start_run_span, start_node_span`
- **GOTCHA**: Run span must be created BEFORE loop and ended AFTER, even on failure — use try/finally
- **VALIDATE**: Run simple DAG, check JSONL for trace.run_start, trace.node_start, trace.node_end, trace.run_end

### Task 5: Add LLM Turn tracing to node_executor.py
- **ACTION**: After backend execution, emit TRACE_LLM_TURN with token usage from BackendResult
- **IMPLEMENT**: In `_execute_with_timeout()` after backend returns:
  ```python
  token_usage = result.metadata.get("token_usage", {})
  self._emit_trace(EventType.TRACE_LLM_TURN, {
      "node_id": node.id,
      "input_tokens": token_usage.get("input_tokens", 0),
      "output_tokens": token_usage.get("output_tokens", 0),
      "model": result.metadata.get("model", "unknown"),
      "backend": result.metadata.get("backend", "unknown"),
  })
  ```
- **MIRROR**: Token recording at `core/evaluation_pipeline.py:171-196`
- **IMPORTS**: `from core.event_models import EventType`
- **GOTCHA**: BackendResult.metadata may not have token_usage — default to empty dict
- **VALIDATE**: TRACE_LLM_TURN events in JSONL with token counts

### Task 6: Capture Tool Call data from ClaudeCodeBackend
- **ACTION**: Extract tool call info from SDK result and emit TRACE_TOOL_CALL events
- **IMPLEMENT**: In `_parse_sdk_result()` and `_parse_cli_output()`:
  - Extract `tool_uses` list from result data if available
  - For each tool use: emit TRACE_TOOL_CALL event with `{tool_name, node_id, input_preview}`
  - If no per-tool data: emit single aggregate TRACE_TOOL_CALL
  - Pass context (with node_id) through to parse methods
- **MIRROR**: `_extract_token_usage()` at `agent/backends/claude_code.py:483-496`
- **IMPORTS**: None new
- **GOTCHA**: SDK and CLI result structures differ — handle both paths
- **VALIDATE**: TRACE_TOOL_CALL events in JSONL after run

### Task 7: Create monitoring/token_reporter.py
- **ACTION**: Module that generates per-Run token summary from trace events
- **IMPLEMENT**:
  ```python
  class TokenSummary(BaseModel):
      run_id: str
      total_input_tokens: int
      total_output_tokens: int
      node_summaries: dict[str, NodeTokenSummary]
      duration_ms: int
  
  class NodeTokenSummary(BaseModel):
      node_id: str
      agent_type: str
      input_tokens: int
      output_tokens: int
      tool_call_count: int
      duration_ms: int
  
  class TokenReporter:
      def summarize_run(self, events: list[Event]) -> TokenSummary:
          # Filter TRACE events, aggregate totals, per-node breakdown
  ```
- **MIRROR**: `monitoring/metrics.py` MetricsCollector pattern
- **IMPORTS**: `from core.event_models import EventType, Event`, `from pydantic import BaseModel`
- **GOTCHA**: Events may be empty or incomplete if observability was partially disabled
- **VALIDATE**: Unit test with synthetic events

### Task 8: Add Token API to visualizer
- **ACTION**: Add `/api/runs/{session_id}/tokens` endpoint
- **IMPLEMENT**:
  ```python
  @app.get("/api/runs/{session_id}/tokens")
  async def api_run_tokens(session_id: str):
      # Load session events, filter TRACE events, generate summary
      return summary.model_dump()
  ```
- **MIRROR**: Endpoint pattern at `visualizer/server.py:154-166`
- **IMPORTS**: `from monitoring.token_reporter import TokenReporter`, `from core.event_models import EventType`
- **GOTCHA**: Session may not exist — return 404
- **VALIDATE**: `curl http://localhost:8080/api/runs/{id}/tokens` returns JSON

### Task 9: Add Token panel to Visualizer
- **ACTION**: Add token distribution table to existing index.html
- **IMPLEMENT**: Add section below DAG viz:
  - Table: Node ID | Agent Type | Input Tokens | Output Tokens | Tool Calls | Duration
  - Total row at bottom
  - CSS bar chart for last 20 runs (no external chart library)
- **MIRROR**: Existing HTML structure in `visualizer/static/index.html`
- **IMPORTS**: None (vanilla JS)
- **GOTCHA**: No chart library — use CSS bars like existing dashboard
- **VALIDATE**: Start `weave viz`, run task, see token table

### Task 10: Write tests
- **ACTION**: Create 3 test files
- **IMPLEMENT**:
  - `tests/test_trace_events.py` — TRACE events correctly emitted during DAG execution
  - `tests/test_token_reporter.py` — TokenReporter with synthetic events
  - `tests/test_otel_spans.py` — Span helpers return correct objects + NoOpSpan fallback
- **MIRROR**: `tests/test_dag_engine.py` — `_make_linear_dag()`, `MagicMock`, `@pytest.mark.asyncio`
- **IMPORTS**: `pytest`, `unittest.mock`, `from core.event_models import EventType`
- **GOTCHA**: OTel may not be installed in test env — handle NoOpSpan
- **VALIDATE**: `python -m pytest tests/test_trace_events.py tests/test_token_reporter.py tests/test_otel_spans.py -v`

---

## Testing Strategy

### Unit Tests

| Test | Input | Expected Output | Edge Case? |
|---|---|---|---|
| TRACE_RUN_START emitted | Execute simple DAG | trace.run_start in JSONL | No |
| TRACE_NODE_END has tokens | Node with mock backend | input_tokens + output_tokens > 0 | No |
| TokenReporter summarizes | List of TRACE events | Correct TokenSummary totals | No |
| NoOpSpan fallback | OTel not installed | All span ops no-op | Yes |
| Zero tokens | Backend returns no usage | Events with 0 values | Yes |
| Concurrent nodes | 2 parallel nodes | 2 separate TRACE_NODE pairs | Yes |

### Edge Cases Checklist
- [x] OTel not installed — NoOpSpan fallback
- [x] Backend returns no token_usage — default to zeros
- [x] Empty event list for TokenReporter — return zero summary
- [x] Session doesn't exist for token API — return 404

---

## Validation Commands

### Static Analysis
```bash
python -m flake8 --max-line-length=100 monitoring/otel.py monitoring/token_reporter.py core/event_models.py core/config.py
```
EXPECT: Zero errors

### Unit Tests
```bash
python -m pytest tests/test_trace_events.py tests/test_token_reporter.py tests/test_otel_spans.py -v
```
EXPECT: All tests pass

### Full Test Suite
```bash
python -m pytest -v --tb=short
```
EXPECT: No regressions

### Manual Validation
- [ ] Run `weave run "Fix a typo in README"` — check JSONL for trace events
- [ ] Run `weave viz` — open browser, see token panel after run
- [ ] Uninstall opentelemetry-api — verify run still works

---

## Acceptance Criteria
- [ ] 6 new TRACE EventType values in core/event_models.py
- [ ] Run/Node/LLM Turn/Tool Call spans emitted during execution
- [ ] Token counts recorded in JSONL at each layer
- [ ] /api/runs/{id}/tokens returns token summary
- [ ] Visualizer shows token distribution table
- [ ] All tests pass, no regressions
- [ ] Works without opentelemetry installed

## Risks
| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| SDK result lacks per-tool-call data | M | Low | Emit aggregate TRACE_TOOL_CALL |
| OTel import issues on some platforms | L | None | NoOpSpan fallback |
| Performance overhead from events | L | Low | Benchmark with 25-node DAG |

## Notes
- Full `query()` streaming refactor of ClaudeCodeBackend is deferred. M5.1 captures what `run()` provides.
- Task 6 may need runtime adjustment based on actual SDK result structure.
