"""Worker recovery — orphan job and pending ticket recovery at startup.

Extracted from TaskWorker for maintainability (#442).
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from typing import Any

from control_plane.models import JobStatus
from control_plane.approval import TicketStatus


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


def log_event(event_type: str, job_id: str, payload: dict[str, Any]) -> None:
    """Emit a structured recovery event log to stderr."""
    entry: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "level": "INFO",
        "event": event_type,
    }
    if job_id:
        entry["job_id"] = job_id
    entry.update(payload)
    print(json.dumps(entry, ensure_ascii=False, default=str), file=sys.stderr, flush=True)


async def recover_orphan_jobs(
    repository: Any,
    run_service: Any,
) -> list[str]:
    """Scan for jobs whose lease expired while they were LEASED or RUNNING.

    Returns them to QUEUED. Records recovery events for each orphan job
    and emits a summary event.  Returns a list of recovered job IDs.
    """
    orphans = await asyncio.to_thread(repository.recover_orphan_jobs)
    recovered: list[str] = []
    for job in orphans:
        try:
            if job.status == JobStatus.LEASED:
                await asyncio.to_thread(repository.release_lease, job.id)
                _json_log(
                    "INFO",
                    "Released orphan lease back to queued",
                    job_id=job.id,
                    status=JobStatus.QUEUED.value,
                )
                log_event("recovery", job.id, {
                    "old_status": "leased",
                    "new_status": JobStatus.QUEUED.value,
                    "reason": "lease_expired",
                    "recovered_at": datetime.now(timezone.utc).isoformat(),
                })
            elif job.status == JobStatus.RUNNING:
                await asyncio.to_thread(
                    repository.transition_job_status,
                    job.id,
                    JobStatus.FAILED,
                    error="Worker crashed — job recovered at startup",
                    error_category="unknown",
                )
                _json_log(
                    "INFO",
                    "Marked orphan running job as failed for retry",
                    job_id=job.id,
                    status=JobStatus.FAILED.value,
                )
                log_event("recovery", job.id, {
                    "old_status": "running",
                    "new_status": JobStatus.FAILED.value,
                    "reason": "lease_expired",
                    "recovered_at": datetime.now(timezone.utc).isoformat(),
                })
            elif job.status == JobStatus.PENDING_APPROVAL:
                approval_repo = getattr(run_service, "approval_repo", None)
                if approval_repo:
                    pending_tickets = await asyncio.to_thread(
                        approval_repo.get_pending_for_job, job.id,
                    )
                    if pending_tickets:
                        _json_log(
                            "INFO",
                            "PENDING_APPROVAL job still has pending tickets",
                            job_id=job.id,
                            status=JobStatus.PENDING_APPROVAL.value,
                        )
                    else:
                        all_tickets = await asyncio.to_thread(
                            approval_repo.list_tickets, job_id=job.id,
                        )
                        approved_tickets = [
                            t for t in all_tickets
                            if t.status == TicketStatus.APPROVED
                        ]
                        if approved_tickets:
                            await asyncio.to_thread(
                                repository.transition_job_status,
                                job.id, JobStatus.QUEUED,
                            )
                            job = await asyncio.to_thread(repository.get_job, job.id)
                            if job:
                                job.lease_owner = None
                                job.lease_expires_at = None
                                await asyncio.to_thread(repository.update_job, job)
                            _json_log(
                                "INFO",
                                "Re-queuing PENDING_APPROVAL orphan (ticket already approved)",
                                job_id=job.id,
                                status=JobStatus.QUEUED.value,
                            )
                        else:
                            await asyncio.to_thread(
                                repository.transition_job_status,
                                job.id, JobStatus.FAILED,
                                error="Worker crashed while awaiting approval",
                                error_category="unknown",
                            )
                            _json_log(
                                "INFO",
                                "PENDING_APPROVAL orphan marked failed (no pending tickets)",
                                job_id=job.id,
                                status=JobStatus.FAILED.value,
                            )
                else:
                    await asyncio.to_thread(
                        repository.transition_job_status,
                        job.id, JobStatus.FAILED,
                        error="Cannot recover PENDING_APPROVAL orphan: no approval repo",
                        error_category="unknown",
                    )
                    _json_log(
                        "INFO",
                        "PENDING_APPROVAL orphan marked failed (no approval repo)",
                        job_id=job.id,
                        status=JobStatus.FAILED.value,
                    )
            recovered.append(job.id)
        except Exception as exc:
            _json_log(
                "ERROR",
                f"Failed to recover orphan job: {exc}",
                job_id=job.id,
            )

    if recovered:
        log_event("worker_recovery_summary", "", {
            "recovered_count": len(recovered),
            "recovered_job_ids": recovered,
        })

    return recovered


async def recover_pending_tickets(
    repository: Any,
    run_service: Any,
) -> list[str]:
    """Recover pending approval tickets at startup.

    1. Expire timed-out pending tickets
    2. Push associated jobs through failure policy
    3. Check state inconsistencies (job no longer active but ticket still pending)
    4. Return processed ticket IDs
    """
    if not hasattr(run_service, "approval_repo") or not run_service.approval_repo:
        return []

    approval_repo = run_service.approval_repo
    ticket_ids: list[str] = []

    # Step 1: Expire timed-out pending tickets
    expired_tickets = await asyncio.to_thread(approval_repo.expire_tickets)

    for ticket in expired_tickets:
        ticket_ids.append(ticket.id)

        job = await asyncio.to_thread(repository.get_job, ticket.job_id)
        if job and job.status in (JobStatus.LEASED, JobStatus.RUNNING, JobStatus.PENDING_APPROVAL):
            error_msg = f"Approval ticket {ticket.id} expired (timeout)"
            if job.status == JobStatus.LEASED:
                job = await asyncio.to_thread(
                    repository.transition_job_status,
                    job.id,
                    JobStatus.QUEUED,
                    error=error_msg,
                    error_category="timeout",
                )
            elif job.status == JobStatus.PENDING_APPROVAL:
                job = await asyncio.to_thread(
                    repository.transition_job_status,
                    job.id,
                    JobStatus.FAILED,
                    error=error_msg,
                    error_category="approval_timeout",
                )
                job = await run_service.handle_job_failure(
                    job,
                    error_msg,
                    "approval_timeout",
                )
            else:
                job = await asyncio.to_thread(
                    repository.transition_job_status,
                    job.id,
                    JobStatus.FAILED,
                    error=error_msg,
                    error_category="timeout",
                )
                job = await run_service.handle_job_failure(
                    job,
                    error_msg,
                    "timeout",
                )

        log_event("ticket_expired_recovery", ticket.job_id, {
            "ticket_id": ticket.id,
            "previous_status": "pending",
            "new_status": "expired",
            "job_id": ticket.job_id,
            "reason": "lease_timeout_on_restart",
        })

    # Step 2: Check remaining pending tickets for state consistency
    pending_tickets = await asyncio.to_thread(
        approval_repo.list_tickets, status=TicketStatus.PENDING
    )

    for ticket in pending_tickets:
        job = await asyncio.to_thread(repository.get_job, ticket.job_id)
        if job and job.status not in (
            JobStatus.RUNNING, JobStatus.LEASED, JobStatus.PENDING_APPROVAL,
        ):
            # Job no longer active but ticket still pending — mark expired
            ticket.status = TicketStatus.EXPIRED
            ticket.decided_by = "auto"
            ticket.reason = "Job no longer active"
            ticket.decided_at = datetime.now(timezone.utc)
            ticket.updated_at = datetime.now(timezone.utc)
            await asyncio.to_thread(approval_repo.update_ticket, ticket)

            if job.status not in (
                JobStatus.SUCCEEDED, JobStatus.FAILED,
                JobStatus.CANCELED, JobStatus.DEAD_LETTER,
            ):
                error_msg = f"Approval ticket {ticket.id} orphaned (job no longer active)"
                if job.status != JobStatus.QUEUED:
                    job = await asyncio.to_thread(
                        repository.transition_job_status,
                        job.id,
                        JobStatus.FAILED,
                        error=error_msg,
                        error_category="unknown",
                    )
                    job = await run_service.handle_job_failure(
                        job,
                        error_msg,
                        "unknown",
                    )

            ticket_ids.append(ticket.id)
            log_event("ticket_orphan_recovery", ticket.job_id, {
                "ticket_id": ticket.id,
                "job_status": job.status.value,
                "reason": "job_no_longer_active",
            })

    if ticket_ids:
        log_event("ticket_recovery_summary", "", {
            "recovered_count": len(ticket_ids),
            "recovered_ticket_ids": ticket_ids,
        })

    return ticket_ids
