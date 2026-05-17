"""
JobLifecycleManager — job failure handling, approval flow, and status queries.

Extracted from RunService as part of #177 PR6.
Behavior-preserving extraction: all logic is identical, just relocated
for testability and separation of concerns.

Handles:
- Job failure classification and retry/dead-letter decisions
- Approval resume/abort flows
- Job status queries and listing
"""
from __future__ import annotations

import logging
from typing import Any

from control_plane.models import Job, Run, JobStatus, RunStatus
from control_plane.repository import JobRepository

logger = logging.getLogger(__name__)


class JobLifecycleManager:
    """Manages job lifecycle transitions: failure, retry, approval, cancellation.

    Extracted from RunService (#177 PR6).
    """

    def __init__(
        self,
        repository: JobRepository,
        emit_event: Any,
        running_tasks: dict,
    ) -> None:
        self.repository = repository
        self._emit_event = emit_event
        self._running_tasks = running_tasks

    async def get_job_status(self, job_id: str) -> dict[str, Any]:
        """
        Return a comprehensive status dict for *job_id*.

        Includes the job fields plus a ``runs`` list with all
        execution attempts for this job.
        """
        job = self.repository.get_job(job_id)
        if job is None:
            raise ValueError(f"Job not found: {job_id}")

        runs = self.repository.list_runs_by_job(job_id)
        return {
            "job_id": job.id,
            "status": job.status.value,
            "attempt": job.attempt,
            "last_error": job.last_error,
            "error_category": job.error_category,
            "created_at": job.created_at.isoformat(),
            "updated_at": job.updated_at.isoformat(),
            "requirement": job.requirement,
            "project_path": job.project_path,
            "runs": [
                {
                    "run_id": r.id,
                    "status": r.status.value,
                    "session_id": r.session_id,
                    "dag_result": r.dag_result,
                    "started_at": r.started_at.isoformat() if r.started_at else None,
                    "completed_at": r.completed_at.isoformat() if r.completed_at else None,
                }
                for r in runs
            ],
        }

    async def list_jobs(self, status: JobStatus | None = None) -> list[Job]:
        """Return all jobs, optionally filtered by status."""
        return self.repository.list_jobs(status=status)

    async def cancel_job(self, job_id: str) -> Job:
        """
        Cancel a job if it is in a cancellable state.

        Only jobs in QUEUED, LEASED, or RUNNING can be cancelled.
        Raises ValueError if the transition is illegal (e.g. already terminal).
        """
        job = self.repository.get_job(job_id)
        if job is None:
            raise ValueError(f"Job not found: {job_id}")

        if not job.is_active():
            raise ValueError(
                f"Cannot cancel job {job_id}: already in terminal state {job.status.value}"
            )

        return self.repository.transition_job_status(job_id, JobStatus.CANCELED)

    async def handle_job_failure(
        self,
        job: Job,
        error: str,
        error_category: str = "unknown",
    ) -> Job:
        """
        Handle a failed job by either queuing for retry or sending to dead-letter.

        - If ``attempt < max_attempts``: transition to QUEUED, increment attempt,
          clear previous error state.
        - Otherwise: transition to DEAD_LETTER.
        - For ``rate_limit`` errors: re-queue WITHOUT consuming a retry attempt.
          429 errors are quota exhaustion, not implementation failures (#351).

        Args:
            job: The failed Job instance.
            error: Error message to record.
            error_category: Canonical error category
                            (timeout / eval_failed / tool_blocked /
                             rate_limit / watchdog / unknown).
        """
        max_attempts = job.retry_policy.max_attempts

        # Rate limit: re-queue WITHOUT consuming retry budget (#351).
        # 429 errors are transient quota exhaustion, not failures.
        if error_category == "rate_limit":
            logger.warning(
                "Rate limit hit for job %s — re-queuing without "
                "consuming retry (attempt %d/%d remains): %s",
                job.id, job.attempt, max_attempts,
                error[:200],
            )
            return self.repository.transition_job_status(
                job.id, JobStatus.QUEUED,
                error=error,
                error_category=error_category,
                skip_attempt_bump=True,
            )

        if job.attempt < max_attempts:
            # Retry: FAILED -> QUEUED (bump_attempt happens in repository)
            return self.repository.transition_job_status(
                job.id, JobStatus.QUEUED,
                error=error,
                error_category=error_category,
            )
        else:
            # Exhausted retries: FAILED -> DEAD_LETTER
            return self.repository.transition_job_status(
                job.id, JobStatus.DEAD_LETTER,
                error=error,
                error_category=error_category,
            )

    async def resume_after_approval(self, job_id: str, ticket_id: str) -> Run | None:
        """Resume a job after an approval decision.

        For PENDING_APPROVAL jobs: the Worker's poll loop detects the approval
        and resumes execution itself — no action needed here.

        For legacy RUNNING/LEASED jobs: re-queue for workers.
        """
        job = self.repository.get_job(job_id)
        if not job:
            return None

        # PENDING_APPROVAL jobs: the Worker's poll loop detects approved tickets
        # and resumes execution. Do NOT re-queue — that races with the poll loop.
        # If no worker is active, orphan recovery will handle it on next startup.
        if job.status == JobStatus.PENDING_APPROVAL:
            self._emit_event("approval_resumed_poll", job_id, {
                "ticket_id": ticket_id,
                "job_id": job_id,
                "message": "Worker poll loop will detect approval and resume",
            })
            runs = self.repository.list_runs_by_job(job_id)
            active_runs = [r for r in runs if r.status in {RunStatus.RUNNING, RunStatus.PENDING_APPROVAL}]
            return active_runs[-1] if active_runs else None

        # Legacy path for RUNNING/LEASED jobs
        runs = self.repository.list_runs_by_job(job_id)
        active_runs = [r for r in runs if r.status == RunStatus.RUNNING]
        if not active_runs:
            return None

        run = active_runs[-1]

        if job.status in {JobStatus.RUNNING, JobStatus.LEASED}:
            job.status = JobStatus.QUEUED
            job.lease_owner = None
            job.lease_expires_at = None
            job.last_error = ""
            job.error_category = ""
            job = self.repository.update_job(job)
        elif job.status != JobStatus.QUEUED:
            return None

        self._emit_event("approval_resumed", job_id, {
            "ticket_id": ticket_id,
            "run_id": run.id,
            "job_id": job_id,
            "job_status": job.status.value,
        })

        return run

    async def abort_after_rejection(self, job_id: str, ticket_id: str, reason: str = "") -> Job:
        """
        审批被拒绝后中止任务。

        将 job 状态推进到 failed 或 dead_letter（根据重试策略）。
        """
        job = self.repository.get_job(job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")

        error_msg = f"Approval ticket {ticket_id} rejected"
        if reason:
            error_msg += f": {reason}"

        if job.status == JobStatus.RUNNING:
            # Stop in-flight execution first, then mark job canceled.
            running_task = self._running_tasks.get(job.id)
            if running_task and not running_task.done():
                running_task.cancel()
            job = self.repository.transition_job_status(
                job.id,
                JobStatus.CANCELED,
                error=error_msg,
                error_category="tool_blocked",
            )
        elif job.status == JobStatus.LEASED:
            # LEASED cannot transition directly to FAILED in repository rules.
            job = self.repository.transition_job_status(
                job.id,
                JobStatus.QUEUED,
                error=error_msg,
                error_category="tool_blocked",
            )
            # Clear lease metadata so workers can immediately acquire this queued job.
            job.lease_owner = None
            job.lease_expires_at = None
            job = self.repository.update_job(job)
        elif job.status == JobStatus.QUEUED:
            job = self.repository.transition_job_status(
                job.id,
                JobStatus.FAILED,
                error=error_msg,
                error_category="tool_blocked",
            )

        if job.status == JobStatus.FAILED:
            job = await self.handle_job_failure(job, error_msg, "tool_blocked")

        self._emit_event("approval_rejected_abort", job_id, {
            "ticket_id": ticket_id,
            "job_status": job.status.value,
            "reason": reason,
        })

        return job
