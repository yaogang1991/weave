"""Tests for session snapshot + incremental replay (#454).

Verifies:
1. restore_state() loads snapshot and only replays post-snapshot events
2. checkpoint() writes snapshot and truncates the log
3. Falls back to full replay when no snapshot exists
4. Snapshot file path: data/events/{session_id}.snapshot.json
"""
import json
import sys
from pathlib import Path

import pytest

# Ensure project root on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # noqa: E402

from session.store import SessionStore, SessionSnapshot  # noqa: E402
from core.models import EventType  # noqa: E402


@pytest.fixture
def store(tmp_path):
    """Create a SessionStore with a temp directory."""
    return SessionStore(base_path=str(tmp_path / "events"))


class TestSnapshotRestore:
    """Verify restore_state uses snapshots correctly."""

    def test_restore_without_snapshot_replays_all_events(self, store):
        """Without a snapshot, restore_state replays the full event log."""
        sid = "test-no-snapshot"
        store.create_session(sid, "test_workflow")
        store.emit_event(sid, EventType.AGENT_MESSAGE, {"role": "assistant", "content": "hello"})
        store.emit_event(sid, EventType.AGENT_MESSAGE, {"role": "assistant", "content": "world"})

        state = store.restore_state(sid)
        assert state.session_id == sid
        assert len(state.context_window) == 2

    def test_restore_with_snapshot_skips_replayed_events(self, store):
        """With a snapshot, only post-snapshot events are replayed."""
        sid = "test-with-snapshot"
        store.create_session(sid, "test_workflow")
        store.emit_event(sid, EventType.AGENT_MESSAGE, {"role": "assistant", "content": "before"})

        # Create snapshot
        state_before = store.restore_state(sid)
        snapshot = SessionSnapshot(
            state=state_before,
            event_index=3,  # after SESSION_START + AGENT_MESSAGE + next
            timestamp=state_before.created_at,
        )
        store._save_snapshot(sid, snapshot)
        store._truncate_log(sid, snapshot.event_index)

        # Add post-snapshot events
        store.emit_event(sid, EventType.AGENT_MESSAGE, {"role": "assistant", "content": "after"})

        state = store.restore_state(sid)
        # context_window should have snapshot state + post-snapshot event
        assert state.session_id == sid

    def test_snapshot_file_path(self, store):
        """Snapshot file is at data/events/{session_id}.snapshot.json."""
        sid = "test-path"
        store.create_session(sid, "test")
        state = store.restore_state(sid)
        snapshot = SessionSnapshot(
            state=state,
            event_index=2,
            timestamp=state.created_at,
        )
        store._save_snapshot(sid, snapshot)

        expected = store.base_path / f"{sid}.snapshot.json"
        assert expected.exists()

    def test_corrupt_snapshot_falls_back_to_full_replay(self, store):
        """A corrupt snapshot triggers fallback to full replay."""
        sid = "test-corrupt"
        store.create_session(sid, "test")
        store.emit_event(sid, EventType.AGENT_MESSAGE, {"role": "assistant", "content": "ok"})

        # Write a corrupt snapshot
        snapshot_path = store._snapshot_file(sid)
        snapshot_path.write_text("NOT VALID JSON{{{")

        state = store.restore_state(sid)
        assert state.session_id == sid
        assert len(state.context_window) == 1


class TestCheckpoint:
    """Verify checkpoint creates snapshot and truncates log."""

    def test_checkpoint_creates_snapshot_file(self, store):
        """checkpoint() writes a snapshot.json file."""
        sid = "test-checkpoint-file"
        store.create_session(sid, "test")
        store.emit_event(sid, EventType.AGENT_MESSAGE, {"role": "assistant", "content": "msg"})

        store.checkpoint(sid, label="first")

        snapshot_path = store._snapshot_file(sid)
        assert snapshot_path.exists()
        data = json.loads(snapshot_path.read_text())
        assert data["event_index"] == 2  # SESSION_START + AGENT_MESSAGE

    def test_checkpoint_truncates_log(self, store):
        """checkpoint() truncates the JSONL log up to event_index."""
        sid = "test-truncate"
        store.create_session(sid, "test")
        for i in range(5):
            store.emit_event(
                sid, EventType.AGENT_MESSAGE,
                {"role": "assistant", "content": f"msg-{i}"},
            )

        events_before = store.get_events(sid)
        assert len(events_before) == 6  # START + 5 messages

        store.checkpoint(sid)

        # Log should be truncated — only events after snapshot remain
        events_after = store.get_events(sid)
        assert len(events_after) == 0  # All events were before snapshot

    def test_checkpoint_preserves_state(self, store):
        """After checkpoint + new events, restore_state returns correct state."""
        sid = "test-preserve"
        store.create_session(sid, "test")
        store.emit_event(
            sid, EventType.AGENT_MESSAGE,
            {"role": "assistant", "content": "before-checkpoint"},
        )

        store.checkpoint(sid)

        # Add new events after checkpoint
        store.emit_event(
            sid, EventType.AGENT_MESSAGE,
            {"role": "assistant", "content": "after-checkpoint"},
        )

        state = store.restore_state(sid)
        assert state.session_id == sid
        assert len(state.context_window) == 1  # Only post-checkpoint message

    def test_empty_snapshot_returns_none(self, store):
        """_load_snapshot returns None when no snapshot file exists."""
        result = store._load_snapshot("nonexistent-session")
        assert result is None
