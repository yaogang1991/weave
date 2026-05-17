"""Tests for SessionStore.exists() and get_summary()."""
import pytest
from core.models import EventType
from session.store import SessionStore


@pytest.fixture
def store(tmp_path):
    return SessionStore(base_path=str(tmp_path / "events"))


class TestExists:
    def test_exists_false_for_unknown(self, store):
        assert not store.exists("nonexistent")

    def test_exists_true_after_create(self, store):
        store.create_session("s1", "test")
        assert store.exists("s1")


class TestGetSummaryNotFound:
    def test_returns_empty_for_unknown(self, store):
        assert store.get_summary("nonexistent") == {}


class TestGetSummaryRunning:
    def test_running_after_start(self, store):
        store.create_session("s1", "test")
        summary = store.get_summary("s1")
        assert summary["status"] == "running"
        assert summary["session_id"] == "s1"
        assert summary["source"] == "session_store"
        assert summary["event_count"] == 1
        assert "created_at" in summary


class TestGetSummaryCompleted:
    def test_completed_with_execution_summary(self, store):
        store.create_session("s1", "test")
        # Simulate a node start/end
        store.emit_event("s1", EventType.WORKFLOW_STAGE_START, {
            "node_id": "impl_api",
            "agent_type": "generator",
            "task": "Implement API",
        })
        store.emit_event("s1", EventType.WORKFLOW_STAGE_END, {
            "node_id": "impl_api",
        })
        # Simulate completion with execution summary
        store.emit_event("s1", EventType.SESSION_END, {
            "summary": {
                "total_nodes": 1,
                "success": 1,
                "failed": 0,
                "skipped": 0,
                "all_succeeded": True,
            },
        })

        summary = store.get_summary("s1")
        assert summary["status"] == "completed"
        assert summary["execution"]["total_nodes"] == 1
        assert summary["execution"]["all_succeeded"] is True
        assert summary["node_results"]["impl_api"] == "success"
        assert summary["node_details"]["impl_api"]["agent_type"] == "generator"
        assert summary["node_details"]["impl_api"]["task"] == "Implement API"
        assert summary["node_details"]["impl_api"]["status"] == "success"
        assert summary["event_count"] == 4  # start + stage_start + stage_end + session_end


class TestGetSummaryError:
    def test_error_with_message(self, store):
        store.create_session("s1", "test")
        store.emit_event("s1", EventType.WORKFLOW_STAGE_START, {
            "node_id": "impl_api",
            "agent_type": "generator",
            "task": "Implement API",
        })
        store.emit_event("s1", EventType.WORKFLOW_STAGE_ERROR, {
            "node_id": "impl_api",
            "error": "LLM timeout",
        })
        store.emit_event("s1", EventType.SESSION_ERROR, {
            "error": "Execution failed: RuntimeError: LLM timeout",
        })

        summary = store.get_summary("s1")
        assert summary["status"] == "error"
        assert summary["node_results"]["impl_api"] == "failed"
        assert summary["node_details"]["impl_api"]["error"] == "LLM timeout"
        assert "Execution failed" in summary["errors"][0]

    def test_error_without_stage_events(self, store):
        store.create_session("s1", "test")
        store.emit_event("s1", EventType.SESSION_ERROR, {
            "error": "Plan generation failed",
        })
        summary = store.get_summary("s1")
        assert summary["status"] == "error"
        assert summary["errors"] == ["Plan generation failed"]
        assert summary["node_results"] == {}


class TestGetSummaryMixed:
    def test_partial_success(self, store):
        store.create_session("s1", "test")
        # Node 1 succeeds
        store.emit_event("s1", EventType.WORKFLOW_STAGE_START, {
            "node_id": "plan", "agent_type": "planner", "task": "Plan DAG",
        })
        store.emit_event("s1", EventType.WORKFLOW_STAGE_END, {"node_id": "plan"})
        # Node 2 fails
        store.emit_event("s1", EventType.WORKFLOW_STAGE_START, {
            "node_id": "impl", "agent_type": "generator", "task": "Generate code",
        })
        store.emit_event("s1", EventType.WORKFLOW_STAGE_ERROR, {
            "node_id": "impl", "error": "Syntax error in output",
        })
        # Session completes
        store.emit_event("s1", EventType.SESSION_END, {
            "summary": {
                "total_nodes": 2, "success": 1,
                "failed": 1, "skipped": 0,
                "all_succeeded": False,
            },
        })

        summary = store.get_summary("s1")
        assert summary["status"] == "completed"
        assert summary["node_results"]["plan"] == "success"
        assert summary["node_results"]["impl"] == "failed"
        assert summary["execution"]["all_succeeded"] is False
