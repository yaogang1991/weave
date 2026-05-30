"""Worker job execution and approval polling.

Extracted from TaskWorker for maintainability (#442).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

import asyncio
import json
import sys
from datetime import datetime, timezone
from typing import Any

from control_plane.models import JobStatus, RunStatus
from control_plane.approval import TicketStatus
from core.exceptions import PendingApprovalError


def _json_log(
    level: str,
    message: str,
    job_id: str = "",
    status: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    """Emit a single structured JSON line to stderr."""
    entry: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "message": message,
    }
    if job_id:
        entry["job_id"] = job_id
    if status:
        entry["status"] = status
    if extra:
        entry.update(extra)
    log_line = json.dumps(entry, ensure_ascii=False, default=str)
    print(log_line, file=sys.stderr, flush=True)


def finalize_pending_approval_run(
    repository: Any,
    job_id: str,
    run_final_status: str,
    detail_msg: str,
) -> None:
    """Finalize any run stuck in PENDING_APPROVAL status."""
    try:
        target_status = RunStatus(run_final_status)
    except ValueError:
        target_status = RunStatus.FAILED
    try:
        runs = repository.list_runs_by_job(job_id)
        for run in runs:
            if run.status == RunStatus.PENDING_APPROVAL:
                run.status = target_status
                run.completed_at = datetime.now(timezone.utc)
                run.dag_result = {"error": detail_msg}
                repository.update_run(run)
    except Exception as exc:
        _json_log(
            "WARNING",
            f"Failed to finalize pending-approval run: {exc}",
            job_id=job_id,
        )


async def handle_failure(
    repository: Any,
    run_service: Any,
    job_id: str,
    error: str,
    error_category: str,
) -> None:
    """Handle a job failure by delegating to RunService.handle_job_failure."""
    try:
        job = await asyncio.to_thread(repository.get_job, job_id)
        if job is None:
            _json_log("ERROR", "Job not found during failure handling", job_id=job_id)
            return

        updated_job = await run_service.handle_job_failure(
            job, error, error_category
        )
        _json_log(
            "INFO",
            f"Failure handled — job now {updated_job.status.value}",
            job_id=job_id,
            status=updated_job.status.value,
        )
    except Exception as exc:
        _json_log(
            "ERROR",
            f"Failure handling itself failed: {exc}",
            job_id=job_id,
        )
        try:
            await asyncio.to_thread(
                repository.transition_job_status,
                job_id,
                JobStatus.FAILED,
                error=error,
                error_category=error_category,
            )
        except Exception as exc2:
            _json_log(
                "CRITICAL",
                f"Could not transition job to FAILED: {exc2}",
                job_id=job_id,
            )


async def execute_job_core(
    repository: Any,
    run_service: Any,
    job_id: str,
    non_interactive: bool,
) -> None:
    """Core job execution — runs inside the concurrency semaphore.

    Steps:
        1. Transition LEASED -> RUNNING
        2. Call RunService.run_job
        3. On success -> SUCCEEDED
        4. On failure -> raise to outer handler
    """
    _json_log("INFO", "Starting job execution", job_id=job_id, status="running")

    try:
        # 1. Non-interactive: expire old approval tickets before execution
        if non_interactive:
            approval_repo = getattr(run_service, "approval_repo", None)
            if approval_repo is not None:
                approval_repo.expire_tickets()

        # 2. Transition to RUNNING.
        await asyncio.to_thread(
            repository.transition_job_status,
            job_id,
            JobStatus.RUNNING,
        )
        _json_log(
            "INFO",
            "Job transitioned to running",
            job_id=job_id,
            status=JobStatus.RUNNING.value,
        )

        # 3. Execute the job via RunService.
        run = await run_service.run_job(job_id)

        # 3. Handle result.
        if run.status == RunStatus.SUCCEEDED:
            current_job = await asyncio.to_thread(
                repository.get_job, job_id,
            )
            _json_log(
                "INFO",
                (
                    f"Job finished with status "
                    f"{current_job.status.value if current_job else 'unknown'}"
                ),
                job_id=job_id,
                status=current_job.status.value if current_job else "unknown",
            )
        else:
            current_job = await asyncio.to_thread(
                repository.get_job, job_id
            )
            if current_job is None:
                raise RuntimeError("Job disappeared during execution")
            if current_job.status == JobStatus.QUEUED:
                _json_log(
                    "INFO",
                    f"Job re-queued by run_job (attempt {current_job.attempt})",
                    job_id=job_id,
                    status=JobStatus.QUEUED.value,
                )
            elif current_job.status == JobStatus.DEAD_LETTER:
                _json_log(
                    "WARNING",
                    "Job moved to dead_letter by run_job",
                    job_id=job_id,
                    status=JobStatus.DEAD_LETTER.value,
                )
            else:
                raise RuntimeError(
                    f"Run ended with status {run.status.value}, "
                    f"job status is {current_job.status.value}"
                )

    except asyncio.CancelledError:
        _json_log(
            "WARNING",
            "Job execution cancelled (worker shutting down)",
            job_id=job_id,
        )
        try:
            await asyncio.to_thread(
                repository.release_lease, job_id
            )
        except Exception as e:
            logger.warning("Lease release failed: %s", e)
            raise

    except PendingApprovalError as exc:
        # Agent hit a high-risk tool — pause and wait for approval.
        await asyncio.to_thread(
            repository.transition_job_status,
            job_id, JobStatus.PENDING_APPROVAL,
        )
        _json_log(
            "INFO",
            f"Job paused for approval (ticket: {exc.ticket_id})",
            job_id=job_id,
            status=JobStatus.PENDING_APPROVAL.value,
            extra={"ticket_id": exc.ticket_id},
        )

        # Poll for approval decision
        final_status = await poll_for_approval(
            repository, run_service, job_id, exc.ticket_id,
        )

        if final_status == JobStatus.RUNNING:
            _json_log(
                "INFO",
                "Approval granted, resuming job",
                job_id=job_id,
                status=JobStatus.RUNNING.value,
            )
            finalize_pending_approval_run(
                repository, job_id, "failed", "Superseded by post-approval re-execution",
            )
            # Loop to handle multi-approval
            while True:
                try:
                    run = await run_service.run_job(job_id)
                    if run.status == RunStatus.SUCCEEDED:
                        current_job = await asyncio.to_thread(
                            repository.get_job, job_id,
                        )
                        _json_log(
                            "INFO", "Job completed after approval",
                            job_id=job_id,
                            status=current_job.status.value if current_job else "unknown",
                        )
                    else:
                        raise RuntimeError(
                            f"Run ended with status {run.status.value}"
                        )
                    break
                except PendingApprovalError as pa_exc:
                    _json_log(
                        "INFO",
                        (
                            f"Post-approval execution requires another approval "
                            f"(ticket: {pa_exc.ticket_id})"
                        ),
                        job_id=job_id,
                        status=JobStatus.PENDING_APPROVAL.value,
                        extra={"ticket_id": pa_exc.ticket_id},
                    )
                    finalize_pending_approval_run(
                        repository, job_id, "failed", "Superseded by next approval cycle",
                    )
                    await asyncio.to_thread(
                        repository.transition_job_status,
                        job_id, JobStatus.PENDING_APPROVAL,
                    )
                    final_status = await poll_for_approval(
                        repository, run_service, job_id, pa_exc.ticket_id,
                    )
                    if final_status != JobStatus.RUNNING:
                        break
                    finalize_pending_approval_run(
                        repository, job_id, "failed", "Superseded by next approval cycle",
                    )
                except Exception:
                    raise


async def poll_for_approval(
    repository: Any,
    run_service: Any,
    job_id: str,
    ticket_id: str,
    stop_event: Any = None,
) -> JobStatus:
    """Poll for an approval decision on a pending ticket.

    Returns:
        JobStatus.RUNNING if approved.
        JobStatus.FAILED/CANCELED/DEAD_LETTER if rejected/expired.
    """
    approval_repo = getattr(run_service, "approval_repo", None)
    if approval_repo is None:
        await handle_failure(
            repository, run_service, job_id,
            "No approval repository configured", "tool_blocked",
        )
        return JobStatus.FAILED

    poll_interval = 5  # seconds
    timeout = run_service.approval_timeout_sec
    elapsed = 0

    while True:
        should_stop = stop_event is not None and stop_event.is_set()
        if should_stop or elapsed >= timeout:
            break

        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

        ticket = await asyncio.to_thread(approval_repo.get_ticket, ticket_id)
        if ticket is None:
            continue

        if ticket.status == TicketStatus.APPROVED:
            job = await asyncio.to_thread(repository.get_job, job_id)
            if job and job.status == JobStatus.CANCELED:
                _json_log(
                    "INFO",
                    "Job was canceled while awaiting approval",
                    job_id=job_id,
                    status=JobStatus.CANCELED.value,
                )
                finalize_pending_approval_run(
                    repository, job_id, "aborted", "Job canceled during approval wait",
                )
                return JobStatus.CANCELED
            await asyncio.to_thread(
                repository.transition_job_status,
                job_id, JobStatus.RUNNING,
            )
            return JobStatus.RUNNING

        if ticket.status == TicketStatus.REJECTED:
            error_msg = f"Approval rejected for ticket {ticket_id}: {ticket.reason}"
            finalize_pending_approval_run(repository, job_id, "failed", error_msg)
            await asyncio.to_thread(
                repository.transition_job_status,
                job_id, JobStatus.FAILED,
                error=error_msg,
                error_category="tool_blocked",
            )
            job = await asyncio.to_thread(repository.get_job, job_id)
            if job:
                await run_service.handle_job_failure(job, error_msg, "tool_blocked")
                job = await asyncio.to_thread(repository.get_job, job_id)
            _json_log("WARNING", f"Approval rejected: {ticket.reason}", job_id=job_id)
            return job.status if job else JobStatus.FAILED

        if ticket.status == TicketStatus.EXPIRED:
            error_msg = f"Approval ticket {ticket_id} expired"
            finalize_pending_approval_run(repository, job_id, "failed", error_msg)
            await asyncio.to_thread(
                repository.transition_job_status,
                job_id, JobStatus.FAILED,
                error=error_msg,
                error_category="approval_timeout",
            )
            job = await asyncio.to_thread(repository.get_job, job_id)
            if job:
                await run_service.handle_job_failure(job, error_msg, "approval_timeout")
                job = await asyncio.to_thread(repository.get_job, job_id)
            _json_log("WARNING", f"Approval ticket expired: {ticket_id}", job_id=job_id)
            return job.status if job else JobStatus.FAILED

    # Loop exited — either timeout or worker shutting down.
    if stop_event is not None and stop_event.is_set():
        _json_log(
            "INFO",
            "Worker shutting down during approval poll — preserving PENDING_APPROVAL",
            job_id=job_id,
            status=JobStatus.PENDING_APPROVAL.value,
        )
        return JobStatus.PENDING_APPROVAL

    # Timeout reached without a decision
    error_msg = f"Approval polling timed out after {timeout}s for ticket {ticket_id}"
    finalize_pending_approval_run(repository, job_id, "failed", error_msg)
    await asyncio.to_thread(
        repository.transition_job_status,
        job_id, JobStatus.FAILED,
        error=error_msg,
        error_category="approval_timeout",
    )
    job = await asyncio.to_thread(repository.get_job, job_id)
    if job:
        await run_service.handle_job_failure(job, error_msg, "approval_timeout")
        job = await asyncio.to_thread(repository.get_job, job_id)
    return job.status if job else JobStatus.FAILED
