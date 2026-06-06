"""Tests for core/event_models.py — Event and session domain models.

Covers:
- EventType enum completeness and naming convention
- Event model creation, immutability
- SessionState model and transitions
- SessionMetrics model
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from core.event_models import Event, EventType, SessionMetrics, SessionState


# ---------------------------------------------------------------------------
# EventType
# ---------------------------------------------------------------------------


class TestEventType:
    """Test EventType enum values and naming convention."""

    def test_all_event_types_follow_convention(self):
        """All EventType values should follow domain.action convention.

        Known violations (legacy, non-blocking):
        - DEGENERATION_RECOVERED: 'degeneration_recovered' (no dot separator)
        """
        violations = []
        for et in EventType:
            if "." not in et.value:
                violations.append(et.name)
        # Only DEGENERATION_RECOVERED is a known legacy violation
        assert violations == ["DEGENERATION_RECOVERED"], (
            f"Unexpected convention violations: {violations}"
        )

    def test_core_event_types_exist(self):
        """Verify core event types are defined."""
        assert EventType.SESSION_START
        assert EventType.SESSION_END
        assert EventType.AGENT_TOOL_USE
        assert EventType.WORKFLOW_STAGE_START
        assert EventType.EVAL_RESULT
        assert EventType.BUDGET_WARNING

    def test_event_type_values_are_strings(self):
        for et in EventType:
            assert isinstance(et.value, str)

    def test_event_type_count(self):
        """Ensure we have a reasonable number of event types."""
        assert len(EventType) >= 30  # At least 30 defined types

    def test_trace_events_exist(self):
        """M5.1 trace events should be defined."""
        assert EventType.TRACE_RUN_START
        assert EventType.TRACE_RUN_END
        assert EventType.TRACE_NODE_START
        assert EventType.TRACE_NODE_END
        assert EventType.TRACE_LLM_TURN
        assert EventType.TRACE_TOOL_CALL

    def test_memory_events_exist(self):
        """M3.2 memory events should be defined."""
        assert EventType.MEMORY_STORED
        assert EventType.MEMORY_ACCESSED
        assert EventType.MEMORY_SHARED


# ---------------------------------------------------------------------------
# Event
# ---------------------------------------------------------------------------


class TestEvent:
    """Test Event model."""

    def test_event_creation_minimal(self):
        event = Event(type=EventType.SESSION_START, session_id="s1")
        assert event.type == EventType.SESSION_START
        assert event.session_id == "s1"
        assert event.payload == {}
        assert event.metadata == {}

    def test_event_auto_id(self):
        event = Event(type=EventType.SESSION_START, session_id="s1")
        # Should have a valid UUID
        uuid.UUID(event.id)

    def test_event_auto_timestamp(self):
        event = Event(type=EventType.SESSION_START, session_id="s1")
        assert isinstance(event.timestamp, datetime)
        assert event.timestamp.tzinfo is not None

    def test_event_with_payload(self):
        event = Event(
            type=EventType.AGENT_TOOL_USE,
            session_id="s1",
            payload={"tool": "bash", "args": ["ls"]},
        )
        assert event.payload["tool"] == "bash"

    def test_event_unique_ids(self):
        e1 = Event(type=EventType.SESSION_START, session_id="s1")
        e2 = Event(type=EventType.SESSION_START, session_id="s1")
        assert e1.id != e2.id

    def test_event_serialization(self):
        event = Event(
            type=EventType.WORKFLOW_STAGE_START,
            session_id="s1",
            payload={"stage": "plan"},
        )
        data = event.model_dump()
        assert data["type"] == "workflow.stage_start"
        assert data["session_id"] == "s1"
        assert data["payload"]["stage"] == "plan"


# ---------------------------------------------------------------------------
# SessionMetrics
# ---------------------------------------------------------------------------


class TestSessionMetrics:
    """Test SessionMetrics model."""

    def test_default_metrics(self):
        m = SessionMetrics()
        assert m.total_events == 0
        assert m.total_tool_calls == 0
        assert m.total_tokens_input == 0
        assert m.total_tokens_output == 0
        assert m.total_duration_ms == 0
        assert m.stage_durations == {}
        assert m.errors == []

    def test_metrics_with_values(self):
        m = SessionMetrics(
            total_events=42,
            total_tool_calls=10,
            total_tokens_input=5000,
            total_tokens_output=2000,
            total_duration_ms=30000,
            stage_durations={"plan": 5000, "execute": 25000},
            errors=["timeout on node_3"],
        )
        assert m.total_events == 42
        assert len(m.errors) == 1


# ---------------------------------------------------------------------------
# SessionState
# ---------------------------------------------------------------------------


class TestSessionState:
    """Test SessionState model."""

    def test_creation_minimal(self):
        now = datetime.now(UTC)
        state = SessionState(session_id="s1", created_at=now)
        assert state.session_id == "s1"
        assert state.status == "created"
        assert state.current_stage is None
        assert state.stages_completed == []

    def test_all_statuses(self):
        now = datetime.now(UTC)
        for status in ("created", "running", "idle", "error", "completed"):
            state = SessionState(session_id="s1", created_at=now, status=status)
            assert state.status == status

    def test_invalid_status_rejected(self):
        now = datetime.now(UTC)
        with pytest.raises(Exception):
            SessionState(session_id="s1", created_at=now, status="invalid")

    def test_with_context_window(self):
        now = datetime.now(UTC)
        state = SessionState(
            session_id="s1",
            created_at=now,
            context_window=[],
        )
        assert state.context_window == []

    def test_with_metrics(self):
        now = datetime.now(UTC)
        metrics = SessionMetrics(total_events=10)
        state = SessionState(
            session_id="s1",
            created_at=now,
            metrics=metrics,
        )
        assert state.metrics.total_events == 10

    def test_serialization_round_trip(self):
        now = datetime.now(UTC)
        state = SessionState(
            session_id="s1",
            created_at=now,
            status="running",
            current_stage="execute",
            stages_completed=["plan"],
            artifacts={"main.py": "artifact_1"},
        )
        data = state.model_dump()
        restored = SessionState(**data)
        assert restored.session_id == state.session_id
        assert restored.status == state.status
        assert restored.stages_completed == ["plan"]
