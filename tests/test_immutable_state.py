"""Tests for immutable state in SessionStore._apply_event (#457)."""
from datetime import datetime, timezone

import pytest

from core.models import EventType, SessionState
from session.store import SessionStore


@pytest.fixture
def store(tmp_path):
    return SessionStore(base_path=str(tmp_path / "events"))


@pytest.fixture
def fresh_state():
    return SessionState(
        session_id="test-immutable",
        created_at=datetime.now(timezone.utc),
    )


class TestImmutableApplyEvent:
    """_apply_event must return a new SessionState, never mutate input."""

    def test_apply_event_returns_new_object(self, store, fresh_state):
        event = store.emit_event(
            "test-immutable", EventType.SESSION_START, {"workflow": "w"}
        )
        result = store._apply_event(fresh_state, event)
        assert result is not fresh_state
        assert isinstance(result, SessionState)

    def test_apply_event_does_not_mutate_original(self, store, fresh_state):
        event = store.emit_event(
            "test-immutable", EventType.SESSION_START, {"workflow": "w"}
        )
        original_status = fresh_state.status
        original_stage = fresh_state.current_stage
        _ = store._apply_event(fresh_state, event)
        assert fresh_state.status == original_status
        assert fresh_state.current_stage == original_stage

    def test_apply_event_preserves_previous_fields(self, store, fresh_state):
        """Fields set by earlier events are preserved through later events."""
        e1 = store.emit_event(
            "test-immutable", EventType.SESSION_START, {"workflow": "w"}
        )
        state1 = store._apply_event(fresh_state, e1)
        assert state1.status == "running"

        e2 = store.emit_event(
            "test-immutable", EventType.WORKFLOW_STAGE_START,
            {"stage_name": "plan"},
        )
        state2 = store._apply_event(state1, e2)
        assert state2.status == "running"
        assert state2.current_stage == "plan"
        # state1 unchanged
        assert state1.current_stage is None

    def test_restore_state_accumulates_immutably(self, store):
        """restore_state returns a valid state without mutating intermediates."""
        store.emit_event("test-imm", EventType.SESSION_START, {"workflow": "w"})
        store.emit_event(
            "test-imm", EventType.WORKFLOW_STAGE_START, {"stage_name": "plan"}
        )
        store.emit_event(
            "test-imm", EventType.WORKFLOW_STAGE_END, {"stage_name": "plan"}
        )
        state = store.restore_state("test-imm")
        assert state.status == "idle"
        assert "plan" in state.stages_completed

    def test_agent_message_does_not_mutate_context(self, store, fresh_state):
        msg_payload = {
            "role": "assistant",
            "content": "hello",
        }
        event = store.emit_event(
            "test-immutable", EventType.AGENT_MESSAGE, msg_payload
        )
        original_len = len(fresh_state.context_window)
        _ = store._apply_event(fresh_state, event)
        assert len(fresh_state.context_window) == original_len

    def test_max_ctx_configurable(self, tmp_path, fresh_state):
        """max_ctx from constructor is respected, not hardcoded (#485)."""
        store = SessionStore(base_path=str(tmp_path / "events"), max_ctx=2)
        for i in range(5):
            event = store.emit_event(
                "test-immutable",
                EventType.AGENT_MESSAGE,
                {"role": "assistant", "content": f"msg-{i}"},
            )
            fresh_state = store._apply_event(fresh_state, event)

        assert len(fresh_state.context_window) == 2
        assert fresh_state.context_window[0].content == "msg-3"
        assert fresh_state.context_window[1].content == "msg-4"


class TestRestoreFileSnapshotBackup:
    """restore_file_snapshot must create backup before overwriting (#457)."""

    def test_restore_creates_backup(self, tmp_path):
        from core.retry_policy import RetryPolicyEngine

        work_dir = tmp_path / "work"
        work_dir.mkdir()
        original_file = work_dir / "app.py"
        original_file.write_text("original content")

        snapshot = {"app.py": "restored content"}
        RetryPolicyEngine.restore_file_snapshot(str(work_dir), snapshot)

        backup_file = work_dir / "app.py.bak"
        assert backup_file.exists()
        assert backup_file.read_text() == "original content"
        assert original_file.read_text() == "restored content"

    def test_restore_no_backup_when_no_original(self, tmp_path):
        from core.retry_policy import RetryPolicyEngine

        work_dir = tmp_path / "work"
        work_dir.mkdir()
        snapshot = {"new_file.py": "new content"}
        RetryPolicyEngine.restore_file_snapshot(str(work_dir), snapshot)

        assert (work_dir / "new_file.py").read_text() == "new content"
        assert not (work_dir / "new_file.py.bak").exists()
