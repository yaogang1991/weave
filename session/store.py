"""
Session Manager: append-only event log with recovery capabilities.
Inspired by Anthropic's Session design:
- Session ≠ Context Window
- Events are durable, context is ephemeral
- getEvents() provides positional slicing
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from core.models import Event, EventType, SessionState, AgentMessage


class SessionStore:
    """Append-only JSONL event store."""

    def __init__(self, base_path: str = "./data/events"):
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)

    def _session_file(self, session_id: str) -> Path:
        return self.base_path / f"{session_id}.jsonl"

    def create_session(self, session_id: str, workflow_name: str) -> SessionState:
        state = SessionState(
            session_id=session_id,
            created_at=datetime.now(timezone.utc),
            status="created",
        )
        self.emit_event(session_id, EventType.SESSION_START, {
            "workflow": workflow_name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        return state

    def emit_event(
        self,
        session_id: str,
        event_type: EventType,
        payload: dict,
        metadata: dict | None = None,
    ) -> Event:
        event = Event(
            type=event_type,
            session_id=session_id,
            payload=payload,
            metadata=metadata or {},
        )
        file_path = self._session_file(session_id)
        with open(file_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event.model_dump(mode="json"), default=str) + "\n")
        return event

    def get_events(
        self,
        session_id: str,
        start: int | None = None,
        end: int | None = None,
        event_type: EventType | None = None,
    ) -> list[Event]:
        file_path = self._session_file(session_id)
        if not file_path.exists():
            return []

        events = []
        with open(file_path, "r", encoding="utf-8") as f:
            for idx, line in enumerate(f):
                if start is not None and idx < start:
                    continue
                if end is not None and idx >= end:
                    break
                data = json.loads(line.strip())
                event = Event(**data)
                if event_type is None or event.type == event_type:
                    events.append(event)
        return events

    def restore_state(self, session_id: str) -> SessionState:
        """Replay events to reconstruct session state."""
        events = self.get_events(session_id)
        if not events:
            raise ValueError(f"Session {session_id} not found")

        state = SessionState(
            session_id=session_id,
            created_at=events[0].timestamp,
            status="created",
        )

        for event in events:
            self._apply_event(state, event)

        return state

    def _apply_event(self, state: SessionState, event: Event) -> None:
        if event.type == EventType.SESSION_START:
            state.status = "running"
        elif event.type == EventType.SESSION_IDLE:
            state.status = "idle"
        elif event.type == EventType.SESSION_END:
            state.status = "completed"
        elif event.type == EventType.SESSION_ERROR:
            state.status = "error"
            state.metrics.errors.append(event.payload.get("error", "Unknown"))
        elif event.type == EventType.WORKFLOW_STAGE_START:
            state.current_stage = event.payload.get("stage_name")
            state.status = "running"
        elif event.type == EventType.WORKFLOW_STAGE_END:
            stage_name = event.payload.get("stage_name")
            if stage_name and stage_name not in state.stages_completed:
                state.stages_completed.append(stage_name)
            state.current_stage = None
            state.status = "idle"
        elif event.type == EventType.AGENT_MESSAGE:
            msg = AgentMessage(**event.payload)
            state.context_window.append(msg)
            # Trim context window
            max_ctx = 50  # configurable
            if len(state.context_window) > max_ctx:
                state.context_window = state.context_window[-max_ctx:]
        elif event.type == EventType.AGENT_TOOL_USE:
            state.metrics.total_tool_calls += 1
        elif event.type == EventType.TOOL_EXEC_END:
            state.metrics.total_duration_ms += event.payload.get("duration_ms", 0)
        elif event.type == EventType.EVAL_RESULT:
            if not event.payload.get("passed", False):
                state.metrics.errors.append(
                    f"Stage {state.current_stage} failed eval"
                )

    def list_sessions(self) -> list[str]:
        return [f.stem for f in self.base_path.glob("*.jsonl")]

    def exists(self, session_id: str) -> bool:
        """Check whether a session exists."""
        return self._session_file(session_id).exists()

    def get_summary(self, session_id: str) -> dict:
        """Return a summary dict for a session.

        Derives status from SESSION_START/SESSION_END/SESSION_ERROR events.
        Reads DAG execution summary from SESSION_END.payload["summary"] if
        available. Returns empty dict if session not found.
        """
        events = self.get_events(session_id)
        if not events:
            return {}

        status = "created"
        created_at = None
        errors: list[str] = []
        execution: dict | None = None
        node_details: dict[str, dict] = {}

        for ev in events:
            payload = ev.payload

            if ev.type == EventType.SESSION_START:
                status = "running"
                created_at = payload.get("timestamp") or ev.timestamp.isoformat()

            elif ev.type == EventType.SESSION_END:
                status = "completed"
                execution = payload.get("summary")

            elif ev.type == EventType.SESSION_ERROR:
                status = "error"
                errors.append(payload.get("error", "Unknown"))

            elif ev.type == EventType.WORKFLOW_STAGE_START:
                nid = payload.get("node_id", "")
                if nid:
                    node_details.setdefault(nid, {})["agent_type"] = payload.get("agent_type", "")
                    node_details[nid]["task"] = payload.get("task", "")

            elif ev.type == EventType.WORKFLOW_STAGE_END:
                nid = payload.get("node_id", "")
                if nid:
                    node_details.setdefault(nid, {})["status"] = "success"

            elif ev.type == EventType.WORKFLOW_STAGE_ERROR:
                nid = payload.get("node_id", "")
                if nid:
                    node_details.setdefault(nid, {})["status"] = "failed"
                    node_details[nid]["error"] = payload.get("error", "")

        # Build node_results from node_details
        node_results: dict[str, str] = {}
        for nid, info in node_details.items():
            node_results[nid] = info.get("status", "unknown")

        result: dict = {
            "session_id": session_id,
            "source": "session_store",
            "status": status,
            "created_at": created_at,
            "event_count": len(events),
            "node_results": node_results,
            "errors": errors[:10],
        }

        if execution:
            result["execution"] = execution

        if node_details:
            result["node_details"] = node_details

        return result

    def checkpoint(self, session_id: str, label: str) -> None:
        """Create a named checkpoint by copying current event log."""
        src = self._session_file(session_id)
        if src.exists():
            checkpoint_dir = self.base_path / "checkpoints" / session_id
            checkpoint_dir.mkdir(parents=True, exist_ok=True)
            dst = checkpoint_dir / f"{label}.jsonl"
            import shutil
            shutil.copy(src, dst)
