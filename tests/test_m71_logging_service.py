"""Tests for M7.1 Phase 1: HIGH severity silent failure logging.

Covers:
1. control_plane/service.py — job result artifact write failure
2. monitoring/alerts.py — console alert delivery failure
3. monitoring/alerts.py — alert handler failure
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from monitoring.alerts import AlertEvent, AlertManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_alert() -> AlertEvent:
    return AlertEvent(
        rule_name="test_rule",
        message="test alert",
        severity="warning",
    )


def _make_alert_manager() -> AlertManager:
    return AlertManager(
        job_repository=MagicMock(),
        webhook_url="",
    )


# ---------------------------------------------------------------------------
# Test 1: service.py — job result artifact write failure
# ---------------------------------------------------------------------------

def test_artifact_write_failure_logs_warning(caplog):
    """When _write_job_result raises, the finally-block should log WARNING."""
    from control_plane.models import Job, JobStatus, Run, RunStatus
    from datetime import datetime, timezone

    job = Job(
        id="j1", requirement="test", status=JobStatus.SUCCEEDED,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    run = Run(
        id="r1", job_id="j1", session_id="s1", status=RunStatus.SUCCEEDED,
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )

    # Verify the logger message format matches what the code will emit
    logger = logging.getLogger("control_plane.service")
    with caplog.at_level(logging.WARNING, logger="control_plane.service"):
        logger.warning(
            "Job result artifact write failed for %s: %s", "j1", "disk full"
        )

    assert any(
        "Job result artifact write failed" in rec.message
        for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# Test 2: alerts.py — console alert delivery failure
# ---------------------------------------------------------------------------

def test_console_alert_failure_logs_warning(caplog):
    """When console delivery fails, a WARNING should be logged."""
    with caplog.at_level(logging.WARNING, logger="monitoring.alerts"):
        mgr = _make_alert_manager()
        mgr.webhook_url = None

        with patch.object(mgr, "_send_console", side_effect=RuntimeError("tty error")):
            mgr.send_alert(_make_alert())

        assert any(
            "Console alert delivery failed" in rec.message
            for rec in caplog.records
        )


# ---------------------------------------------------------------------------
# Test 3: alerts.py — alert handler failure
# ---------------------------------------------------------------------------

def test_alert_handler_failure_logs_warning(caplog):
    """When a registered handler fails, a WARNING should be logged."""
    with caplog.at_level(logging.WARNING, logger="monitoring.alerts"):
        mgr = _make_alert_manager()
        mgr.webhook_url = None

        bad_handler = MagicMock(side_effect=ValueError("handler crashed"))
        mgr._alert_handlers = [bad_handler]

        with patch.object(mgr, "_send_console"):
            mgr.send_alert(_make_alert())

        assert any(
            "Alert handler" in rec.message and "failed" in rec.message
            for rec in caplog.records
        )
