"""
Tests for Worker / RunService boundary — P0-1 from issue #132.

Verifies that Worker does not double-push terminal states
(RUNNING -> SUCCEEDED) that RunService has already handled.
"""
import asyncio
import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, AsyncMock, patch

from control_plane.worker import TaskWorker, WorkerConfig
from control_plane.models import Job, JobStatus, RunStatus
from control_plane.repository import JobRepository
from control_plane.service import RunService


@pytest.fixture
def tmp_repo(tmp_path):
    return JobRepository(base_path=str(tmp_path / "data"))


@pytest.fixture
def mock_run_service(tmp_repo):
    svc = MagicMock(spec=RunService)
    svc.approval_repo = None
    svc.approval_timeout_sec = 300
    svc.run_job = AsyncMock()
    svc.handle_job_failure = AsyncMock()
    return svc


@pytest.fixture
def worker(tmp_repo, mock_run_service):
    return TaskWorker(
        repository=tmp_repo,
        run_service=mock_run_service,
        config=WorkerConfig(concurrency=1),
    )


def _make_leased_job(repo: JobRepository) -> Job:
    """Create a job and advance it to LEASED (ready for _execute_job_core)."""
    job = repo.create_job("test requirement")
    repo.acquire_lease(job.id, "test-owner", 60)
    return job


class TestWorkerNoDoubleSucceeded:
    def test_worker_does_not_push_succeeded_on_success(
        self, worker, tmp_repo, mock_run_service
    ):
        """Worker must not call transition_job_status(SUCCEEDED) when
        RunService.run_job() already did so."""
        job = _make_leased_job(tmp_repo)

        async def fake_run_job(job_id):
            # Simulate RunService transitioning to SUCCEEDED internally
            tmp_repo.transition_job_status(job_id, JobStatus.SUCCEEDED)
            run = MagicMock()
            run.status = RunStatus.SUCCEEDED
            return run

        mock_run_service.run_job.side_effect = fake_run_job

        original_transition = tmp_repo.transition_job_status
        transition_calls: list[tuple[str, JobStatus]] = []

        def tracking_transition(job_id, status, **kwargs):
            transition_calls.append((job_id, status))
            return original_transition(job_id, status, **kwargs)

        with patch.object(tmp_repo, "transition_job_status", side_effect=tracking_transition):
            asyncio.run(worker._execute_job_core(job.id))

        succeeded_calls = [c for c in transition_calls if c[1] == JobStatus.SUCCEEDED]
        assert len(succeeded_calls) == 1, (
            f"Expected exactly 1 SUCCEEDED transition (from RunService), "
            f"got {len(succeeded_calls)}: {succeeded_calls}"
        )


class TestWorkerPostApprovalNoDoubleSucceeded:
    def test_post_approval_no_double_succeeded(
        self, worker, tmp_repo, mock_run_service
    ):
        """Post-approval re-execution must not double-push SUCCEEDED."""
        from control_plane.approval import ApprovalTicket, TicketStatus
        from core.exceptions import PendingApprovalError

        job = _make_leased_job(tmp_repo)

        # Set up approval repo with a pre-approved ticket
        approval_repo = MagicMock()
        mock_run_service.approval_repo = approval_repo

        now = datetime.now(timezone.utc)
        ticket = ApprovalTicket(
            id="ticket-1",
            job_id=job.id,
            tool_name="bash",
            args_hash="abc123",
            args_preview="rm -rf /tmp/test",
            risk_level="high",
            status=TicketStatus.APPROVED,
            decided_by="test",
            requested_at=now,
            created_at=now,
            updated_at=now,
        )
        approval_repo.get_ticket.return_value = ticket

        # run_job: first call raises PendingApprovalError, second succeeds
        call_count = [0]

        async def fake_run_job(job_id):
            call_count[0] += 1
            if call_count[0] == 1:
                raise PendingApprovalError(
                    ticket_id="ticket-1", guardrail_result=MagicMock()
                )
            # Second call — RunService transitions to SUCCEEDED
            tmp_repo.transition_job_status(job_id, JobStatus.SUCCEEDED)
            run = MagicMock()
            run.status = RunStatus.SUCCEEDED
            return run

        mock_run_service.run_job.side_effect = fake_run_job

        # Track SUCCEEDED transitions
        original_transition = tmp_repo.transition_job_status
        succeeded_count = [0]

        def tracking_transition(jid, status, **kwargs):
            if status == JobStatus.SUCCEEDED:
                succeeded_count[0] += 1
            return original_transition(jid, status, **kwargs)

        with patch.object(
            tmp_repo, "transition_job_status", side_effect=tracking_transition
        ):
            asyncio.run(worker._execute_job_core(job.id))

        assert succeeded_count[0] == 1, (
            f"Expected 1 SUCCEEDED transition (from RunService), "
            f"got {succeeded_count[0]}"
        )


class TestWorkerFailurePathNoDoublePush:
    def test_failure_path_respects_runservice_final_state(
        self, worker, tmp_repo, mock_run_service
    ):
        """When RunService already re-queued a failed job, Worker must not
        try to transition it again."""
        job = _make_leased_job(tmp_repo)

        async def fake_run_job(job_id):
            # RunService transitions RUNNING -> FAILED -> QUEUED (retry)
            tmp_repo.transition_job_status(job_id, JobStatus.FAILED)
            tmp_repo.transition_job_status(job_id, JobStatus.QUEUED)
            run = MagicMock()
            run.status = RunStatus.FAILED
            return run

        mock_run_service.run_job.side_effect = fake_run_job
        mock_run_service.handle_job_failure = AsyncMock()

        # Should complete without raising — Worker reads the final QUEUED state.
        asyncio.run(worker._execute_job_core(job.id))

        final_job = tmp_repo.get_job(job.id)
        assert final_job is not None
        assert final_job.status == JobStatus.QUEUED
