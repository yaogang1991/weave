"""Tests for reporter/logger.py — session report generation."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from core.models import EventType
from reporter.logger import Reporter


@pytest.fixture
def mock_store():
    store = MagicMock()
    state = MagicMock()
    state.status = "completed"
    state.stages_completed = ["plan", "generate", "evaluate"]
    state.metrics.total_tool_calls = 5
    state.metrics.errors = ["timeout on node n1"]
    state.current_stage = None
    store.restore_state.return_value = state

    event = MagicMock()
    event.type = EventType.WORKFLOW_STAGE_START
    event.timestamp = datetime.now(timezone.utc)
    event.payload = {"node_id": "n1"}
    store.get_events.return_value = [event]

    return store


@pytest.fixture
def reporter(tmp_path, mock_store):
    return Reporter(session_store=mock_store, report_path=str(tmp_path / "reports"))


class TestGenerateSessionReport:
    def test_creates_markdown_file(self, reporter):
        path = reporter.generate_session_report("sess_001")
        assert path.endswith(".md")
        with open(path, encoding="utf-8") as f:
            content = f.read()
        assert "Session Report" in content
        assert "sess_001" in content

    def test_report_contains_status(self, reporter):
        path = reporter.generate_session_report("sess_001")
        with open(path, encoding="utf-8") as f:
            content = f.read()
        assert "completed" in content

    def test_report_contains_stages(self, reporter):
        path = reporter.generate_session_report("sess_001")
        with open(path, encoding="utf-8") as f:
            content = f.read()
        assert "plan" in content
        assert "generate" in content

    def test_report_contains_events(self, reporter):
        path = reporter.generate_session_report("sess_001")
        with open(path, encoding="utf-8") as f:
            content = f.read()
        assert "Event Timeline" in content

    def test_report_contains_errors(self, reporter):
        path = reporter.generate_session_report("sess_001")
        with open(path, encoding="utf-8") as f:
            content = f.read()
        assert "Errors" in content
        assert "timeout" in content

    def test_report_path_under_report_dir(self, reporter):
        path = reporter.generate_session_report("sess_001")
        assert str(reporter.report_path) in path


class TestEventIcon:
    def test_known_event_type(self, reporter):
        icon = reporter._event_icon(EventType.USER_MESSAGE)
        assert icon != "•"

    def test_unknown_event_type(self, reporter):
        icon = reporter._event_icon(EventType.SESSION_DAG)
        assert icon == "•"

    def test_all_known_types_have_icons(self, reporter):
        known = [
            EventType.USER_MESSAGE,
            EventType.AGENT_MESSAGE,
            EventType.WORKFLOW_STAGE_START,
            EventType.SESSION_START,
            EventType.SESSION_END,
        ]
        for et in known:
            assert reporter._event_icon(et) != "•"


class TestPrintProgress:
    def test_prints_progress(self, reporter, capsys):
        reporter.print_progress("sess_001")
        captured = capsys.readouterr()
        assert "sess_001"[:8] in captured.out
        assert "completed" in captured.out

    def test_prints_current_stage(self, reporter, capsys):
        reporter.session_store.restore_state.return_value.current_stage = "evaluating"
        reporter.print_progress("sess_001")
        captured = capsys.readouterr()
        assert "evaluating" in captured.out

    def test_prints_error_count(self, reporter, capsys):
        reporter.print_progress("sess_001")
        captured = capsys.readouterr()
        assert "1 errors" in captured.out


class TestReportDirectory:
    def test_creates_report_directory(self, tmp_path, mock_store):
        report_path = tmp_path / "new_reports"
        Reporter(session_store=mock_store, report_path=str(report_path))
        assert report_path.exists()
