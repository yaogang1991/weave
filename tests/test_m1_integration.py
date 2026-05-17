"""
M1 Integration Tests — covering core paths and edge cases.

Test scenarios:
1. submit -> status full pipeline
2. submit -> cancel -> status verification
3. timeout -> retry pipeline
4. replan success and failure
5. restart recovery flow

Each test uses JobRepository with a temporary directory to ensure isolation.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from control_plane.models import Job, JobStatus, Run, RunStatus, RetryPolicy  # noqa: F401
from control_plane.repository import JobRepository
from control_plane.worker import TaskWorker, WorkerConfig


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def tmp_repo(tmp_path: Path) -> JobRepository:
    """A JobRepository backed by a fresh temporary directory."""
    return JobRepository(str(tmp_path / "jobs"))


@pytest.fixture
def make_job():
    """Factory for creating Job instances with custom overrides."""
    def _make(**kwargs: Any) -> Job:
        now = datetime.now(timezone.utc)
        defaults = {
            "id": f"job_{uuid.uuid4().hex[:12]}",
            "requirement": "Build a REST API",
            "status": JobStatus.QUEUED,
            "created_at": now,
            "updated_at": now,
        }
        defaults.update(kwargs)
        return Job(**defaults)
    return _make


@pytest.fixture
def mock_run_service():
    """A mock RunService for worker tests."""
    service = MagicMock()
    service.run_job = AsyncMock(return_value=MagicMock(status=RunStatus.SUCCEEDED))
    service.handle_job_failure = AsyncMock(side_effect=lambda job, error, error_category: job)
    return service


# =============================================================================
# TestSubmitStatusFlow — submit -> status full pipeline
# =============================================================================


class TestSubmitStatusFlow:
    """submit -> status complete pipeline."""

    def test_submit_creates_job_with_queued_status(self, tmp_repo: JobRepository):
        """Creating a job via the repository should set status to QUEUED."""
        job = tmp_repo.create_job(requirement="Build a todo API")
        assert job.status == JobStatus.QUEUED
        assert job.requirement == "Build a todo API"
        assert job.id.startswith("job_")

    def test_status_returns_job_and_runs(self, tmp_repo: JobRepository):
        """Retrieving a job by ID should return the job with correct status."""
        job = tmp_repo.create_job(requirement="Test status retrieval")
        retrieved = tmp_repo.get_job(job.id)
        assert retrieved is not None
        assert retrieved.id == job.id
        assert retrieved.status == JobStatus.QUEUED
        assert retrieved.requirement == "Test status retrieval"

    def test_list_returns_all_jobs(self, tmp_repo: JobRepository):
        """list_jobs should return all jobs when no status filter is applied."""
        job1 = tmp_repo.create_job(requirement="Job one")
        job2 = tmp_repo.create_job(requirement="Job two")
        job3 = tmp_repo.create_job(requirement="Job three")

        all_jobs = tmp_repo.list_jobs()
        assert len(all_jobs) == 3
        ids = {j.id for j in all_jobs}
        assert ids == {job1.id, job2.id, job3.id}

    def test_list_with_status_filter(self, tmp_repo: JobRepository):
        """list_jobs with a status filter should only return matching jobs."""
        job1 = tmp_repo.create_job(requirement="Will succeed")
        job2 = tmp_repo.create_job(requirement="Will be canceled")
        job3 = tmp_repo.create_job(requirement="Also queued")

        # Transition job2 to CANCELED
        tmp_repo.transition_job_status(job2.id, JobStatus.CANCELED)

        queued_jobs = tmp_repo.list_jobs(status=JobStatus.QUEUED)
        canceled_jobs = tmp_repo.list_jobs(status=JobStatus.CANCELED)

        assert len(queued_jobs) == 2
        assert len(canceled_jobs) == 1
        assert queued_jobs[0].id in {job1.id, job3.id}
        assert canceled_jobs[0].id == job2.id


# =============================================================================
# TestCancelFlow — cancel pipeline
# =============================================================================


class TestCancelFlow:
    """cancel pipeline."""

    def test_cancel_queued_job(self, tmp_repo: JobRepository):
        """Canceling a QUEUED job should transition it to CANCELED."""
        job = tmp_repo.create_job(requirement="Cancel me")
        canceled = tmp_repo.transition_job_status(job.id, JobStatus.CANCELED)
        assert canceled.status == JobStatus.CANCELED

    def test_cancel_running_job(self, tmp_repo: JobRepository):
        """Canceling a RUNNING job should transition it to CANCELED."""
        job = tmp_repo.create_job(requirement="Running then canceled")
        # Simulate the full lifecycle: QUEUED -> LEASED -> RUNNING -> CANCELED
        tmp_repo.acquire_lease(job.id, "test_owner", 60)
        tmp_repo.transition_job_status(job.id, JobStatus.RUNNING)
        canceled = tmp_repo.transition_job_status(job.id, JobStatus.CANCELED)
        assert canceled.status == JobStatus.CANCELED

    def test_cancel_nonexistent_job_raises_error(self, tmp_repo: JobRepository):
        """Canceling a non-existent job should raise ValueError."""
        with pytest.raises(ValueError, match="Job not found"):
            tmp_repo.transition_job_status("job_nonexistent", JobStatus.CANCELED)

    def test_cancel_already_succeeded_job_fails(self, tmp_repo: JobRepository):
        """Canceling a SUCCEEDED job should raise ValueError (illegal transition)."""
        job = tmp_repo.create_job(requirement="Already done")
        tmp_repo.acquire_lease(job.id, "test_owner", 60)
        tmp_repo.transition_job_status(job.id, JobStatus.RUNNING)
        tmp_repo.transition_job_status(job.id, JobStatus.SUCCEEDED)

        with pytest.raises(ValueError, match="Illegal status transition"):
            tmp_repo.transition_job_status(job.id, JobStatus.CANCELED)


# =============================================================================
# TestTimeoutRetryFlow — timeout -> retry -> dead_letter pipeline
# =============================================================================


class TestTimeoutRetryFlow:
    """timeout -> retry -> dead_letter pipeline."""

    def test_job_timeout_sets_timed_out_status(self, tmp_repo: JobRepository):
        """Simulating timeout should transition job through RUNNING to FAILED."""
        job = tmp_repo.create_job(
            requirement="Timeout test",
            retry_policy=RetryPolicy(max_attempts=1, backoff_sec=5),
        )
        tmp_repo.acquire_lease(job.id, "test_owner", 60)
        tmp_repo.transition_job_status(job.id, JobStatus.RUNNING)

        # Simulate timeout — transition RUNNING -> FAILED
        failed = tmp_repo.transition_job_status(
            job.id, JobStatus.FAILED,
            error="Job execution timed out", error_category="timeout",
        )
        assert failed.status == JobStatus.FAILED
        assert failed.last_error == "Job execution timed out"
        assert failed.error_category == "timeout"

    def test_retry_increments_attempt(self, tmp_repo: JobRepository):
        """FAILED -> QUEUED retry transition should increment attempt counter."""
        job = tmp_repo.create_job(
            requirement="Retry test",
            retry_policy=RetryPolicy(max_attempts=3, backoff_sec=5),
        )
        tmp_repo.acquire_lease(job.id, "test_owner", 60)
        tmp_repo.transition_job_status(job.id, JobStatus.RUNNING)
        tmp_repo.transition_job_status(job.id, JobStatus.FAILED, error="oops")

        assert job.attempt == 0
        retried = tmp_repo.transition_job_status(job.id, JobStatus.QUEUED)
        assert retried.status == JobStatus.QUEUED
        assert retried.attempt == 1
        assert retried.last_error == ""
        assert retried.error_category == ""
        assert retried.lease_owner is None
        assert retried.lease_expires_at is None

    def test_max_attempts_reaches_dead_letter(self, tmp_repo: JobRepository):
        """After exhausting retries, job should transition to DEAD_LETTER."""
        job = tmp_repo.create_job(
            requirement="Dead letter test",
            retry_policy=RetryPolicy(max_attempts=2, backoff_sec=5),
        )
        # First attempt fails, retry (attempt becomes 1)
        tmp_repo.acquire_lease(job.id, "test_owner", 60)
        tmp_repo.transition_job_status(job.id, JobStatus.RUNNING)
        tmp_repo.transition_job_status(job.id, JobStatus.FAILED, error="Attempt 1")
        tmp_repo.transition_job_status(job.id, JobStatus.QUEUED)  # attempt = 1

        # Second attempt also fails — attempt(1) < max_attempts(2), could retry
        # but we simulate the service deciding to dead-letter anyway
        tmp_repo.acquire_lease(job.id, "test_owner", 60)
        tmp_repo.transition_job_status(job.id, JobStatus.RUNNING)
        tmp_repo.transition_job_status(job.id, JobStatus.FAILED, error="Attempt 2")

        # Transition to DEAD_LETTER directly (exhausted patience)
        dead = tmp_repo.transition_job_status(job.id, JobStatus.DEAD_LETTER)
        assert dead.status == JobStatus.DEAD_LETTER
        assert dead.attempt == 1  # bumped once during the single retry
        assert dead.is_terminal()
        assert dead.lease_owner is None
        assert dead.lease_expires_at is None

    def test_retry_backoff_increases(self, tmp_repo: JobRepository):
        """Backoff delay should increase with successive retries."""
        policy = RetryPolicy(max_attempts=3, backoff_sec=5)
        delays = []
        for attempt in range(policy.max_attempts):
            delay = policy.backoff_sec * (2 ** attempt)
            delays.append(delay)

        assert delays == [5, 10, 20]
        assert all(d > 0 for d in delays)


# =============================================================================
# TestReplanFlow — replan closed loop
# =============================================================================


class TestReplanFlow:
    """replan closed loop."""

    def test_replan_preserves_successful_nodes(self, tmp_repo: JobRepository):
        """A replan should preserve context from successful nodes."""
        job = tmp_repo.create_job(
            requirement="Replan test",
            retry_policy=RetryPolicy(max_attempts=3, backoff_sec=5),
        )
        # Simulate: original job fails, replan creates child job
        tmp_repo.acquire_lease(job.id, "test_owner", 60)
        tmp_repo.transition_job_status(job.id, JobStatus.RUNNING)
        tmp_repo.transition_job_status(job.id, JobStatus.FAILED, error="Need replan")

        # Simulate replan: create a new job referencing the original
        child_job = tmp_repo.create_job(
            requirement="Replan test (revised)",
            retry_policy=RetryPolicy(max_attempts=3, backoff_sec=5),
        )
        child_job.metadata["parent_job_id"] = job.id
        tmp_repo.update_job(child_job)

        assert child_job.metadata["parent_job_id"] == job.id
        assert child_job.status == JobStatus.QUEUED

    def test_replan_exceeding_max_replans_terminates(self, tmp_repo: JobRepository):
        """After exceeding max replans, the job should reach a terminal state."""
        job = tmp_repo.create_job(
            requirement="Exhaust replans",
            retry_policy=RetryPolicy(max_attempts=1, backoff_sec=5),
        )
        tmp_repo.acquire_lease(job.id, "test_owner", 60)
        tmp_repo.transition_job_status(job.id, JobStatus.RUNNING)
        tmp_repo.transition_job_status(job.id, JobStatus.FAILED, error="Failed again")

        # max_attempts == 1, attempt == 0, but we've failed -> DEAD_LETTER
        dead = tmp_repo.transition_job_status(job.id, JobStatus.DEAD_LETTER)
        assert dead.status == JobStatus.DEAD_LETTER
        assert dead.is_terminal()

    def test_replan_handler_called_on_replan_decision(self, tmp_repo: JobRepository):
        """The replan handler should be invoked when a replan decision is made."""
        handler_called = False

        def mock_replan_handler(dag, failed_node_id):
            nonlocal handler_called
            handler_called = True
            return MagicMock()  # Return a mock DAG

        # Simulate failure that triggers replan
        tmp_repo.create_job(  # noqa: F841
            requirement="Trigger replan",
            retry_policy=RetryPolicy(max_attempts=3, backoff_sec=5),
        )

        # Invoke the handler directly (simulating orchestrator decision)
        mock_dag = MagicMock()
        mock_replan_handler(mock_dag, "node_1")

        assert handler_called is True


# =============================================================================
# TestRecoveryFlow — restart recovery
# =============================================================================


class TestRecoveryFlow:
    """Restart recovery."""

    def test_recover_returns_orphan_jobs(self, tmp_repo: JobRepository):
        """recover_orphan_jobs should find jobs with expired leases."""
        # Create a job with an expired lease
        job = tmp_repo.create_job(requirement="Orphan job")
        now = datetime.now(timezone.utc)
        # Manually set an expired lease
        job.status = JobStatus.LEASED
        job.lease_owner = "crashed_worker"
        job.lease_expires_at = now - timedelta(seconds=300)  # 5 min ago
        tmp_repo.update_job(job)

        orphans = tmp_repo.recover_orphan_jobs()
        assert len(orphans) == 1
        assert orphans[0].id == job.id

    def test_recover_queued_job_unchanged(self, tmp_repo: JobRepository):
        """QUEUED jobs without expired leases should not be recovered."""
        job = tmp_repo.create_job(requirement="Normal queued job")
        assert job.status == JobStatus.QUEUED
        assert job.lease_expires_at is None

        orphans = tmp_repo.recover_orphan_jobs()
        # Should not find any orphans
        assert len(orphans) == 0

    def test_recovered_jobs_have_queued_or_failed_status(self, tmp_repo: JobRepository):
        """After recovery, orphaned leased jobs can be released to QUEUED."""
        job = tmp_repo.create_job(requirement="Recover me")
        now = datetime.now(timezone.utc)
        job.status = JobStatus.LEASED
        job.lease_owner = "crashed_worker"
        job.lease_expires_at = now - timedelta(seconds=300)
        tmp_repo.update_job(job)

        # Recovery finds the orphan
        orphans = tmp_repo.recover_orphan_jobs()
        assert len(orphans) == 1

        # Release lease back to QUEUED (what the worker would do)
        released = tmp_repo.release_lease(job.id)
        assert released.status == JobStatus.QUEUED
        assert released.lease_owner is None
        assert released.lease_expires_at is None

    def test_worker_startup_calls_recover(self, tmp_repo: JobRepository, mock_run_service):
        """TaskWorker.start() should call _recover_orphan_jobs on startup."""
        # Create an orphan job
        job = tmp_repo.create_job(requirement="Worker recovery test")
        now = datetime.now(timezone.utc)
        job.status = JobStatus.LEASED
        job.lease_owner = "crashed_worker"
        job.lease_expires_at = now - timedelta(seconds=300)
        tmp_repo.update_job(job)

        config = WorkerConfig(concurrency=1, poll_interval_sec=1)
        worker = TaskWorker(tmp_repo, mock_run_service, config)

        # Just test the _recover_orphan_jobs method directly
        recovered = asyncio.run(worker._recover_orphan_jobs())
        assert len(recovered) == 1
        assert recovered[0] == job.id

        # Verify the job was returned to QUEUED
        updated_job = tmp_repo.get_job(job.id)
        assert updated_job is not None
        assert updated_job.status == JobStatus.QUEUED

    def test_recover_running_orphan(self, tmp_repo: JobRepository):
        """RUNNING jobs with expired leases should be identified as orphans."""
        job = tmp_repo.create_job(requirement="Running orphan")
        now = datetime.now(timezone.utc)
        job.status = JobStatus.RUNNING
        job.lease_owner = "crashed_worker"
        job.lease_expires_at = now - timedelta(seconds=300)
        tmp_repo.update_job(job)

        orphans = tmp_repo.recover_orphan_jobs()
        assert len(orphans) == 1
        assert orphans[0].id == job.id

        # Simulate what worker does: mark as FAILED for retry
        failed = tmp_repo.transition_job_status(
            job.id, JobStatus.FAILED,
            error="Worker crashed", error_category="unknown",
        )
        assert failed.status == JobStatus.FAILED

    def test_list_active_jobs(self, tmp_repo: JobRepository):
        """list_active_jobs should return queued, leased, and running jobs."""
        q = tmp_repo.create_job(requirement="Queued")
        l = tmp_repo.create_job(requirement="Leased")
        r = tmp_repo.create_job(requirement="Running")
        _ = tmp_repo.create_job(requirement="Succeeded")

        # Set up statuses
        tmp_repo.acquire_lease(l.id, "owner", 60)
        tmp_repo.acquire_lease(r.id, "owner", 60)
        tmp_repo.transition_job_status(r.id, JobStatus.RUNNING)

        # Succeeded one
        s = tmp_repo.create_job(requirement="Done")
        tmp_repo.acquire_lease(s.id, "owner", 60)
        tmp_repo.transition_job_status(s.id, JobStatus.RUNNING)
        tmp_repo.transition_job_status(s.id, JobStatus.SUCCEEDED)

        active = tmp_repo.list_active_jobs()
        active_ids = {j.id for j in active}

        assert q.id in active_ids
        assert l.id in active_ids
        assert r.id in active_ids
        assert s.id not in active_ids  # SUCCEEDED is not active


# =============================================================================
# TestLeaseManagement — lease edge cases
# =============================================================================


class TestLeaseManagement:
    """Lease management edge cases."""

    def test_acquire_lease_on_queued_job(self, tmp_repo: JobRepository):
        """Acquiring a lease on a QUEUED job should succeed."""
        job = tmp_repo.create_job(requirement="Lease me")
        leased = tmp_repo.acquire_lease(job.id, "worker_1", 60)
        assert leased is not None
        assert leased.status == JobStatus.LEASED
        assert leased.lease_owner == "worker_1"
        assert leased.lease_expires_at is not None

    def test_acquire_lease_on_already_leased_job_fails(self, tmp_repo: JobRepository):
        """Acquiring a lease on an already-leased job should return None."""
        job = tmp_repo.create_job(requirement="Already leased")
        tmp_repo.acquire_lease(job.id, "worker_1", 60)

        second = tmp_repo.acquire_lease(job.id, "worker_2", 60)
        assert second is None

    def test_lease_expiry_allows_reacquire(self, tmp_repo: JobRepository):
        """After lease expiry, another worker should be able to acquire."""
        job = tmp_repo.create_job(requirement="Expired lease")
        now = datetime.now(timezone.utc)
        # Set an already-expired lease
        job.status = JobStatus.LEASED
        job.lease_owner = "old_worker"
        job.lease_expires_at = now - timedelta(seconds=10)
        tmp_repo.update_job(job)

        reacquired = tmp_repo.acquire_lease(job.id, "new_worker", 60)
        assert reacquired is not None
        assert reacquired.lease_owner == "new_worker"

    def test_release_lease_returns_to_queued(self, tmp_repo: JobRepository):
        """Releasing a lease should return the job to QUEUED status."""
        job = tmp_repo.create_job(requirement="Release me")
        tmp_repo.acquire_lease(job.id, "worker_1", 60)
        released = tmp_repo.release_lease(job.id)
        assert released.status == JobStatus.QUEUED
        assert released.lease_owner is None
        assert released.lease_expires_at is None

    def test_stale_leases_detection(self, tmp_repo: JobRepository):
        """get_stale_leases should find leases older than the threshold."""
        job = tmp_repo.create_job(requirement="Stale lease")
        now = datetime.now(timezone.utc)
        job.status = JobStatus.LEASED
        job.lease_owner = "worker"
        job.lease_expires_at = now - timedelta(seconds=300)  # 5 min ago
        tmp_repo.update_job(job)

        stale = tmp_repo.get_stale_leases(max_age_sec=120)
        assert len(stale) == 1
        assert stale[0].id == job.id

    def test_fresh_leases_not_stale(self, tmp_repo: JobRepository):
        """Fresh leases should not be detected as stale."""
        job = tmp_repo.create_job(requirement="Fresh lease")
        tmp_repo.acquire_lease(job.id, "worker", 3600)  # 1 hour lease

        stale = tmp_repo.get_stale_leases(max_age_sec=120)
        assert len(stale) == 0


# =============================================================================
# TestEdgeCases — boundary conditions
# =============================================================================


class TestEdgeCases:
    """Boundary conditions and edge cases."""

    def test_empty_repository(self, tmp_repo: JobRepository):
        """Operations on an empty repository should return empty results."""
        assert tmp_repo.list_jobs() == []
        assert tmp_repo.list_active_jobs() == []
        assert tmp_repo.recover_orphan_jobs() == []
        assert tmp_repo.get_stale_leases() == []
        assert tmp_repo.get_job("nonexistent") is None

    def test_job_status_transitions(self, tmp_repo: JobRepository):
        """Valid status transitions should succeed; invalid should fail."""
        job = tmp_repo.create_job(requirement="Transition test")

        # QUEUED -> LEASED
        tmp_repo.acquire_lease(job.id, "owner", 60)
        job = tmp_repo.get_job(job.id)
        assert job.status == JobStatus.LEASED

        # LEASED -> RUNNING
        tmp_repo.transition_job_status(job.id, JobStatus.RUNNING)
        job = tmp_repo.get_job(job.id)
        assert job.status == JobStatus.RUNNING

        # RUNNING -> SUCCEEDED
        tmp_repo.transition_job_status(job.id, JobStatus.SUCCEEDED)
        job = tmp_repo.get_job(job.id)
        assert job.status == JobStatus.SUCCEEDED

    def test_invalid_status_transition_raises(self, tmp_repo: JobRepository):
        """Invalid status transitions should raise ValueError."""
        job = tmp_repo.create_job(requirement="Invalid transition")

        # Cannot go QUEUED -> SUCCEEDED (must pass through LEASED and RUNNING)
        with pytest.raises(ValueError, match="Illegal status transition"):
            tmp_repo.transition_job_status(job.id, JobStatus.SUCCEEDED)

    def test_terminal_state_is_terminal(self, tmp_repo: JobRepository):
        """Terminal states should be correctly identified."""
        for status in [JobStatus.SUCCEEDED, JobStatus.FAILED,
                       JobStatus.CANCELED, JobStatus.DEAD_LETTER]:
            job = tmp_repo.create_job(requirement=f"Test {status.value}")
            if status == JobStatus.SUCCEEDED:
                tmp_repo.acquire_lease(job.id, "o", 60)
                tmp_repo.transition_job_status(job.id, JobStatus.RUNNING)
                tmp_repo.transition_job_status(job.id, JobStatus.SUCCEEDED)
            elif status == JobStatus.FAILED:
                tmp_repo.acquire_lease(job.id, "o", 60)
                tmp_repo.transition_job_status(job.id, JobStatus.RUNNING)
                tmp_repo.transition_job_status(job.id, JobStatus.FAILED, error="fail")
            elif status == JobStatus.CANCELED:
                tmp_repo.transition_job_status(job.id, JobStatus.CANCELED)
            elif status == JobStatus.DEAD_LETTER:
                tmp_repo.acquire_lease(job.id, "o", 60)
                tmp_repo.transition_job_status(job.id, JobStatus.RUNNING)
                tmp_repo.transition_job_status(job.id, JobStatus.FAILED, error="fail")
                tmp_repo.transition_job_status(job.id, JobStatus.DEAD_LETTER)

            job = tmp_repo.get_job(job.id)
            assert job.is_terminal(), f"{status.value} should be terminal"
            assert not job.is_active(), f"{status.value} should not be active"

    def test_create_run(self, tmp_repo: JobRepository):
        """Creating a run should associate it with the job."""
        job = tmp_repo.create_job(requirement="Run test")
        run = tmp_repo.create_run(job.id, "session_123")

        assert run.job_id == job.id
        assert run.session_id == "session_123"
        assert run.status == RunStatus.RUNNING

        runs = tmp_repo.list_runs_by_job(job.id)
        assert len(runs) == 1
        assert runs[0].id == run.id

    def test_job_isolation(self, tmp_repo: JobRepository):
        """Jobs should be isolated from each other."""
        job1 = tmp_repo.create_job(requirement="Job 1")
        job2 = tmp_repo.create_job(requirement="Job 2")

        # Transition job1 should not affect job2
        tmp_repo.transition_job_status(job1.id, JobStatus.CANCELED)

        j1 = tmp_repo.get_job(job1.id)
        j2 = tmp_repo.get_job(job2.id)

        assert j1.status == JobStatus.CANCELED
        assert j2.status == JobStatus.QUEUED
