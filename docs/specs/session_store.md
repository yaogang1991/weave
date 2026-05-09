# Module SPEC: session/store.py

## Purpose

Append-only JSONL event store that serves as the single source of truth for all session state.
Implements the principle that *sessions are durable and context windows are ephemeral* -- every
state change is recorded as an immutable event, and full state can be reconstructed by replaying
the event log.

## Public Interfaces

### Class `SessionStore`

```python
class SessionStore:
    def __init__(self, base_path: str = "./data/events") -> None
```

**Methods:**

| Method | Signature | Returns | Description |
|--------|-----------|---------|-------------|
| `create_session` | `(session_id: str, workflow_name: str) -> SessionState` | `SessionState` | Creates a new session, emits `SESSION_START` event, returns initial state |
| `emit_event` | `(session_id: str, event_type: EventType, payload: dict, metadata: dict \| None = None) -> Event` | `Event` | Appends an event to the session's JSONL file |
| `get_events` | `(session_id: str, start: int \| None = None, end: int \| None = None, event_type: EventType \| None = None) -> list[Event]` | `list[Event]` | Reads events with optional positional slicing and type filtering |
| `restore_state` | `(session_id: str) -> SessionState` | `SessionState` | Replays all events to reconstruct full session state |
| `list_sessions` | `() -> list[str]` | `list[str]` | Lists all session IDs (derived from `.jsonl` filenames) |
| `checkpoint` | `(session_id: str, label: str) -> None` | `None` | Creates a named checkpoint by copying the current event log to `checkpoints/` |

**Private Methods:**

| Method | Signature | Description |
|--------|-----------|-------------|
| `_session_file` | `(session_id: str) -> Path` | Resolves the JSONL file path: `{base_path}/{session_id}.jsonl` |
| `_apply_event` | `(state: SessionState, event: Event) -> None` | Mutates state in-place by applying a single event |

## Data Flow

```
create_session()
    |
    v
emit_event() -----> {base_path}/{session_id}.jsonl  (append, one JSON per line)
    |
    v
get_events() -----> positional slice [start:end] + optional EventType filter
    |
    v
restore_state() --> get_events() --> _apply_event() loop --> SessionState
    |
    v
checkpoint() ------> {base_path}/checkpoints/{session_id}/{label}.jsonl (file copy)
```

**Event application logic** (`_apply_event`):

| Event Type | State Mutation |
|------------|---------------|
| `SESSION_START` | `status = "running"` |
| `SESSION_IDLE` | `status = "idle"` |
| `SESSION_END` | `status = "completed"` |
| `SESSION_ERROR` | `status = "error"`, appends error to `metrics.errors` |
| `WORKFLOW_STAGE_START` | `current_stage = payload.stage_name`, `status = "running"` |
| `WORKFLOW_STAGE_END` | appends to `stages_completed`, clears `current_stage`, `status = "idle"` |
| `AGENT_MESSAGE` | appends `AgentMessage` to `context_window`, trims to last 50 entries |
| `AGENT_TOOL_USE` | increments `metrics.total_tool_calls` |
| `TOOL_EXEC_END` | accumulates `metrics.total_duration_ms` from `payload.duration_ms` |
| `EVAL_RESULT` | if `passed == False`, appends error to `metrics.errors` |

## Error Codes

This module raises standard Python exceptions rather than custom error codes:

| Condition | Exception | Context |
|-----------|-----------|---------|
| Session not found on restore | `ValueError(f"Session {session_id} not found")` | `restore_state()` when JSONL file is empty or missing |
| File I/O errors | `OSError` / `IOError` | Propagated from file operations (directory creation, file write) |
| JSON parse errors | `json.JSONDecodeError` | Corrupt JSONL line during `get_events()` or `restore_state()` |
| Pydantic validation errors | `pydantic.ValidationError` | Malformed event data during `Event(**data)` deserialization |

## Dependencies

| Dependency | Type | Usage |
|------------|------|-------|
| `core.models` | Internal | `Event`, `EventType`, `SessionState`, `AgentMessage`, `ToolCall` |
| `json` | Stdlib | JSONL serialization/deserialization |
| `pathlib.Path` | Stdlib | File path handling |
| `datetime` | Stdlib | Timestamp generation (UTC) |
| `shutil` | Stdlib | File copy for `checkpoint()` (imported inline) |

## Configuration

| Parameter | Default | Description |
|-----------|---------|-------------|
| `base_path` | `"./data/events"` | Root directory for JSONL files, created on init |
| Context window max size | `50` (hardcoded in `_apply_event`) | Maximum number of `AgentMessage` entries retained in `state.context_window` |

## Extension Points

1. **Storage backend**: The current implementation uses flat JSONL files. A subclass could override
   `_session_file()`, `emit_event()`, and `get_events()` to use a database or object store.
2. **Event replay**: New event types are handled by adding branches in `_apply_event()`. Unknown
   event types are silently ignored.
3. **Context trimming strategy**: Currently a fixed-size FIFO trim (50 entries). Could be made
   configurable or replaced with a summarization strategy.
4. **Checkpoint storage**: Checkpoints are stored as file copies. Could be extended to support
   incremental snapshots or compressed archives.

## Invariants

1. **Append-only**: Events are only ever appended to JSONL files. No event is ever modified or
   deleted in place.
2. **One event per line**: Each line in the JSONL file is exactly one JSON-serialized `Event`
   produced by `event.model_dump(mode="json")`.
3. **Idempotent replay**: Calling `restore_state()` multiple times on the same session ID produces
   an equivalent `SessionState` (not necessarily the same Python object, but structurally identical).
4. **Directory existence**: `__init__` calls `mkdir(parents=True, exist_ok=True)`, guaranteeing
   `base_path` exists after construction.
5. **File-per-session**: Each session maps to exactly one JSONL file named `{session_id}.jsonl`.
6. **Checkpoint isolation**: Checkpoints are stored under `{base_path}/checkpoints/{session_id}/`
   and never modify the original event log.
7. **UTC timestamps**: All timestamps in `create_session()` are generated with `datetime.now(timezone.utc)`.
   The `Event` model uses `datetime.utcnow` as its default factory (note: this is the legacy
   non-timezone-aware default from `core.models`).
