# Module SPEC: visualizer/ (server.py + cli_renderer.py + event_bridge.py)

## Purpose

Real-time visualization layer for the Weave. Provides three complementary interfaces:
- **FastAPI HTTP/WebSocket server** (`server.py`): Serves a web dashboard and REST API for
  sessions, plans, jobs, tickets, metrics, and alerts.
- **CLI DAG renderer** (`cli_renderer.py`): Terminal-based execution visualization using ANSI
  color codes for live DAG status updates.
- **WebSocket event bridge** (`event_bridge.py`): Broadcasts `ExecutionEvent` messages from the
  DAG execution engine to all connected WebSocket clients.

## Public Interfaces

### Module `visualizer/server.py`

#### FastAPI Application

```python
app: FastAPI  # title="Weave Visualizer", version="2.0"
bridge: WebSocketEventBridge  # global singleton instance
```

#### Request Models

```python
class RejectRequest(PydanticModel):
    reason: str = ""
```

#### HTTP Endpoints

| Method | Path | Handler | Returns | Description |
|--------|------|---------|---------|-------------|
| GET | `/` | `dashboard()` | `HTMLResponse` | Serves `static/index.html` or fallback HTML |
| GET | `/console` | `console_page()` | `HTMLResponse` | Serves `static/console.html` or fallback HTML |
| GET | `/api/health` | `health()` | `JSONResponse` | `{"status": "ok", "timestamp": ...}` |
| GET | `/api/sessions` | `api_list_sessions()` | `JSONResponse` | Lists all sessions with metadata |
| GET | `/api/sessions/{session_id}` | `api_get_session(session_id: str)` | `JSONResponse` | Full session data (events + DAG) |
| GET | `/api/plans` | `api_list_plans()` | `JSONResponse` | Lists saved execution plans |
| GET | `/api/jobs` | `api_list_jobs(status: str \| None = None)` | `JSONResponse` | Lists jobs with optional status filter |
| GET | `/api/jobs/{job_id}` | `api_get_job(job_id: str)` | `JSONResponse` | Job details with associated runs |
| POST | `/api/jobs/{job_id}/cancel` | `api_cancel_job(job_id: str)` | `JSONResponse` | Cancels a job |
| POST | `/api/jobs/{job_id}/retry` | `api_retry_job(job_id: str)` | `JSONResponse` | Retries a failed/dead_letter job |
| POST | `/api/recover` | `api_recover()` | `JSONResponse` | Recovers orphaned jobs |
| GET | `/api/tickets` | `api_list_tickets(status: str \| None = None, job_id: str \| None = None)` | `JSONResponse` | Lists approval tickets with stats |
| POST | `/api/tickets/{ticket_id}/approve` | `api_approve_ticket(ticket_id: str, reason: str = "")` | `JSONResponse` | Approves a ticket |
| POST | `/api/tickets/{ticket_id}/reject` | `api_reject_ticket(ticket_id: str, body: RejectRequest)` | `JSONResponse` | Rejects a ticket |
| GET | `/api/metrics` | `api_metrics()` | `JSONResponse` | System metrics via `MetricsCollector` |
| GET | `/api/alerts` | `api_alerts()` | `JSONResponse` | Active alerts via `AlertManager.check_all()` |

#### WebSocket Endpoint

| Path | Handler | Description |
|------|---------|-------------|
| `WS /ws` | `websocket_endpoint(websocket: WebSocket)` | Real-time event streaming; accepts JSON commands |

**WebSocket client commands:**

| Command | Fields | Response |
|---------|--------|----------|
| `list_sessions` | `{command: "list_sessions"}` | `{type: "sessions_list", sessions: [...]}` |
| `get_session` | `{command: "get_session", session_id: "..."}` | `{type: "session_data", ...}` |
| `list_plans` | `{command: "list_plans"}` | `{type: "plans_list", plans: [...]}` |
| `ping` | `{command: "ping"}` | `{type: "pong"}` |

#### Helper Functions

| Function | Signature | Description |
|----------|-----------|-------------|
| `get_event_bridge` | `() -> WebSocketEventBridge` | Returns the global bridge singleton |
| `run_server` | `async (host: str = "0.0.0.0", port: int = 8080) -> None` | Starts uvicorn server programmatically |
| `_list_sessions` | `() -> list[dict]` | Reads all sessions from `SessionStore` |
| `_get_session_data` | `(session_id: str) -> dict` | Reads events and reconstructs DAG for a session |
| `_reconstruct_dag_from_events` | `(events: list[dict]) -> dict \| None` | Looks for DAG structure (nodes+edges) in event payloads |
| `_list_plans` | `() -> list[dict]` | Reads `plan_*.json` files from `./data/plans/` |
| `_handle_client_command` | `async (websocket: WebSocket, data: dict) -> None` | Dispatches WebSocket client commands |

---

### Class `CLIDAGRenderer` (cli_renderer.py)

```python
class CLIDAGRenderer:
    def __init__(self) -> None
```

**Class Constants:**

| Constant | Type | Description |
|----------|------|-------------|
| `STATUS_COLORS` | `dict[NodeStatus, str]` | ANSI escape codes: PENDING=gray, RUNNING=blue, SUCCESS=green, FAILED=red, SKIPPED=yellow, RETRYING=magenta |
| `RESET` | `str` | `"\033[0m"` |
| `BOLD` | `str` | `"\033[1m"` |

**Instance State:**

| Attribute | Type | Description |
|-----------|------|-------------|
| `_node_status` | `dict[str, str]` | Tracks node_id -> status string |
| `_node_start_times` | `dict[str, float]` | Tracks node_id -> start timestamp (for duration calc) |
| `_event_count` | `int` | Running count of processed events |

**Methods:**

| Method | Signature | Returns | Description |
|--------|-----------|---------|-------------|
| `handle_event` | `(event: ExecutionEvent) -> None` | `None` | Event handler compatible with `DAGExecutionEngine.on_event()`; updates status and prints event line |
| `render_dag` | `(dag: DAG) -> None` | `None` | Prints the DAG topology by topological level before execution |
| `render_live_status` | `(dag: DAG) -> None` | `None` | Prints compact status of all nodes with durations |
| `render_summary` | `(dag: DAG) -> None` | `None` | Prints execution summary (total, success, failed, skipped counts) |

**Private Methods:**

| Method | Signature | Description |
|--------|-----------|-------------|
| `_print_event` | `(icon: str, node_id: str, event_type: str, details: dict, duration: float \| None = None) -> None` | Prints a single formatted event line; truncates detail values > 40 chars |
| `_get_duration` | `(node_id: str) -> float \| None` | Returns elapsed seconds since node start |
| `_agent_color` | `(agent_type: str) -> str` | Returns ANSI color: planner=cyan, generator=green, evaluator=yellow, default=white |

**Event type mapping** (in `handle_event`):

| `event.event_type` | Icon | Action |
|--------------------|------|--------|
| `"started"` | triangle | Sets status to "running", records start time |
| `"completed"` | checkmark | Sets status to "success", calculates duration |
| `"failed"` | cross | Sets status to "failed" |
| `"retrying"` | arrows | Sets status to "retrying" |
| `"skipped"` | skip | Sets status to "skipped" |

---

### Class `WebSocketEventBridge` (event_bridge.py)

```python
class WebSocketEventBridge:
    def __init__(self) -> None
```

**Instance State:**

| Attribute | Type | Description |
|-----------|------|-------------|
| `_clients` | `list[Any]` | Connected WebSocket objects |
| `_lock` | `asyncio.Lock` | Protects `_clients` mutations |
| `_history` | `list[dict]` | Buffered recent events for late-joining clients |
| `_max_history` | `int` | Maximum history buffer size (default: 1000) |

**Methods:**

| Method | Signature | Returns | Description |
|--------|-----------|---------|-------------|
| `connect` | `async (websocket) -> None` | `None` | Registers a WebSocket client; sends buffered history on connect |
| `disconnect` | `async (websocket) -> None` | `None` | Removes a WebSocket client |
| `handle_event` | `async (event: ExecutionEvent) -> None` | `None` | Compatible with `DAGExecutionEngine.on_event()`; buffers and broadcasts event |
| `broadcast_dag` | `async (dag_data: dict) -> None` | `None` | Broadcasts a DAG structure update |
| `broadcast_session_start` | `async (session_id: str, dag_data: dict) -> None` | `None` | Broadcasts session start with initial DAG |
| `broadcast_session_end` | `async (session_id: str, summary: dict) -> None` | `None` | Broadcasts session completion |
| `get_history` | `() -> list[dict]` | `list[dict]` | Returns a copy of the event history buffer |
| `clear_history` | `() -> None` | `None` | Clears the event history buffer |

**Private Methods:**

| Method | Signature | Description |
|--------|-----------|-------------|
| `_broadcast` | `async (payload: dict) -> None` | Sends payload to all clients; removes dead clients on send failure |

**Broadcast payload types:**

| Method | `type` field | Additional fields |
|--------|-------------|-------------------|
| `handle_event` | `"execution_event"` | `timestamp`, `node_id`, `event_type`, `details` |
| `broadcast_dag` | `"dag_update"` | `dag` |
| `broadcast_session_start` | `"session_start"` | `session_id`, `dag` |
| `broadcast_session_end` | `"session_end"` | `session_id`, `summary` |

## Data Flow

```
DAGExecutionEngine
    |
    +-- on_event(CLIDAGRenderer.handle_event)  --> terminal output
    |
    +-- on_event(WebSocketEventBridge.handle_event) --> broadcast to WebSocket clients
                                                             |
                                                        _history buffer (FIFO, max 1000)
                                                             |
                                                        _broadcast() --> all _clients
                                                                          dead clients removed

FastAPI Server:
    HTTP GET /api/*  <-->  SessionStore / JobRepository / ApprovalRepository / MetricsCollector / AlertManager
    WS /ws           <-->  WebSocketEventBridge
                              |
                         client commands (list_sessions, get_session, list_plans, ping)
                              |
                         server responses (sessions_list, session_data, plans_list, pong)

CLI Renderer:
    DAGExecutionEngine.on_event --> handle_event() --> _print_event() --> stdout
    DAG object -----------------> render_dag() / render_live_status() / render_summary() --> stdout
```

## Error Codes

HTTP status codes returned by the server:

| Code | Condition | Endpoint |
|------|-----------|----------|
| 400 | Invalid job/ticket status value | `api_list_jobs`, `api_list_tickets` |
| 400 | Cannot retry job in non-failed/dead_letter status | `api_retry_job` |
| 400 | Invalid job status transition | `api_cancel_job` |
| 400 | Invalid ticket operation (already decided) | `api_approve_ticket`, `api_reject_ticket` |
| 404 | Job not found | `api_get_job`, `api_retry_job` |

WebSocket/bridge error handling:

| Condition | Behavior |
|-----------|----------|
| WebSocket client disconnect | Client removed from `_clients` in `disconnect()` |
| Send to dead client fails | Client added to `dead_clients` list and removed in `_broadcast()` |
| Plan file parse error | File skipped in `_list_plans()` (try/except continue) |

CLI renderer errors: None -- all rendering is best-effort output to stdout.

## Dependencies

| Dependency | Type | Usage |
|------------|------|-------|
| `fastapi` | Third-party | HTTP/WebSocket framework |
| `uvicorn` | Third-party | ASGI server |
| `pydantic` | Third-party | `RejectRequest` model |
| `visualizer.event_bridge` | Internal | `WebSocketEventBridge` |
| `session.store` | Internal | `SessionStore` for session data |
| `core.config` | Internal | `WeaveConfig` for event store path |
| `core.models` | Internal | `DAG`, `ExecutionEvent`, `NodeStatus` |
| `control_plane.models` | Internal | `JobStatus`, `RunStatus` |
| `control_plane.repository` | Internal | `JobRepository` |
| `control_plane.approval` | Internal | `ApprovalRepository`, `TicketStatus` |
| `monitoring.metrics` | Internal | `MetricsCollector` |
| `monitoring.alerts` | Internal | `AlertManager`, `create_default_alerts` |
| `asyncio` | Stdlib | Lock, gather for async operations |
| `json` | Stdlib | Plan file parsing |
| `time` | Stdlib | Duration calculation in CLI renderer |

## Configuration

| Parameter | Default | Scope | Description |
|-----------|---------|-------|-------------|
| Server host | `"0.0.0.0"` | `run_server()` | Uvicorn bind address |
| Server port | `8080` | `run_server()` | Uvicorn bind port |
| Log level | `"info"` | `run_server()` | Uvicorn log level |
| History buffer size | `1000` | `WebSocketEventBridge._max_history` | Max buffered events for late-joining clients |
| Plans directory | `"./data/plans"` | `_list_plans()` | Where plan JSON files are read from |
| Static files directory | `{server.py parent}/static/` | `app.mount("/static")` | Static asset serving |

## Extension Points

1. **New REST endpoints**: Add route functions to `server.py` with `@app.get`/`@app.post`
   decorators. Follow the existing pattern of instantiating repository objects per-request.
2. **New WebSocket commands**: Add branches in `_handle_client_command()` for new command types.
3. **Custom event rendering**: Subclass `CLIDAGRenderer` and override `_print_event()` or
   `render_summary()` for custom formatting.
4. **Alternative transports**: `WebSocketEventBridge` can be replaced or supplemented with an
   SSE (Server-Sent Events) or MQTT bridge by implementing the same `handle_event` interface.
5. **History persistence**: The in-memory `_history` buffer in `WebSocketEventBridge` is
   ephemeral. A persistent store could be added for replay across server restarts.
6. **Static dashboard**: The `static/index.html` and `static/console.html` files are served
   directly. Replacing them provides a fully custom UI without server changes.

## Invariants

1. **Global bridge singleton**: `server.py` creates exactly one `WebSocketEventBridge` instance
   at module level, shared across all WebSocket connections and accessible via `get_event_bridge()`.
2. **Thread-safe client management**: All mutations to `_clients` in `WebSocketEventBridge` are
   protected by `asyncio.Lock`.
3. **Dead client cleanup**: `_broadcast()` removes any client that fails to receive a message.
   This prevents memory leaks from disconnected clients.
4. **History FIFO**: The history buffer is a list that evicts the oldest entry (`pop(0)`) when
   `_max_history` is exceeded.
5. **Per-request repository instances**: HTTP handlers create new `JobRepository` and
   `ApprovalRepository` instances per request rather than sharing state.
6. **CLI renderer is stateless across DAGs**: `_node_status`, `_node_start_times`, and
   `_event_count` are instance-level but are not reset between DAG executions.
7. **Plan file naming convention**: `_list_plans()` only reads files matching `plan_*.json`.
8. **Event compatibility**: Both `CLIDAGRenderer.handle_event()` and `WebSocketEventBridge.handle_event()`
   accept `ExecutionEvent` objects, making them interchangeable as `DAGExecutionEngine.on_event` targets.
