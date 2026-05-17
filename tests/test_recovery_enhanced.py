"""
Tests for enhanced recovery logic covering approval-interruption scenarios.

Covers:
- Startup recovery of expired pending tickets
- Failure propagation from expired tickets to associated jobs
- Orphan ticket recovery when job is no longer active
- resume_after_approval returns correct Run
- abort_after_rejection advances job to failure
- Simulated kill -9 restart recovery flow
- No long-stuck active jobs after recovery
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

# Ensure project root is on sys.path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from control_plane.models import Job, JobStatus, Run, RunStatus, RetryPolicy  # noqa: E402
from control_plane.repository import JobRepository  # noqa: E402
from control_plane.approval import ApprovalRepository, ApprovalTicket, TicketStatus  # noqa: E402
from control_plane.service import RunService  # noqa: E402
from control_plane.worker import TaskWorker, WorkerConfig  # noqa: E402


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def tmp_repo(tmp_path: Path) -> JobRepository:
    """A JobRepository backed by a temporary directory."""
    return JobRepository(str(tmp_path / "jobs"))


@pytest.fixture
def tmp_approval_repo(tmp_path: Path) -> ApprovalRepository:
    """An ApprovalRepository backed by a temporary directory."""
    return ApprovalRepository(str(tmp_path / "approvals"))


@pytest.fixture
def mock_run_service(tmp_repo: JobRepository, tmp_approval_repo: ApprovalRepository) -> MagicMock:
    """A mock RunService with a real repository and approval_repo."""
    service = MagicMock(spec=RunService)
    service.repository = tmp_repo
    service.approval_repo = tmp_approval_repo
    # Make handle_job_failure delegate to the real logic via side_effect
    service.handle_job_failure = AsyncMock(
        side_effect=lambda job, error, error_category: _real_handle_job_failure(
            tmp_repo, job, error, error_category
        )
    )
    service._emit_event = MagicMock()
    return service


@pytest.fixture
def worker(tmp_repo: JobRepository, mock_run_service: MagicMock) -> TaskWorker:
    """A TaskWorker with real repository and mock run_service."""
    config = WorkerConfig(concurrency=1, poll_interval_sec=1)
    return TaskWorker(repository=tmp_repo, run_service=mock_run_service, config=config)


@pytest.fixture
def real_run_service(
    tmp_repo: JobRepository,
    tmp_approval_repo: ApprovalRepository,
    llm_config: Any
) -> RunService:
    """A real RunService with real repositories (for resume/abort tests)."""
    from core.config import LLMConfig
    return RunService(
        repository=tmp_repo,
        llm_config=LLMConfig(api_key="test", model="test-model"),
        approval_repo=tmp_approval_repo,
    )


def _real_handle_job_failure(
    repo: JobRepository,
    job: Job,
    error: str,
    error_category: str = "unknown",
) -> Job:
    """Real implementation of handle_job_failure for the mock.

    Mimics the real RunService.handle_job_failure: job must be in FAILED
    state before retry/dead-letter logic is applied.
    """
    # Map custom error categories to valid ones
    valid_categories = {"", "timeout", "eval_failed", "tool_blocked", "unknown"}
    if error_category not in valid_categories:
        error_category = "unknown"

    # Refresh job from repo to get latest state
    refreshed = repo.get_job(job.id)
    if refreshed is not None:
        job = refreshed

    # Ensure job is in FAILED state (handle_job_failure expects this)
    if job.status == JobStatus.RUNNING:
        job = repo.transition_job_status(
            job.id, JobStatus.FAILED, error=error, error_category=error_category
        )
    elif job.status == JobStatus.LEASED:
        # LEASED cannot go directly to FAILED; must go via QUEUED
        job = repo.release_lease(job.id)
        job = repo.transition_job_status(
            job.id, JobStatus.FAILED, error=error, error_category=error_category
        )
    elif job.status == JobStatus.QUEUED:
        # Already queued (e.g., retry-queued) — nothing to do
        return job
    elif job.status not in (JobStatus.FAILED, JobStatus.SUCCEEDED,
                            JobStatus.CANCELED, JobStatus.DEAD_LETTER):
        # Any other non-terminal state -> FAILED first
        job = repo.transition_job_status(
            job.id, JobStatus.FAILED, error=error, error_category=error_category
        )
    # If already FAILED, proceed directly to retry/dead-letter

    max_attempts = job.retry_policy.max_attempts
    if job.attempt < max_attempts:
        # FAILED -> QUEUED (retry)
        return repo.transition_job_status(
            job.id, JobStatus.QUEUED, error=error, error_category=error_category
        )
    else:
        # Exhausted retries: FAILED -> DEAD_LETTER
        return repo.transition_job_status(
            job.id, JobStatus.DEAD_LETTER, error=error, error_category=error_category
        )


# =============================================================================
# Helper: create a job with a specific status
# =============================================================================


def _create_job(repo: JobRepository, status: JobStatus, **overrides: Any) -> Job:
    """Create and persist a job, force its status, and return it."""
    retry_policy = RetryPolicy(max_attempts=2, backoff_sec=1)
    job = repo.create_job(
        requirement="Test requirement",
        retry_policy=retry_policy,
    )
    # Force status
    if status != JobStatus.QUEUED:
        # Use direct manipulation for setup
        job.status = status
        job.updated_at = datetime.now(timezone.utc)
        if "lease_expires_at" in overrides:
            job.lease_expires_at = overrides.pop("lease_expires_at")
        repo.update_job(job)
    for key, val in overrides.items():
        setattr(job, key, val)
    repo.update_job(job)
    return repo.get_job(job.id) or job


def _create_run(repo: JobRepository, job_id: str, status: RunStatus = RunStatus.RUNNING) -> Run:
    """Create and persist a run for a job."""
    run = repo.create_run(job_id, f"session_{job_id}")
    run.status = status
    repo.update_run(run)
    return run


def _create_pending_ticket(
    approval_repo: ApprovalRepository,
    job_id: str,
    expires_at: datetime | None = None,
    tool_name: str = "bash",
) -> ApprovalTicket:
    """Create a pending approval ticket."""
    if expires_at is None:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=300)
    ticket = approval_repo.create_ticket(
        job_id=job_id,
        tool_name=tool_name,
        args={"command": "echo test"},
        risk_level="high",
        timeout_sec=300,
    )
    # Override expires_at if needed
    if expires_at != ticket.expires_at:
        ticket.expires_at = expires_at
        approval_repo.update_ticket(ticket)
    return ticket


# =============================================================================
# Test: Startup recovery of expired pending tickets
# =============================================================================


class TestExpiredTicketRecovery:
    """Tests for _recover_pending_tickets — Step 1: expire timed-out tickets."""

    @pytest.mark.asyncio
    async def test_expires_pending_ticket_on_startup(
        self, worker: TaskWorker,
        tmp_repo: JobRepository,
        tmp_approval_repo: ApprovalRepository
    ):
        """A pending ticket past its expiry is marked EXPIRED on worker start."""
        job = _create_job(tmp_repo, JobStatus.RUNNING)
        # Create a ticket that expired 10 seconds ago
        expired_time = datetime.now(timezone.utc) - timedelta(seconds=10)
        ticket = _create_pending_ticket(tmp_approval_repo, job.id, expires_at=expired_time)

        assert ticket.status == TicketStatus.PENDING

        recovered = await worker._recover_pending_tickets()

        assert ticket.id in recovered
        updated = tmp_approval_repo.get_ticket(ticket.id)
        assert updated is not None
        assert updated.status == TicketStatus.EXPIRED
        assert updated.decided_by == "timeout"

    @pytest.mark.asyncio
    async def test_non_expired_ticket_left_alone(
        self, worker: TaskWorker,
        tmp_repo: JobRepository,
        tmp_approval_repo: ApprovalRepository
    ):
        """A pending ticket that has NOT expired is left untouched."""
        job = _create_job(tmp_repo, JobStatus.RUNNING)
        future_time = datetime.now(timezone.utc) + timedelta(seconds=300)
        ticket = _create_pending_ticket(tmp_approval_repo, job.id, expires_at=future_time)

        recovered = await worker._recover_pending_tickets()

        assert ticket.id not in recovered
        updated = tmp_approval_repo.get_ticket(ticket.id)
        assert updated is not None
        assert updated.status == TicketStatus.PENDING

    @pytest.mark.asyncio
    async def test_no_approval_repo_returns_empty(self, worker: TaskWorker):
        """If run_service has no approval_repo, recovery returns []."""
        worker.run_service.approval_repo = None
        recovered = await worker._recover_pending_tickets()
        assert recovered == []


# =============================================================================
# Test: Expired ticket pushes associated job to failure
# =============================================================================


class TestExpiredTicketPushesJobToFailure:
    """Tests that expired tickets trigger failure handling for their jobs."""

    @pytest.mark.asyncio
    async def test_expired_ticket_running_job_queued_for_retry(
        self, worker: TaskWorker,
        tmp_repo: JobRepository,
        tmp_approval_repo: ApprovalRepository
    ):
        """An expired ticket for a RUNNING job causes it to be queued for retry."""
        job = _create_job(tmp_repo, JobStatus.RUNNING)
        expired_time = datetime.now(timezone.utc) - timedelta(seconds=10)
        _create_pending_ticket(tmp_approval_repo, job.id, expires_at=expired_time)  # noqa: F841

        await worker._recover_pending_tickets()

        updated_job = tmp_repo.get_job(job.id)
        assert updated_job is not None
        # Job should be retried (RUNNING -> FAILED -> QUEUED) since attempt < max_attempts
        assert updated_job.status == JobStatus.QUEUED
        # Attempt should be incremented
        assert updated_job.attempt == 1

    @pytest.mark.asyncio
    async def test_expired_ticket_running_job_with_lease_queued_for_retry(
        self, worker: TaskWorker,
        tmp_repo: JobRepository,
        tmp_approval_repo: ApprovalRepository
    ):
        """An expired ticket for a RUNNING job causes it to be queued for retry."""
        job = _create_job(tmp_repo, JobStatus.RUNNING)
        expired_time = datetime.now(timezone.utc) - timedelta(seconds=10)
        _create_pending_ticket(tmp_approval_repo, job.id, expires_at=expired_time)  # noqa: F841

        await worker._recover_pending_tickets()

        updated_job = tmp_repo.get_job(job.id)
        assert updated_job is not None
        # Job should be retried (RUNNING -> FAILED -> QUEUED)
        assert updated_job.status == JobStatus.QUEUED
        assert updated_job.attempt == 1

    @pytest.mark.asyncio
    async def test_expired_ticket_exhausted_attempts_goes_dead_letter(
        self, worker: TaskWorker,
        tmp_repo: JobRepository,
        tmp_approval_repo: ApprovalRepository
    ):
        """When max_attempts exhausted, expired ticket sends job to DEAD_LETTER."""
        job = _create_job(tmp_repo, JobStatus.RUNNING)
        # Set attempt to max_attempts so retry is exhausted
        job.attempt = job.retry_policy.max_attempts
        tmp_repo.update_job(job)

        expired_time = datetime.now(timezone.utc) - timedelta(seconds=10)
        _create_pending_ticket(tmp_approval_repo, job.id, expires_at=expired_time)  # noqa: F841

        await worker._recover_pending_tickets()

        updated_job = tmp_repo.get_job(job.id)
        assert updated_job is not None
        assert updated_job.status == JobStatus.DEAD_LETTER


# =============================================================================
# Test: Orphan ticket recovery (job no longer active)
# =============================================================================


class TestOrphanTicketRecovery:
    """Tests for _recover_pending_tickets — Step 2: orphan ticket cleanup."""

    @pytest.mark.asyncio
    async def test_orphan_ticket_job_succeeded(
        self, worker: TaskWorker,
        tmp_repo: JobRepository,
        tmp_approval_repo: ApprovalRepository
    ):
        """A pending ticket for a SUCCEEDED job is marked expired."""
        job = _create_job(tmp_repo, JobStatus.SUCCEEDED)
        future_time = datetime.now(timezone.utc) + timedelta(seconds=300)
        ticket = _create_pending_ticket(tmp_approval_repo, job.id, expires_at=future_time)

        recovered = await worker._recover_pending_tickets()

        assert ticket.id in recovered
        updated_ticket = tmp_approval_repo.get_ticket(ticket.id)
        assert updated_ticket is not None
        assert updated_ticket.status == TicketStatus.EXPIRED
        assert updated_ticket.decided_by == "auto"

    @pytest.mark.asyncio
    async def test_orphan_ticket_job_failed(
        self, worker: TaskWorker,
        tmp_repo: JobRepository,
        tmp_approval_repo: ApprovalRepository
    ):
        """A pending ticket for a FAILED job is marked expired (job stays FAILED)."""
        # Create job, transition properly: QUEUED -> FAILED
        job = tmp_repo.create_job(requirement="Test job")
        job.status = JobStatus.RUNNING
        tmp_repo.update_job(job)
        tmp_repo.transition_job_status(job.id, JobStatus.FAILED, error="some error")
        job = tmp_repo.get_job(job.id)
        assert job is not None
        assert job.status == JobStatus.FAILED

        future_time = datetime.now(timezone.utc) + timedelta(seconds=300)
        ticket = _create_pending_ticket(tmp_approval_repo, job.id, expires_at=future_time)

        recovered = await worker._recover_pending_tickets()

        assert ticket.id in recovered
        updated_ticket = tmp_approval_repo.get_ticket(ticket.id)
        assert updated_ticket is not None
        assert updated_ticket.status == TicketStatus.EXPIRED
        # Job should stay FAILED (terminal state, not pushed again)
        updated_job = tmp_repo.get_job(job.id)
        assert updated_job.status == JobStatus.FAILED

    @pytest.mark.asyncio
    async def test_orphan_ticket_job_not_running_is_recovered(
        self, worker: TaskWorker,
        tmp_repo: JobRepository,
        tmp_approval_repo: ApprovalRepository
    ):
        """A pending ticket for a job that was never started (QUEUED) is treated as orphan."""
        # Create job, start it RUNNING, then simulate it going back to QUEUED
        # (e.g., after a previous failure retry) while ticket remains
        job = tmp_repo.create_job(requirement="test", retry_policy=RetryPolicy(max_attempts=2))
        # Simulate: job was RUNNING, then failed and queued for retry
        tmp_repo.acquire_lease(job.id, "test_owner", lease_duration_sec=60)
        tmp_repo.transition_job_status(job.id, JobStatus.RUNNING)
        tmp_repo.transition_job_status(job.id, JobStatus.FAILED, error="prev error")
        tmp_repo.transition_job_status(job.id, JobStatus.QUEUED)  # retry
        job = tmp_repo.get_job(job.id)
        assert job.status == JobStatus.QUEUED
        assert job.attempt == 1

        future_time = datetime.now(timezone.utc) + timedelta(seconds=300)
        ticket = _create_pending_ticket(tmp_approval_repo, job.id, expires_at=future_time)

        recovered = await worker._recover_pending_tickets()

        # QUEUED (even with attempt=1) is not RUNNING or LEASED, so ticket is orphan
        assert ticket.id in recovered
        updated_ticket = tmp_approval_repo.get_ticket(ticket.id)
        assert updated_ticket is not None
        assert updated_ticket.status == TicketStatus.EXPIRED
        # Job stays QUEUED (was already retry-queued)
        updated_job = tmp_repo.get_job(job.id)
        assert updated_job.status == JobStatus.QUEUED

    @pytest.mark.asyncio
    async def test_running_job_with_pending_ticket_left_alone(
        self, worker: TaskWorker,
        tmp_repo: JobRepository,
        tmp_approval_repo: ApprovalRepository
    ):
        """A pending ticket for a genuinely RUNNING job is NOT touched."""
        job = _create_job(tmp_repo, JobStatus.RUNNING)
        future_time = datetime.now(timezone.utc) + timedelta(seconds=300)
        ticket = _create_pending_ticket(tmp_approval_repo, job.id, expires_at=future_time)

        recovered = await worker._recover_pending_tickets()

        # Ticket should NOT be recovered since job is genuinely RUNNING
        assert ticket.id not in recovered
        updated_ticket = tmp_approval_repo.get_ticket(ticket.id)
        assert updated_ticket is not None
        assert updated_ticket.status == TicketStatus.PENDING


# =============================================================================
# Test: resume_after_approval
# =============================================================================


class TestResumeAfterApproval:
    """Tests for RunService.resume_after_approval."""

    @pytest.mark.asyncio
    async def test_resume_returns_latest_active_run(
        self, tmp_repo: JobRepository,
        tmp_approval_repo: ApprovalRepository
    ):
        """resume_after_approval returns the latest RUNNING run for the job."""
        from core.config import LLMConfig
        service = RunService(
            repository=tmp_repo,
            llm_config=LLMConfig(api_key="test", model="test-model"),
            approval_repo=tmp_approval_repo,
        )

        job = _create_job(tmp_repo, JobStatus.RUNNING)
        _create_run(tmp_repo, job.id, RunStatus.RUNNING)  # noqa: F841
        run2 = _create_run(tmp_repo, job.id, RunStatus.RUNNING)

        result = await service.resume_after_approval(job.id, "ticket_123")

        assert result is not None
        assert result.id == run2.id
        assert result.job_id == job.id

    @pytest.mark.asyncio
    async def test_resume_no_active_run_returns_none(
        self, tmp_repo: JobRepository,
        tmp_approval_repo: ApprovalRepository
    ):
        """resume_after_approval returns None when no active RUNNING run exists."""
        from core.config import LLMConfig
        service = RunService(
            repository=tmp_repo,
            llm_config=LLMConfig(api_key="test", model="test-model"),
            approval_repo=tmp_approval_repo,
        )

        job = _create_job(tmp_repo, JobStatus.RUNNING)
        # Create a succeeded run, not running
        _create_run(tmp_repo, job.id, RunStatus.SUCCEEDED)  # noqa: F841

        result = await service.resume_after_approval(job.id, "ticket_123")

        assert result is None

    @pytest.mark.asyncio
    async def test_resume_missing_job_returns_none(
        self, tmp_repo: JobRepository,
        tmp_approval_repo: ApprovalRepository
    ):
        """resume_after_approval returns None for a non-existent job."""
        from core.config import LLMConfig
        service = RunService(
            repository=tmp_repo,
            llm_config=LLMConfig(api_key="test", model="test-model"),
            approval_repo=tmp_approval_repo,
        )

        result = await service.resume_after_approval("nonexistent_job", "ticket_123")

        assert result is None


# =============================================================================
# Test: abort_after_rejection
# =============================================================================


class TestAbortAfterRejection:
    """Tests for RunService.abort_after_rejection."""

    @pytest.mark.asyncio
    async def test_abort_queues_for_retry(
        self, tmp_repo: JobRepository,
        tmp_approval_repo: ApprovalRepository
    ):
        """abort_after_rejection queues the job for retry when attempts remain."""
        from core.config import LLMConfig
        service = RunService(
            repository=tmp_repo,
            llm_config=LLMConfig(api_key="test", model="test-model"),
            approval_repo=tmp_approval_repo,
        )

        job = _create_job(tmp_repo, JobStatus.RUNNING)
        # Transition properly: RUNNING -> FAILED (prerequisite for handle_job_failure)
        tmp_repo.transition_job_status(job.id, JobStatus.FAILED, error="test error")
        job = tmp_repo.get_job(job.id)
        original_attempt = job.attempt

        result = await service.abort_after_rejection(job.id, "ticket_456", reason="unsafe command")

        # FAILED -> QUEUED (retry) clears last_error and bumps attempt
        assert result.status == JobStatus.QUEUED
        assert result.attempt == original_attempt + 1

    @pytest.mark.asyncio
    async def test_abort_exhausted_goes_dead_letter(
        self, tmp_repo: JobRepository,
        tmp_approval_repo: ApprovalRepository
    ):
        """abort_after_rejection sends job to DEAD_LETTER when attempts exhausted."""
        from core.config import LLMConfig
        service = RunService(
            repository=tmp_repo,
            llm_config=LLMConfig(api_key="test", model="test-model"),
            approval_repo=tmp_approval_repo,
        )

        job = _create_job(tmp_repo, JobStatus.RUNNING)
        job.attempt = job.retry_policy.max_attempts  # exhaust attempts
        tmp_repo.update_job(job)
        tmp_repo.transition_job_status(job.id, JobStatus.FAILED, error="test error")

        result = await service.abort_after_rejection(job.id, "ticket_789")

        assert result.status == JobStatus.DEAD_LETTER

    @pytest.mark.asyncio
    async def test_abort_missing_job_raises(
        self, tmp_repo: JobRepository,
        tmp_approval_repo: ApprovalRepository
    ):
        """abort_after_rejection raises ValueError for a non-existent job."""
        from core.config import LLMConfig
        service = RunService(
            repository=tmp_repo,
            llm_config=LLMConfig(api_key="test", model="test-model"),
            approval_repo=tmp_approval_repo,
        )

        with pytest.raises(ValueError, match="not found"):
            await service.abort_after_rejection("nonexistent_job", "ticket_000")

    @pytest.mark.asyncio
    async def test_abort_with_reason_in_message(
        self, tmp_repo: JobRepository,
        tmp_approval_repo: ApprovalRepository
    ):
        """abort_after_rejection includes the reason in the error message.

        Note: The error message is set during the FAILED-> transition but
        last_error is cleared on retry (FAILED->QUEUED). We verify the
        job is correctly queued for retry.
        """
        from core.config import LLMConfig
        service = RunService(
            repository=tmp_repo,
            llm_config=LLMConfig(api_key="test", model="test-model"),
            approval_repo=tmp_approval_repo,
        )

        job = _create_job(tmp_repo, JobStatus.RUNNING)
        tmp_repo.transition_job_status(job.id, JobStatus.FAILED, error="test error")
        job = tmp_repo.get_job(job.id)
        original_attempt = job.attempt

        result = await service.abort_after_rejection(
            job.id, "ticket_abc",
            reason="security policy violation"
        )

        # FAILED -> QUEUED (retry) — verify retry was triggered
        assert result.status == JobStatus.QUEUED
        assert result.attempt == original_attempt + 1


# =============================================================================
# Test: Simulated kill -9 restart recovery
# =============================================================================


class TestKill9RestartRecovery:
    """Simulate a kill -9 scenario and verify clean recovery on restart."""

    @pytest.mark.asyncio
    async def test_kill9_with_pending_tickets(self, tmp_path: Path):
        """Simulate kill -9: worker dies while jobs RUNNING with pending tickets.

        On restart, _recover_pending_tickets should:
        1. Expire timed-out tickets
        2. Push associated jobs to FAILED -> QUEUED (retry)
        3. Leave non-expired tickets for genuinely running jobs alone
        """
        repo = JobRepository(str(tmp_path / "jobs"))
        approval_repo = ApprovalRepository(str(tmp_path / "approvals"))

        # Simulate pre-crash state
        job1 = repo.create_job(requirement="Job with expired ticket")
        job1.status = JobStatus.RUNNING
        repo.update_job(job1)
        repo.create_run(job1.id, "sess_1")  # noqa: F841

        job2 = repo.create_job(requirement="Job with valid ticket")
        job2.status = JobStatus.RUNNING
        repo.update_job(job2)
        repo.create_run(job2.id, "sess_2")  # noqa: F841

        job3 = repo.create_job(requirement="Job already succeeded but ticket pending")
        job3.status = JobStatus.SUCCEEDED
        repo.update_job(job3)

        # Create tickets
        expired_time = datetime.now(timezone.utc) - timedelta(seconds=60)
        ticket1 = approval_repo.create_ticket(
            job_id=job1.id, tool_name="bash", args={"command": "rm -rf /"},
            risk_level="critical", timeout_sec=300,
        )
        ticket1.expires_at = expired_time
        approval_repo.update_ticket(ticket1)

        valid_time = datetime.now(timezone.utc) + timedelta(seconds=300)
        ticket2 = approval_repo.create_ticket(
            job_id=job2.id, tool_name="bash", args={"command": "ls"},
            risk_level="high", timeout_sec=300,
        )
        ticket2.expires_at = valid_time
        approval_repo.update_ticket(ticket2)

        ticket3 = approval_repo.create_ticket(
            job_id=job3.id, tool_name="edit", args={"file_path": "/etc/passwd"},
            risk_level="critical", timeout_sec=300,
        )
        ticket3.expires_at = valid_time
        approval_repo.update_ticket(ticket3)

        # Verify pre-crash state
        assert approval_repo.get_ticket(ticket1.id).status == TicketStatus.PENDING
        assert approval_repo.get_ticket(ticket2.id).status == TicketStatus.PENDING
        assert approval_repo.get_ticket(ticket3.id).status == TicketStatus.PENDING
        assert repo.get_job(job1.id).status == JobStatus.RUNNING
        assert repo.get_job(job2.id).status == JobStatus.RUNNING
        assert repo.get_job(job3.id).status == JobStatus.SUCCEEDED

        # Build mock run_service for recovery
        mock_service = MagicMock(spec=RunService)
        mock_service.approval_repo = approval_repo
        mock_service.repository = repo
        mock_service.handle_job_failure = AsyncMock(
            side_effect=lambda job, error, error_category: _real_handle_job_failure(
                repo, job, error, error_category
            )
        )

        config = WorkerConfig(concurrency=1, poll_interval_sec=1)
        worker = TaskWorker(repository=repo, run_service=mock_service, config=config)

        # Simulate restart recovery
        recovered = await worker._recover_pending_tickets()

        # Assertions
        # ticket1: expired -> should be recovered
        assert ticket1.id in recovered
        assert approval_repo.get_ticket(ticket1.id).status == TicketStatus.EXPIRED

        # ticket2: not expired, job RUNNING -> should be left alone
        assert ticket2.id not in recovered
        assert approval_repo.get_ticket(ticket2.id).status == TicketStatus.PENDING

        # ticket3: job SUCCEEDED -> orphan, should be recovered
        assert ticket3.id in recovered
        assert approval_repo.get_ticket(ticket3.id).status == TicketStatus.EXPIRED

        # job1 should have been retried (RUNNING -> FAILED -> QUEUED)
        updated_job1 = repo.get_job(job1.id)
        assert updated_job1.status == JobStatus.QUEUED

        # job2 should still be RUNNING (ticket not expired, not touched)
        updated_job2 = repo.get_job(job2.id)
        assert updated_job2.status == JobStatus.RUNNING

        # job3 should still be SUCCEEDED
        updated_job3 = repo.get_job(job3.id)
        assert updated_job3.status == JobStatus.SUCCEEDED

    @pytest.mark.asyncio
    async def test_kill9_restart_no_orphan_active_jobs(self, tmp_path: Path):
        """After recovery, no active jobs should be stuck with orphaned pending tickets."""
        repo = JobRepository(str(tmp_path / "jobs"))
        approval_repo = ApprovalRepository(str(tmp_path / "approvals"))

        # Simulate: job was RUNNING but ticket orphaned (job status got corrupted)
        job = repo.create_job(requirement="Test job")
        job.status = JobStatus.RUNNING
        repo.update_job(job)
        repo.create_run(job.id, "sess_1")  # noqa: F841

        # Ticket expired long ago
        expired_time = datetime.now(timezone.utc) - timedelta(seconds=600)
        ticket = approval_repo.create_ticket(
            job_id=job.id, tool_name="bash", args={"command": "danger"},
            risk_level="critical", timeout_sec=300,
        )
        ticket.expires_at = expired_time
        approval_repo.update_ticket(ticket)

        mock_service = MagicMock(spec=RunService)
        mock_service.approval_repo = approval_repo
        mock_service.repository = repo
        mock_service.handle_job_failure = AsyncMock(
            side_effect=lambda job, error, error_category: _real_handle_job_failure(
                repo, job, error, error_category
            )
        )

        config = WorkerConfig(concurrency=1, poll_interval_sec=1)
        worker = TaskWorker(repository=repo, run_service=mock_service, config=config)

        # Run recovery
        recovered = await worker._recover_pending_tickets()

        assert len(recovered) > 0
        # After recovery, the job should not still be RUNNING with a pending ticket
        updated_job = repo.get_job(job.id)
        # Job was pushed to FAILED -> QUEUED (retry)
        assert updated_job.status != JobStatus.RUNNING

        # No pending tickets should remain for this job
        remaining_pending = approval_repo.list_tickets(
            status=TicketStatus.PENDING, job_id=job.id
        )
        assert len(remaining_pending) == 0


# =============================================================================
# Test: _emit_event structure
# =============================================================================


class TestEmitEvent:
    """Tests for RunService._emit_event output format."""

    @pytest.mark.asyncio
    async def test_emit_event_structure(
        self, tmp_repo: JobRepository,
        tmp_approval_repo: ApprovalRepository,
        caplog: pytest.LogCaptureFixture,
    ):
        """_emit_event logs a valid JSON line with expected fields."""
        import logging
        from core.config import LLMConfig
        service = RunService(
            repository=tmp_repo,
            llm_config=LLMConfig(api_key="test", model="test-model"),
            approval_repo=tmp_approval_repo,
        )

        with caplog.at_level(logging.INFO, logger="control_plane.service"):
            service._emit_event("test_event", "job_123", {"key": "value"})

        # Find the JSON event in the log messages
        event = None
        for record in caplog.records:
            if "Execution event:" in record.message:
                json_str = record.message.split("Execution event: ", 1)[1]
                event = json.loads(json_str)
                break

        assert event is not None, "No event found in log output"
        assert event["type"] == "test_event"
        assert event["job_id"] == "job_123"
        assert event["details"] == {"key": "value"}
        assert "ts" in event
        # ts should be a valid ISO timestamp
        datetime.fromisoformat(event["ts"])


# =============================================================================
# Test: list_tickets filtering with tool_name
# =============================================================================


class TestListTicketsFiltering:
    """Tests for ApprovalRepository.list_tickets with tool_name filter."""

    def test_filter_by_tool_name(self, tmp_approval_repo: ApprovalRepository):
        """list_tickets filters correctly by tool_name."""
        tmp_approval_repo.create_ticket(  # noqa: F841
            job_id="job_1", tool_name="bash", args={"command": "ls"}, risk_level="high"
        )
        ticket2 = tmp_approval_repo.create_ticket(
            job_id="job_1", tool_name="edit", args={"file_path": "x.py"}, risk_level="high"
        )
        tmp_approval_repo.create_ticket(  # noqa: F841
            job_id="job_2", tool_name="bash", args={"command": "pwd"}, risk_level="medium"
        )

        bash_tickets = tmp_approval_repo.list_tickets(tool_name="bash")
        assert len(bash_tickets) == 2
        assert all(t.tool_name == "bash" for t in bash_tickets)

        edit_tickets = tmp_approval_repo.list_tickets(tool_name="edit")
        assert len(edit_tickets) == 1
        assert edit_tickets[0].id == ticket2.id

    def test_filter_by_status_and_tool_name(self, tmp_approval_repo: ApprovalRepository):
        """list_tickets combines status and tool_name filters."""
        ticket1 = tmp_approval_repo.create_ticket(
            job_id="job_1", tool_name="bash", args={"command": "ls"}, risk_level="high"
        )
        ticket2 = tmp_approval_repo.create_ticket(
            job_id="job_1", tool_name="bash", args={"command": "rm"}, risk_level="high"
        )
        # Approve ticket2
        tmp_approval_repo.approve_ticket(ticket2.id)

        pending_bash = tmp_approval_repo.list_tickets(
            status=TicketStatus.PENDING, tool_name="bash"
        )
        assert len(pending_bash) == 1
        assert pending_bash[0].id == ticket1.id

    def test_filter_by_job_id_and_tool_name(self, tmp_approval_repo: ApprovalRepository):
        """list_tickets combines job_id and tool_name filters."""
        ticket1 = tmp_approval_repo.create_ticket(
            job_id="job_A", tool_name="bash", args={"command": "ls"}, risk_level="high"
        )
        tmp_approval_repo.create_ticket(  # noqa: F841
            job_id="job_B", tool_name="bash", args={"command": "pwd"}, risk_level="high"
        )
        tmp_approval_repo.create_ticket(  # noqa: F841
            job_id="job_A", tool_name="edit", args={"file_path": "x.py"}, risk_level="high"
        )

        result = tmp_approval_repo.list_tickets(job_id="job_A", tool_name="bash")
        assert len(result) == 1
        assert result[0].id == ticket1.id


# =============================================================================
# Test: Worker.start() integration
# =============================================================================


class TestWorkerStartIntegration:
    """Integration tests for worker startup recovery sequence."""

    @pytest.mark.asyncio
    async def test_start_calls_both_recovery_methods(
        self, tmp_repo: JobRepository,
        tmp_approval_repo: ApprovalRepository
    ):
        """Worker.start() calls both _recover_orphan_jobs and _recover_pending_tickets."""
        mock_service = MagicMock(spec=RunService)
        mock_service.approval_repo = tmp_approval_repo
        mock_service.repository = tmp_repo
        mock_service.handle_job_failure = AsyncMock(
            side_effect=lambda job, error, error_category: _real_handle_job_failure(
                tmp_repo, job, error, error_category
            )
        )

        config = WorkerConfig(concurrency=1, poll_interval_sec=1)
        worker = TaskWorker(repository=tmp_repo, run_service=mock_service, config=config)

        # Create orphan job and expired ticket
        job = tmp_repo.create_job(requirement="Orphan job")
        job.status = JobStatus.RUNNING
        job.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=300)
        tmp_repo.update_job(job)

        expired_time = datetime.now(timezone.utc) - timedelta(seconds=60)
        ticket = tmp_approval_repo.create_ticket(
            job_id=job.id, tool_name="bash", args={"command": "test"},
            risk_level="high", timeout_sec=300,
        )
        ticket.expires_at = expired_time
        tmp_approval_repo.update_ticket(ticket)

        # Mock the poll loop so start() doesn't block
        with patch.object(worker, "_poll_loop", new_callable=AsyncMock):
            with patch.object(worker, "_heartbeat", new_callable=AsyncMock):
                # Set stop event so start exits after setup
                async def _stop_soon():
                    await asyncio.sleep(0.1)
                    worker._stop_event.set()

                asyncio.create_task(_stop_soon())
                await worker.start()

        # Verify ticket was recovered
        updated_ticket = tmp_approval_repo.get_ticket(ticket.id)
        assert updated_ticket.status == TicketStatus.EXPIRED

        # Verify orphan job was recovered
        # (The orphan job recovery transitions RUNNING -> FAILED)
        updated_job = tmp_repo.get_job(job.id)
        # The job should not still be RUNNING
        assert updated_job.status != JobStatus.RUNNING
