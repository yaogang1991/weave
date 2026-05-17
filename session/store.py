"""
Session Manager: append-only event log with recovery capabilities.
Inspired by Anthropic's Session design:
- Session ≠ Context Window
- Events are durable, context is ephemeral
- getEvents() provides positional slicing
- Snapshot + incremental replay avoids full JSONL replay (#454)
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

from core.models import Event, EventType, SessionState, AgentMessage

logger = logging.getLogger(__name__)


class SessionSnapshot(BaseModel):
    """Serialized session state at a point in time (#454)."""
    state: SessionState
    event_index: int
    timestamp: datetime


class SessionStore:
    """Append-only JSONL event store."""

    def __init__(
        self,
        base_path: str = "./data/events",
        max_ctx: int = 50,
        snapshot_interval: int = 50,
    ):
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)
        self._max_ctx = max_ctx
        self._snapshot_interval = snapshot_interval
        # Track event counts per session for auto-snapshot (#510)
        self._event_counts: dict[str, int] = {}

    def _session_file(self, session_id: str) -> Path:
        return self.base_path / f"{session_id}.jsonl"

    def _snapshot_file(self, session_id: str) -> Path:
        return self.base_path / f"{session_id}.snapshot.json"

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
        try:
            with open(file_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event.model_dump(mode="json"), default=str) + "\n")
        except OSError as exc:
            logger.error(
                "Failed to write event %s to session %s: %s",
                event_type.value, session_id, exc,
            )
        # Auto-snapshot: create periodic checkpoint (#510)
        self._event_counts[session_id] = (
            self._event_counts.get(session_id, 0) + 1
        )
        self._maybe_snapshot(session_id)
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
        """Replay events to reconstruct session state.

        If a snapshot exists, loads it and only replays events after
        the snapshot point. Otherwise, replays the full event log (#454).
        """
        snapshot = self._load_snapshot(session_id)
        if snapshot is not None:
            state = snapshot.state
            events = self.get_events(session_id, start=snapshot.event_index)
        else:
            events = self.get_events(session_id)
            if not events:
                raise ValueError(f"Session {session_id} not found")
            state = SessionState(
                session_id=session_id,
                created_at=events[0].timestamp,
                status="created",
            )

        for event in events:
            state = self._apply_event(state, event)

        return state

    def _apply_event(self, state: SessionState, event: Event) -> SessionState:
        """Apply event and return a new SessionState (immutable)."""
        updates: dict = {}

        if event.type == EventType.SESSION_START:
            updates["status"] = "running"
        elif event.type == EventType.SESSION_IDLE:
            updates["status"] = "idle"
        elif event.type == EventType.SESSION_END:
            updates["status"] = "completed"
        elif event.type == EventType.SESSION_ERROR:
            updates["status"] = "error"
            new_errors = list(state.metrics.errors) + [
                event.payload.get("error", "Unknown")
            ]
            updates["metrics"] = state.metrics.model_copy(
                update={"errors": new_errors}
            )
        elif event.type == EventType.WORKFLOW_STAGE_START:
            updates["current_stage"] = event.payload.get("stage_name")
            updates["status"] = "running"
        elif event.type == EventType.WORKFLOW_STAGE_END:
            stage_name = event.payload.get("stage_name")
            new_completed = list(state.stages_completed)
            if stage_name and stage_name not in new_completed:
                new_completed.append(stage_name)
            updates["stages_completed"] = new_completed
            updates["current_stage"] = None
            updates["status"] = "idle"
        elif event.type == EventType.AGENT_MESSAGE:
            msg = AgentMessage(**event.payload)
            new_window = list(state.context_window) + [msg]
            if len(new_window) > self._max_ctx:
                new_window = new_window[-self._max_ctx:]
            updates["context_window"] = new_window
        elif event.type == EventType.AGENT_TOOL_USE:
            updates["metrics"] = state.metrics.model_copy(
                update={"total_tool_calls": state.metrics.total_tool_calls + 1}
            )
        elif event.type == EventType.TOOL_EXEC_END:
            updates["metrics"] = state.metrics.model_copy(
                update={
                    "total_duration_ms": (
                        state.metrics.total_duration_ms
                        + event.payload.get("duration_ms", 0)
                    )
                }
            )
        elif event.type == EventType.EVAL_RESULT:
            if not event.payload.get("passed", False):
                new_errors = list(state.metrics.errors) + [
                    f"Stage {state.current_stage} failed eval"
                ]
                updates["metrics"] = state.metrics.model_copy(
                    update={"errors": new_errors}
                )

        if not updates:
            return state
        return state.model_copy(update=updates)

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

    def checkpoint(self, session_id: str, label: str = "") -> None:
        """Create a snapshot of current state and optionally truncate log.

        Writes a SessionSnapshot file, then truncates the event log up to
        the snapshot point to control file size (#454).

        Args:
            session_id: Session to checkpoint.
            label: Optional label for the checkpoint (used for log message).
        """
        state = self.restore_state(session_id)
        events = self.get_events(session_id)
        snapshot = SessionSnapshot(
            state=state,
            event_index=len(events),
            timestamp=datetime.now(timezone.utc),
        )
        self._save_snapshot(session_id, snapshot)
        self._truncate_log(session_id, snapshot.event_index)
        logger.info(
            "Session %s checkpointed at event %d%s",
            session_id, snapshot.event_index,
            f" (label: {label})" if label else "",
        )

    # -- Snapshot helpers (#454) --

    def _maybe_snapshot(self, session_id: str) -> None:
        """Auto-snapshot when event count reaches interval (#510).

        Creates a snapshot and truncates the log to keep file size bounded.
        Skips if session has too few events or snapshot fails.
        """
        count = self._event_counts.get(session_id, 0)
        if count < self._snapshot_interval or count % self._snapshot_interval != 0:
            return
        try:
            self.checkpoint(session_id, label="auto")
            # Reset counter after truncation — remaining events start from 0
            self._event_counts[session_id] = 0
        except Exception as exc:
            logger.debug("Auto-snapshot failed for session %s: %s", session_id, exc)

    def _save_snapshot(self, session_id: str, snapshot: SessionSnapshot) -> None:
        """Write snapshot to disk."""
        path = self._snapshot_file(session_id)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(json.dumps(snapshot.model_dump(mode="json"), indent=2, default=str))
        except OSError as exc:
            logger.error("Failed to save snapshot for session %s: %s", session_id, exc)

    def _load_snapshot(self, session_id: str) -> SessionSnapshot | None:
        """Load snapshot from disk, or None if not found."""
        path = self._snapshot_file(session_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return SessionSnapshot.model_validate(data)
        except Exception as exc:
            logger.warning("Failed to load snapshot for session %s: %s", session_id, exc)
            return None

    def _truncate_log(self, session_id: str, event_index: int) -> None:
        """Remove events up to event_index from the JSONL log."""
        path = self._session_file(session_id)
        if not path.exists():
            return

        try:
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
            remaining = lines[event_index:]
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(remaining)
        except OSError as exc:
            logger.error(
                "Failed to truncate log for session %s at %d: %s",
                session_id, event_index, exc,
            )
