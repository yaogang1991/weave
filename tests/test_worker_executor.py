"""Tests for control_plane/worker_executor.py.

Covers:
- _json_log: structured JSON output to stderr
- finalize_pending_approval_run: finalize runs stuck in PENDING_APPROVAL
- handle_failure: delegate failure handling to run_service
- execute_job_core: LEASED->RUNNING transition, success/failure/approval paths
- poll_for_approval: approval polling loop, approved/rejected/expired/timeout/stop_event
- CancelledError: lease release on cancellation
- PendingApprovalError: PENDING_APPROVAL transition and multi-approval loop
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from io import StringIO
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from control_plane.approval import ApprovalTicket, TicketStatus
from control_plane.models import Job, JobStatus, Run, RunStatus
from core.exceptions import PendingApprovalError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_utc_now = lambda: datetime.now(timezone.utc)


def _make_job(
    job_id: str = "job_1",
    status: JobStatus = JobStatus.LEASED,
    attempt: int = 1,
) -> Job:
    now = _utc_now()
    return Job(
        id=job_id,
        requirement="test requirement",
        status=status,
        attempt=attempt,
        created_at=now,
        updated_at=now,
    )


def _make_run(
    run_id: str = "run_1",
    job_id: str = "job_1",
    status: RunStatus = RunStatus.SUCCEEDED,
) -> Run:
    now = _utc_now()
    return Run(
        id=run_id,
        job_id=job_id,
        session_id="sess_1",
        status=status,
        started_at=now,
        created_at=now,
        updated_at=now,
    )


def _make_ticket(
    ticket_id: str = "ticket_abc",
    job_id: str = "job_1",
    status: TicketStatus = TicketStatus.APPROVED,
    reason: str = "",
) -> ApprovalTicket:
    from control_plane.approval import TicketStatus as TS

    now = _utc_now()
    return ApprovalTicket(
        id=ticket_id,
        job_id=job_id,
        tool_name="bash",
        args_hash="abcd1234",
        args_preview="ls -la",
        risk_level="high",
        status=status,
        requested_at=now,
        reason=reason,
        created_at=now,
        updated_at=now,
    )


# ===========================================================================
# TestJsonLog
# ===========================================================================


class TestJsonLog:
    """Tests for _json_log helper."""

    def test_basic_emits_valid_json_to_stderr(self):
        from control_plane.worker_executor import _json_log

        buf = StringIO()
        with patch("sys.stderr", buf):
            _json_log("INFO", "hello")

        parsed = json.loads(buf.getvalue().strip())
        assert parsed["level"] == "INFO"
        assert parsed["message"] == "hello"
        assert "ts" in parsed

    def test_includes_job_id_when_provided(self):
        from control_plane.worker_executor import _json_log

        buf = StringIO()
        with patch("sys.stderr", buf):
            _json_log("INFO", "msg", job_id="job_42")

        parsed = json.loads(buf.getvalue().strip())
        assert parsed["job_id"] == "job_42"

    def test_omits_job_id_when_empty(self):
        from control_plane.worker_executor import _json_log

        buf = StringIO()
        with patch("sys.stderr", buf):
            _json_log("INFO", "msg")

        parsed = json.loads(buf.getvalue().strip())
        assert "job_id" not in parsed

    def test_includes_status_when_provided(self):
        from control_plane.worker_executor import _json_log

        buf = StringIO()
        with patch("sys.stderr", buf):
            _json_log("INFO", "msg", status="running")

        parsed = json.loads(buf.getvalue().strip())
        assert parsed["status"] == "running"

    def test_extra_fields_merged(self):
        from control_plane.worker_executor import _json_log

        buf = StringIO()
        with patch("sys.stderr", buf):
            _json_log("INFO", "msg", extra={"ticket_id": "t1", "retry": 3})

        parsed = json.loads(buf.getvalue().strip())
        assert parsed["ticket_id"] == "t1"
        assert parsed["retry"] == 3

    def test_non_serializable_extra_uses_str_fallback(self):
        from control_plane.worker_executor import _json_log

        buf = StringIO()
        with patch("sys.stderr", buf):
            _json_log("INFO", "msg", extra={"obj": object()})

        line = buf.getvalue().strip()
        parsed = json.loads(line)
        # The default=str fallback should produce something like "<object ...>"
        assert "obj" in parsed
        assert "object" in parsed["obj"]


# ===========================================================================
# TestFinalizePendingApprovalRun
# ===========================================================================


class TestFinalizePendingApprovalRun:
    """Tests for finalize_pending_approval_run."""

    def test_finalizes_pending_approval_run_to_failed(self):
        from control_plane.worker_executor import finalize_pending_approval_run

        repo = MagicMock()
        run_pa = _make_run(status=RunStatus.PENDING_APPROVAL)
        run_ok = _make_run(run_id="run_2", status=RunStatus.SUCCEEDED)
        repo.list_runs_by_job.return_value = [run_pa, run_ok]

        finalize_pending_approval_run(repo, "job_1", "failed", "some error")

        # Only the PENDING_APPROVAL run should be updated
        assert repo.update_run.call_count == 1
        updated = repo.update_run.call_args[0][0]
        assert updated.status == RunStatus.FAILED
        assert updated.dag_result == {"error": "some error"}
        assert updated.completed_at is not None

    def test_finalizes_to_aborted(self):
        from control_plane.worker_executor import finalize_pending_approval_run

        repo = MagicMock()
        run_pa = _make_run(status=RunStatus.PENDING_APPROVAL)
        repo.list_runs_by_job.return_value = [run_pa]

        finalize_pending_approval_run(repo, "job_1", "aborted", "canceled")

        updated = repo.update_run.call_args[0][0]
        assert updated.status == RunStatus.ABORTED

    def test_invalid_status_defaults_to_failed(self):
        from control_plane.worker_executor import finalize_pending_approval_run

        repo = MagicMock()
        run_pa = _make_run(status=RunStatus.PENDING_APPROVAL)
        repo.list_runs_by_job.return_value = [run_pa]

        finalize_pending_approval_run(repo, "job_1", "not_a_status", "bad")

        updated = repo.update_run.call_args[0][0]
        assert updated.status == RunStatus.FAILED

    def test_no_pending_approval_runs_does_nothing(self):
        from control_plane.worker_executor import finalize_pending_approval_run

        repo = MagicMock()
        repo.list_runs_by_job.return_value = [
            _make_run(status=RunStatus.SUCCEEDED),
        ]

        finalize_pending_approval_run(repo, "job_1", "failed", "noop")

        repo.update_run.assert_not_called()

    def test_exception_in_repo_is_swallowed(self):
        from control_plane.worker_executor import finalize_pending_approval_run

        repo = MagicMock()
        repo.list_runs_by_job.side_effect = RuntimeError("db down")

        # Should not raise
        buf = StringIO()
        with patch("sys.stderr", buf):
            finalize_pending_approval_run(repo, "job_1", "failed", "swallowed")

        # Warning log emitted
        log_output = buf.getvalue().strip()
        assert log_output  # Something was logged
        parsed = json.loads(log_output)
        assert parsed["level"] == "WARNING"


# ===========================================================================
# TestHandleFailure
# ===========================================================================


class TestHandleFailure:
    """Tests for handle_failure."""

    @pytest.mark.asyncio
    async def test_delegates_to_run_service(self):
        from control_plane.worker_executor import handle_failure

        repo = MagicMock()
        job = _make_job()
        repo.get_job.return_value = job

        run_svc = MagicMock()
        updated_job = _make_job(status=JobStatus.FAILED)
        run_svc.handle_job_failure = AsyncMock(return_value=updated_job)

        await handle_failure(repo, run_svc, "job_1", "oops", "timeout")

        run_svc.handle_job_failure.assert_awaited_once_with(job, "oops", "timeout")

    @pytest.mark.asyncio
    async def test_job_not_found_logs_error(self):
        from control_plane.worker_executor import handle_failure

        repo = MagicMock()
        repo.get_job.return_value = None

        run_svc = MagicMock()

        buf = StringIO()
        with patch("sys.stderr", buf):
            await handle_failure(repo, run_svc, "job_missing", "err", "unknown")

        run_svc.handle_job_failure.assert_not_called()
        log_output = buf.getvalue().strip()
        assert log_output
        parsed = json.loads(log_output)
        assert parsed["level"] == "ERROR"

    @pytest.mark.asyncio
    async def test_run_service_failure_falls_back_to_transition(self):
        from control_plane.worker_executor import handle_failure

        repo = MagicMock()
        job = _make_job()
        repo.get_job.return_value = job

        run_svc = MagicMock()
        run_svc.handle_job_failure = AsyncMock(side_effect=RuntimeError("svc error"))

        buf = StringIO()
        with patch("sys.stderr", buf):
            await handle_failure(repo, run_svc, "job_1", "err", "timeout")

        # Should fall back to direct transition
        repo.transition_job_status.assert_called_once_with(
            "job_1", JobStatus.FAILED, error="err", error_category="timeout",
        )

    @pytest.mark.asyncio
    async def test_transition_also_fails_logs_critical(self):
        from control_plane.worker_executor import handle_failure

        repo = MagicMock()
        job = _make_job()
        repo.get_job.return_value = job
        repo.transition_job_status.side_effect = RuntimeError("transition failed")

        run_svc = MagicMock()
        run_svc.handle_job_failure = AsyncMock(side_effect=RuntimeError("svc error"))

        buf = StringIO()
        with patch("sys.stderr", buf):
            await handle_failure(repo, run_svc, "job_1", "err", "unknown")

        lines = [l for l in buf.getvalue().strip().split("\n") if l.strip()]
        # Should have at least ERROR and CRITICAL logs
        levels = [json.loads(l)["level"] for l in lines]
        assert "ERROR" in levels
        assert "CRITICAL" in levels


# ===========================================================================
# TestExecuteJobCore
# ===========================================================================


class TestExecuteJobCore:
    """Tests for execute_job_core."""

    @pytest.mark.asyncio
    async def test_success_path_transitions_and_logs(self):
        from control_plane.worker_executor import execute_job_core

        repo = MagicMock()
        job = _make_job(status=JobStatus.RUNNING)
        repo.get_job.return_value = job

        run_svc = MagicMock()
        run_svc.run_job = AsyncMock(return_value=_make_run(status=RunStatus.SUCCEEDED))

        buf = StringIO()
        with patch("sys.stderr", buf):
            await execute_job_core(repo, run_svc, "job_1", non_interactive=False)

        repo.transition_job_status.assert_called_once_with("job_1", JobStatus.RUNNING)
        run_svc.run_job.assert_awaited_once_with("job_1")

    @pytest.mark.asyncio
    async def test_non_interactive_expires_tickets(self):
        from control_plane.worker_executor import execute_job_core

        repo = MagicMock()
        repo.get_job.return_value = _make_job(status=JobStatus.RUNNING)

        approval_repo = MagicMock()
        run_svc = MagicMock()
        run_svc.run_job = AsyncMock(return_value=_make_run(status=RunStatus.SUCCEEDED))
        run_svc.approval_repo = approval_repo

        await execute_job_core(repo, run_svc, "job_1", non_interactive=True)

        approval_repo.expire_tickets.assert_called_once()

    @pytest.mark.asyncio
    async def test_non_interactive_without_approval_repo(self):
        from control_plane.worker_executor import execute_job_core

        repo = MagicMock()
        repo.get_job.return_value = _make_job(status=JobStatus.RUNNING)

        run_svc = MagicMock()
        run_svc.run_job = AsyncMock(return_value=_make_run(status=RunStatus.SUCCEEDED))
        run_svc.approval_repo = None

        # Should not raise even with no approval_repo
        await execute_job_core(repo, run_svc, "job_1", non_interactive=True)

    @pytest.mark.asyncio
    async def test_run_requeued_status(self):
        from control_plane.worker_executor import execute_job_core

        repo = MagicMock()
        job = _make_job(status=JobStatus.QUEUED)
        repo.get_job.return_value = job

        run_svc = MagicMock()
        run_svc.run_job = AsyncMock(return_value=_make_run(status=RunStatus.FAILED))

        buf = StringIO()
        with patch("sys.stderr", buf):
            await execute_job_core(repo, run_svc, "job_1", non_interactive=False)

        # Should not raise; logs re-queued status
        log_lines = [json.loads(l) for l in buf.getvalue().strip().split("\n") if l.strip()]
        queued_msgs = [l for l in log_lines if l.get("status") == "queued"]
        assert len(queued_msgs) >= 1

    @pytest.mark.asyncio
    async def test_run_dead_letter_status(self):
        from control_plane.worker_executor import execute_job_core

        repo = MagicMock()
        job = _make_job(status=JobStatus.DEAD_LETTER)
        repo.get_job.return_value = job

        run_svc = MagicMock()
        run_svc.run_job = AsyncMock(return_value=_make_run(status=RunStatus.FAILED))

        buf = StringIO()
        with patch("sys.stderr", buf):
            await execute_job_core(repo, run_svc, "job_1", non_interactive=False)

        log_lines = [json.loads(l) for l in buf.getvalue().strip().split("\n") if l.strip()]
        dead_msgs = [l for l in log_lines if l.get("status") == "dead_letter"]
        assert len(dead_msgs) >= 1

    @pytest.mark.asyncio
    async def test_unexpected_run_status_raises(self):
        from control_plane.worker_executor import execute_job_core

        repo = MagicMock()
        job = _make_job(status=JobStatus.FAILED)
        repo.get_job.return_value = job

        run_svc = MagicMock()
        run_svc.run_job = AsyncMock(return_value=_make_run(status=RunStatus.FAILED))

        with pytest.raises(RuntimeError, match="Run ended with status"):
            await execute_job_core(repo, run_svc, "job_1", non_interactive=False)

    @pytest.mark.asyncio
    async def test_job_disappeared_during_execution_raises(self):
        from control_plane.worker_executor import execute_job_core

        repo = MagicMock()
        # First call (after transition) returns None
        repo.get_job.return_value = None

        run_svc = MagicMock()
        run_svc.run_job = AsyncMock(return_value=_make_run(status=RunStatus.FAILED))

        with pytest.raises(RuntimeError, match="Job disappeared"):
            await execute_job_core(repo, run_svc, "job_1", non_interactive=False)

    @pytest.mark.asyncio
    async def test_cancelled_error_releases_lease(self):
        from control_plane.worker_executor import execute_job_core

        repo = MagicMock()
        repo.transition_job_status.side_effect = asyncio.CancelledError()

        run_svc = MagicMock()

        with pytest.raises(asyncio.CancelledError):
            await execute_job_core(repo, run_svc, "job_1", non_interactive=False)

        repo.release_lease.assert_called_once_with("job_1")

    @pytest.mark.asyncio
    async def test_cancelled_error_lease_release_failure_does_not_swallow(self):
        from control_plane.worker_executor import execute_job_core

        repo = MagicMock()
        repo.transition_job_status.side_effect = asyncio.CancelledError()
        repo.release_lease.side_effect = RuntimeError("lease release boom")

        run_svc = MagicMock()

        # CancelledError should still propagate even if lease release fails
        with pytest.raises(asyncio.CancelledError):
            await execute_job_core(repo, run_svc, "job_1", non_interactive=False)

    @pytest.mark.asyncio
    async def test_pending_approval_error_transitions_and_polls(self):
        from control_plane.worker_executor import execute_job_core

        repo = MagicMock()
        job_after_run = _make_job(status=JobStatus.RUNNING)
        repo.get_job.return_value = job_after_run

        run_svc = MagicMock()
        run_svc.run_job = AsyncMock(
            side_effect=PendingApprovalError("ticket_001")
        )
        run_svc.approval_repo = MagicMock()
        run_svc.approval_timeout_sec = 10

        # Make poll_for_approval immediately return RUNNING (approved)
        approved_ticket = _make_ticket(status=TicketStatus.APPROVED)
        run_svc.approval_repo.get_ticket.return_value = approved_ticket

        # After approval, run_job should succeed on second call
        run_svc.run_job = AsyncMock(
            side_effect=[
                PendingApprovalError("ticket_001"),
                _make_run(status=RunStatus.SUCCEEDED),
            ]
        )

        # Need list_runs_by_job for finalize_pending_approval_run
        repo.list_runs_by_job.return_value = []

        buf = StringIO()
        with patch("sys.stderr", buf):
            await execute_job_core(repo, run_svc, "job_1", non_interactive=False)

        # Should have transitioned to PENDING_APPROVAL then back to RUNNING
        transition_calls = repo.transition_job_status.call_args_list
        statuses = [c[0][1] for c in transition_calls]
        assert JobStatus.PENDING_APPROVAL in statuses
        assert JobStatus.RUNNING in statuses

    @pytest.mark.asyncio
    async def test_multi_approval_loop(self):
        """Test that multiple PendingApprovalErrors trigger multiple polls."""
        from control_plane.worker_executor import execute_job_core

        repo = MagicMock()
        repo.get_job.return_value = _make_job(status=JobStatus.RUNNING)
        repo.list_runs_by_job.return_value = []

        run_svc = MagicMock()
        run_svc.approval_repo = MagicMock()
        run_svc.approval_timeout_sec = 10

        approved_ticket = _make_ticket(status=TicketStatus.APPROVED)
        run_svc.approval_repo.get_ticket.return_value = approved_ticket

        # Three approvals, then success
        run_svc.run_job = AsyncMock(
            side_effect=[
                PendingApprovalError("ticket_001"),
                PendingApprovalError("ticket_002"),
                PendingApprovalError("ticket_003"),
                _make_run(status=RunStatus.SUCCEEDED),
            ]
        )

        buf = StringIO()
        with patch("sys.stderr", buf):
            await execute_job_core(repo, run_svc, "job_1", non_interactive=False)

        # run_job called 4 times total (3 approvals + 1 success)
        assert run_svc.run_job.call_count == 4

    @pytest.mark.asyncio
    async def test_approval_rejected_stops_execution(self):
        """When poll returns non-RUNNING, the multi-approval loop exits."""
        from control_plane.worker_executor import execute_job_core

        repo = MagicMock()
        repo.get_job.return_value = _make_job(status=JobStatus.FAILED)
        repo.list_runs_by_job.return_value = []

        run_svc = MagicMock()
        run_svc.approval_repo = MagicMock()
        run_svc.approval_timeout_sec = 10

        rejected_ticket = _make_ticket(
            status=TicketStatus.REJECTED, reason="too risky"
        )
        run_svc.approval_repo.get_ticket.return_value = rejected_ticket
        run_svc.handle_job_failure = AsyncMock(
            return_value=_make_job(status=JobStatus.FAILED)
        )

        run_svc.run_job = AsyncMock(
            side_effect=PendingApprovalError("ticket_001")
        )

        buf = StringIO()
        with patch("sys.stderr", buf):
            await execute_job_core(repo, run_svc, "job_1", non_interactive=False)

        # Should have transitioned to PENDING_APPROVAL then FAILED
        transition_calls = repo.transition_job_status.call_args_list
        statuses = [c[0][1] for c in transition_calls]
        assert JobStatus.PENDING_APPROVAL in statuses
        assert JobStatus.FAILED in statuses


# ===========================================================================
# TestPollForApproval
# ===========================================================================


class TestPollForApproval:
    """Tests for poll_for_approval."""

    @pytest.mark.asyncio
    async def test_no_approval_repo_returns_failed(self):
        from control_plane.worker_executor import poll_for_approval

        repo = MagicMock()
        run_svc = MagicMock()
        run_svc.approval_repo = None
        run_svc.handle_job_failure = AsyncMock(
            return_value=_make_job(status=JobStatus.FAILED)
        )

        result = await poll_for_approval(repo, run_svc, "job_1", "ticket_1")

        assert result == JobStatus.FAILED

    @pytest.mark.asyncio
    async def test_approved_ticket_returns_running(self):
        from control_plane.worker_executor import poll_for_approval

        repo = MagicMock()
        repo.get_job.return_value = _make_job(status=JobStatus.RUNNING)

        run_svc = MagicMock()
        run_svc.approval_timeout_sec = 10

        approved_ticket = _make_ticket(status=TicketStatus.APPROVED)
        run_svc.approval_repo = MagicMock()
        run_svc.approval_repo.get_ticket.return_value = approved_ticket

        # Patch sleep to avoid waiting
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await poll_for_approval(repo, run_svc, "job_1", "ticket_1")

        assert result == JobStatus.RUNNING
        repo.transition_job_status.assert_called_with("job_1", JobStatus.RUNNING)

    @pytest.mark.asyncio
    async def test_approved_but_job_canceled_returns_canceled(self):
        from control_plane.worker_executor import poll_for_approval

        repo = MagicMock()
        repo.get_job.return_value = _make_job(status=JobStatus.CANCELED)
        repo.list_runs_by_job.return_value = []

        run_svc = MagicMock()
        run_svc.approval_timeout_sec = 10

        approved_ticket = _make_ticket(status=TicketStatus.APPROVED)
        run_svc.approval_repo = MagicMock()
        run_svc.approval_repo.get_ticket.return_value = approved_ticket

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await poll_for_approval(repo, run_svc, "job_1", "ticket_1")

        assert result == JobStatus.CANCELED

    @pytest.mark.asyncio
    async def test_rejected_ticket_returns_failed(self):
        from control_plane.worker_executor import poll_for_approval

        repo = MagicMock()
        failed_job = _make_job(status=JobStatus.FAILED)
        repo.get_job.return_value = failed_job
        repo.list_runs_by_job.return_value = []

        run_svc = MagicMock()
        run_svc.approval_timeout_sec = 10
        run_svc.handle_job_failure = AsyncMock(return_value=failed_job)

        rejected_ticket = _make_ticket(
            status=TicketStatus.REJECTED, reason="denied"
        )
        run_svc.approval_repo = MagicMock()
        run_svc.approval_repo.get_ticket.return_value = rejected_ticket

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await poll_for_approval(repo, run_svc, "job_1", "ticket_1")

        assert result == JobStatus.FAILED
        run_svc.handle_job_failure.assert_awaited()

    @pytest.mark.asyncio
    async def test_expired_ticket_returns_failed(self):
        from control_plane.worker_executor import poll_for_approval

        repo = MagicMock()
        failed_job = _make_job(status=JobStatus.FAILED)
        repo.get_job.return_value = failed_job
        repo.list_runs_by_job.return_value = []

        run_svc = MagicMock()
        run_svc.approval_timeout_sec = 10
        run_svc.handle_job_failure = AsyncMock(return_value=failed_job)

        expired_ticket = _make_ticket(status=TicketStatus.EXPIRED)
        run_svc.approval_repo = MagicMock()
        run_svc.approval_repo.get_ticket.return_value = expired_ticket

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await poll_for_approval(repo, run_svc, "job_1", "ticket_1")

        assert result == JobStatus.FAILED

    @pytest.mark.asyncio
    async def test_timeout_returns_failed(self):
        from control_plane.worker_executor import poll_for_approval

        repo = MagicMock()
        failed_job = _make_job(status=JobStatus.FAILED)
        repo.get_job.return_value = failed_job
        repo.list_runs_by_job.return_value = []

        run_svc = MagicMock()
        # Very short timeout to trigger timeout quickly
        run_svc.approval_timeout_sec = 0
        run_svc.handle_job_failure = AsyncMock(return_value=failed_job)

        run_svc.approval_repo = MagicMock()
        run_svc.approval_repo.get_ticket.return_value = None  # never resolved

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await poll_for_approval(repo, run_svc, "job_1", "ticket_1")

        assert result == JobStatus.FAILED

    @pytest.mark.asyncio
    async def test_stop_event_returns_pending_approval(self):
        from control_plane.worker_executor import poll_for_approval

        repo = MagicMock()
        run_svc = MagicMock()
        run_svc.approval_timeout_sec = 600  # long timeout
        run_svc.approval_repo = MagicMock()
        run_svc.approval_repo.get_ticket.return_value = None

        stop_event = MagicMock()
        stop_event.is_set.return_value = True

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await poll_for_approval(
                repo, run_svc, "job_1", "ticket_1", stop_event=stop_event,
            )

        assert result == JobStatus.PENDING_APPROVAL

    @pytest.mark.asyncio
    async def test_pending_ticket_continues_polling(self):
        """A still-pending ticket should not cause early termination."""
        from control_plane.worker_executor import poll_for_approval

        repo = MagicMock()
        repo.get_job.return_value = _make_job(status=JobStatus.RUNNING)

        run_svc = MagicMock()
        run_svc.approval_timeout_sec = 20
        run_svc.approval_repo = MagicMock()

        pending_ticket = _make_ticket(status=TicketStatus.PENDING)
        approved_ticket = _make_ticket(status=TicketStatus.APPROVED)

        call_count = 0

        def fake_get_ticket(tid):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return pending_ticket
            return approved_ticket

        run_svc.approval_repo.get_ticket.side_effect = fake_get_ticket

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await poll_for_approval(repo, run_svc, "job_1", "ticket_1")

        assert result == JobStatus.RUNNING
        assert call_count >= 3  # polled multiple times before approval

    @pytest.mark.asyncio
    async def test_null_ticket_continues_polling(self):
        """get_ticket returning None (not found) should continue polling."""
        from control_plane.worker_executor import poll_for_approval

        repo = MagicMock()
        repo.get_job.return_value = _make_job(status=JobStatus.RUNNING)

        run_svc = MagicMock()
        run_svc.approval_timeout_sec = 20
        run_svc.approval_repo = MagicMock()

        call_count = 0

        def fake_get_ticket(tid):
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                return None
            return _make_ticket(status=TicketStatus.APPROVED)

        run_svc.approval_repo.get_ticket.side_effect = fake_get_ticket

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await poll_for_approval(repo, run_svc, "job_1", "ticket_1")

        assert result == JobStatus.RUNNING

    @pytest.mark.asyncio
    async def test_rejected_ticket_with_no_job_returns_failed(self):
        """If get_job returns None after rejection, should return FAILED."""
        from control_plane.worker_executor import poll_for_approval

        repo = MagicMock()
        # First call returns a job (for the rejection path),
        # second call returns None
        repo.get_job.side_effect = [None]
        repo.list_runs_by_job.return_value = []

        run_svc = MagicMock()
        run_svc.approval_timeout_sec = 10

        rejected_ticket = _make_ticket(status=TicketStatus.REJECTED, reason="no")
        run_svc.approval_repo = MagicMock()
        run_svc.approval_repo.get_ticket.return_value = rejected_ticket

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await poll_for_approval(repo, run_svc, "job_1", "ticket_1")

        assert result == JobStatus.FAILED

    @pytest.mark.asyncio
    async def test_expired_ticket_with_no_job_returns_failed(self):
        """If get_job returns None after expiry, should return FAILED."""
        from control_plane.worker_executor import poll_for_approval

        repo = MagicMock()
        repo.get_job.return_value = None
        repo.list_runs_by_job.return_value = []

        run_svc = MagicMock()
        run_svc.approval_timeout_sec = 10

        expired_ticket = _make_ticket(status=TicketStatus.EXPIRED)
        run_svc.approval_repo = MagicMock()
        run_svc.approval_repo.get_ticket.return_value = expired_ticket

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await poll_for_approval(repo, run_svc, "job_1", "ticket_1")

        assert result == JobStatus.FAILED

    @pytest.mark.asyncio
    async def test_timeout_with_no_job_returns_failed(self):
        """Timeout path where get_job returns None."""
        from control_plane.worker_executor import poll_for_approval

        repo = MagicMock()
        repo.get_job.return_value = None
        repo.list_runs_by_job.return_value = []

        run_svc = MagicMock()
        run_svc.approval_timeout_sec = 0
        run_svc.approval_repo = MagicMock()
        run_svc.approval_repo.get_ticket.return_value = None

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await poll_for_approval(repo, run_svc, "job_1", "ticket_1")

        assert result == JobStatus.FAILED
