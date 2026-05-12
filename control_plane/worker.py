"""
Control Plane Worker — asynchronous job queue consumer with lease-based
coordination, concurrency control, and graceful shutdown.

Key design decisions:
- **Async I/O**: the main loop is fully async; synchronous repository calls are
  offloaded to ``asyncio.to_thread`` so the event loop stays responsive.
- **Lease-based exclusivity**: a worker must ``acquire_lease`` before executing
  a job; this prevents multiple workers from running the same job.
- **Semaphore concurrency**: an ``asyncio.Semaphore`` caps the number of
  concurrent job executions (default 1, practical max 2 for personal use).
- **Graceful shutdown**: SIGTERM / SIGINT signal handlers trigger ``stop()``,
  which cancels the polling loop and waits for in-flight jobs to finish.
- **JSON-line logging**: every significant operation emits a structured log line
  with ``job_id``, ``status``, and ``message`` for easy parsing by log
  aggregators or debug tools.
- **Startup recovery**: on boot the worker scans for orphaned (leased / running
  with expired lease) jobs and returns them to the queue so no job is lost
  after a crash.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import signal
import socket
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure project root is on sys.path so that ``from control_plane.…`` resolves
# even when this file is executed directly.
sys.path.insert(0, str(Path(__file__).parent.parent))

from control_plane.repository import JobRepository
from control_plane.service import RunService
from control_plane.models import Job, JobStatus, RunStatus
from control_plane.approval import TicketStatus
from core.exceptions import PendingApprovalError

# ---------------------------------------------------------------------------
# Logging helpers — JSON Lines on stderr
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class WorkerConfig:
    """Tunable parameters for :class:`TaskWorker`."""

    concurrency: int = 1               # personal scenario — 1 or 2
    poll_interval_sec: int = 5         # how often to poll for new jobs
    lease_duration_sec: int = 60       # lease TTL
    recovery_max_age_sec: int = 120    # orphan threshold (lease expiry)
    heartbeat_interval_sec: int = 30   # how often to refresh an active lease
    max_poll_backoff_sec: int = 60     # cap for empty-queue backoff
    non_interactive: bool = False      # M1.1: non-interactive mode (no stdin)

    def __init__(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            if hasattr(self, k):
                setattr(self, k, v)
            else:
                raise TypeError(f"WorkerConfig has no attribute {k!r}")


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


class TaskWorker:
    """
    Asynchronous job-queue consumer.

    Lifecycle
    ---------
    1. **Startup recovery** — orphan jobs (leases that expired while the
       previous worker was running) are returned to ``QUEUED``.
    2. **Poll loop** — every ``poll_interval_sec`` the worker lists jobs in
       ``QUEUED`` status and tries to acquire a lease.
    3. **Execution** — leased jobs transition to ``RUNNING`` and are handed
       off to :meth:`RunService.run_job`.  A ``Semaphore`` limits concurrency.
    4. **Result handling** — on success the job moves to ``SUCCEEDED``; on
       failure :meth:`RunService.handle_job_failure` decides retry vs. dead-letter.
    5. **Shutdown** — SIGTERM/SIGINT cancels polling; in-flight jobs are
       awaited before the process exits.
    """

    def __init__(
        self,
        repository: JobRepository,
        run_service: RunService,
        config: WorkerConfig | None = None,
    ) -> None:
        self.repository = repository
        self.run_service = run_service
        self.config = config or WorkerConfig()

        # Unique owner id so multiple workers on the same host do not collide.
        self._owner = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"

        # Concurrency guard
        self._semaphore = asyncio.Semaphore(self.config.concurrency)

        # Lifecycle events
        self._stop_event = asyncio.Event()
        self._main_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None

        # Hot reload: initialized here so register_project_path works before start()
        self._config_mtimes: dict[str, float] = {}

        # Track jobs we currently hold a lease on for heartbeat purposes.
        # job_id -> asyncio.Task running the job
        self._in_flight: dict[str, asyncio.Task[None]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the worker: recover orphans, then enter the poll loop."""
        _json_log("INFO", "Worker starting", extra={"owner": self._owner})

        # 1. Recover orphan jobs from a previous crash.
        recovered = await self._recover_orphan_jobs()
        _json_log(
            "INFO",
            f"Recovered {len(recovered)} orphan job(s)",
            extra={"recovered_ids": recovered},
        )

        # 2. Recover pending approval tickets (expired / orphaned).
        recovered_tickets = await self._recover_pending_tickets()
        _json_log(
            "INFO",
            f"Recovered {len(recovered_tickets)} pending ticket(s)",
            extra={"recovered_ticket_ids": recovered_tickets},
        )

        # 3. Start heartbeat refresher in the background.
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat(), name="worker-heartbeat"
        )

        # 4. Enter the main polling loop.
        self._main_task = asyncio.create_task(self._poll_loop(), name="worker-poll")
        await self._main_task

        _json_log("INFO", "Worker stopped")

    async def stop(self) -> None:
        """Signal the worker to stop gracefully."""
        if self._stop_event.is_set():
            return
        _json_log("INFO", "Worker stop requested — shutting down gracefully")
        self._stop_event.set()

        # Cancel the heartbeat task.
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._heartbeat_task

        # Wait for in-flight jobs to complete (with a timeout).
        if self._in_flight:
            _json_log(
                "INFO",
                f"Waiting for {len(self._in_flight)} in-flight job(s) to finish",
            )
            done, pending = await asyncio.wait(
                self._in_flight.values(),
                timeout=self.config.lease_duration_sec,
            )
            for task in pending:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

        # Cancel the main polling task.
        if self._main_task and not self._main_task.done():
            self._main_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._main_task

    # ------------------------------------------------------------------
    # Recovery
    # ------------------------------------------------------------------

    def _log_event(self, event_type: str, job_id: str, payload: dict[str, Any]) -> None:
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

    async def _recover_orphan_jobs(self) -> list[str]:
        """
        Scan for jobs whose lease expired while they were ``LEASED`` or
        ``RUNNING``, and return them to ``QUEUED``.

        Records recovery events for each orphan job and emits a summary
        event.  Returns a list of recovered job IDs.
        """
        orphans = await asyncio.to_thread(self.repository.recover_orphan_jobs)
        recovered: list[str] = []
        for job in orphans:
            try:
                # For orphaned leased jobs, release the lease back to QUEUED.
                if job.status == JobStatus.LEASED:
                    await asyncio.to_thread(self.repository.release_lease, job.id)
                    _json_log(
                        "INFO",
                        "Released orphan lease back to queued",
                        job_id=job.id,
                        status=JobStatus.QUEUED.value,
                    )
                    self._log_event("recovery", job.id, {
                        "old_status": "leased",
                        "new_status": JobStatus.QUEUED.value,
                        "reason": "lease_expired",
                        "recovered_at": datetime.now(timezone.utc).isoformat(),
                    })
                # For orphaned running jobs, transition to FAILED then retry.
                elif job.status == JobStatus.RUNNING:
                    await asyncio.to_thread(
                        self.repository.transition_job_status,
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
                    self._log_event("recovery", job.id, {
                        "old_status": "running",
                        "new_status": JobStatus.FAILED.value,
                        "reason": "lease_expired",
                        "recovered_at": datetime.now(timezone.utc).isoformat(),
                    })
                # PENDING_APPROVAL orphan: check ticket status
                elif job.status == JobStatus.PENDING_APPROVAL:
                    approval_repo = getattr(self.run_service, "approval_repo", None)
                    if approval_repo:
                        pending_tickets = await asyncio.to_thread(
                            approval_repo.get_pending_for_job, job.id,
                        )
                        if pending_tickets:
                            # Still has pending tickets — keep in PENDING_APPROVAL
                            _json_log(
                                "INFO",
                                "PENDING_APPROVAL job still has pending tickets",
                                job_id=job.id,
                                status=JobStatus.PENDING_APPROVAL.value,
                            )
                        else:
                            # Check if any ticket was already approved — resume instead of failing
                            all_tickets = await asyncio.to_thread(
                                approval_repo.list_tickets, job_id=job.id,
                            )
                            approved_tickets = [
                                t for t in all_tickets
                                if t.status == TicketStatus.APPROVED
                            ]
                            if approved_tickets:
                                # Ticket was approved while worker was down — re-queue
                                # so normal _execute_job path handles LEASED→RUNNING.
                                await asyncio.to_thread(
                                    self.repository.transition_job_status,
                                    job.id, JobStatus.QUEUED,
                                )
                                # Clear stale lease so a worker can pick it up
                                job = await asyncio.to_thread(self.repository.get_job, job.id)
                                if job:
                                    job.lease_owner = None
                                    job.lease_expires_at = None
                                    await asyncio.to_thread(self.repository.update_job, job)
                                _json_log(
                                    "INFO",
                                    "Re-queuing PENDING_APPROVAL orphan (ticket already approved)",
                                    job_id=job.id,
                                    status=JobStatus.QUEUED.value,
                                )
                            else:
                                # No pending or approved tickets — mark as failed for retry
                                await asyncio.to_thread(
                                    self.repository.transition_job_status,
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
                        # No approval_repo — cannot determine ticket status, mark failed
                        await asyncio.to_thread(
                            self.repository.transition_job_status,
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
            self._log_event("worker_recovery_summary", "", {
                "recovered_count": len(recovered),
                "recovered_job_ids": recovered,
            })

        return recovered

    async def _recover_pending_tickets(self) -> list[str]:
        """
        启动时处理 pending ticket：
        1. 过期已超时的 pending ticket
        2. 将过期 ticket 关联的 job 推进失败策略
        3. 检查状态不一致（job 已不在 running/leased 但 ticket 仍 pending）
        4. 返回被处理的 ticket ID 列表
        """
        if not hasattr(self.run_service, "approval_repo") or not self.run_service.approval_repo:
            return []

        approval_repo = self.run_service.approval_repo
        ticket_ids: list[str] = []

        # Step 1: 过期超时的 pending ticket
        expired_tickets = await asyncio.to_thread(approval_repo.expire_tickets)

        for ticket in expired_tickets:
            ticket_ids.append(ticket.id)

            # 将关联的 job 推进失败策略
            job = await asyncio.to_thread(self.repository.get_job, ticket.job_id)
            if job and job.status in (JobStatus.LEASED, JobStatus.RUNNING, JobStatus.PENDING_APPROVAL):
                error_msg = f"Approval ticket {ticket.id} expired (timeout)"
                if job.status == JobStatus.LEASED:
                    job = await asyncio.to_thread(
                        self.repository.transition_job_status,
                        job.id,
                        JobStatus.QUEUED,
                        error=error_msg,
                        error_category="timeout",
                    )
                elif job.status == JobStatus.PENDING_APPROVAL:
                    job = await asyncio.to_thread(
                        self.repository.transition_job_status,
                        job.id,
                        JobStatus.FAILED,
                        error=error_msg,
                        error_category="approval_timeout",
                    )
                    job = await self.run_service.handle_job_failure(
                        job,
                        error_msg,
                        "approval_timeout",
                    )
                else:
                    job = await asyncio.to_thread(
                        self.repository.transition_job_status,
                        job.id,
                        JobStatus.FAILED,
                        error=error_msg,
                        error_category="timeout",
                    )
                    job = await self.run_service.handle_job_failure(
                        job,
                        error_msg,
                        "timeout",
                    )

            self._log_event("ticket_expired_recovery", ticket.job_id, {
                "ticket_id": ticket.id,
                "previous_status": "pending",
                "new_status": "expired",
                "job_id": ticket.job_id,
                "reason": "lease_timeout_on_restart",
            })

        # Step 2: 检查剩余 pending ticket 的状态一致性
        # 如果 job 已不在 running/leased 但 ticket 仍 pending，说明异常中断
        pending_tickets = await asyncio.to_thread(
            approval_repo.list_tickets, status=TicketStatus.PENDING
        )

        for ticket in pending_tickets:
            job = await asyncio.to_thread(self.repository.get_job, ticket.job_id)
            if job and job.status not in (JobStatus.RUNNING, JobStatus.LEASED, JobStatus.PENDING_APPROVAL):
                # Job 已不在执行中，但 ticket 仍 pending —— 异常情况
                # 将 ticket 标记为 expired，job 推进失败
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
                            self.repository.transition_job_status,
                            job.id,
                            JobStatus.FAILED,
                            error=error_msg,
                            error_category="unknown",
                        )
                        job = await self.run_service.handle_job_failure(
                            job,
                            error_msg,
                            "unknown",
                        )

                ticket_ids.append(ticket.id)
                self._log_event("ticket_orphan_recovery", ticket.job_id, {
                    "ticket_id": ticket.id,
                    "job_status": job.status.value,
                    "reason": "job_no_longer_active",
                })

        if ticket_ids:
            self._log_event("ticket_recovery_summary", "", {
                "recovered_count": len(ticket_ids),
                "recovered_ticket_ids": ticket_ids,
            })

        return ticket_ids

    # ------------------------------------------------------------------
    # Poll loop
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """
        Continuously poll for ``QUEUED`` jobs until :meth:`stop` is called.

        When no jobs are available the loop sleeps for ``poll_interval_sec``;
        on repeated empty polls a capped exponential backoff is applied so we
        do not hammer the repository.

        On each poll cycle, project config files are checked for changes
        (hot reload). Only new dispatches use the updated config.
        """
        empty_polls = 0
        backoff = self.config.poll_interval_sec

        while not self._stop_event.is_set():
            try:
                # Hot reload: check project configs for changes
                self._check_config_reload()

                found_job = await self._poll_and_execute()

                if found_job:
                    empty_polls = 0
                    backoff = self.config.poll_interval_sec
                else:
                    empty_polls += 1
                    # Cap the backoff at max_poll_backoff_sec
                    backoff = min(
                        self.config.poll_interval_sec * (2 ** (empty_polls // 3)),
                        self.config.max_poll_backoff_sec,
                    )

                # Sleep in small chunks so we react quickly to stop_event.
                slept = 0
                while slept < backoff and not self._stop_event.is_set():
                    await asyncio.sleep(1)
                    slept += 1

            except asyncio.CancelledError:
                _json_log("INFO", "Poll loop cancelled")
                return
            except Exception as exc:
                _json_log("ERROR", f"Unexpected error in poll loop: {exc}")
                await asyncio.sleep(self.config.poll_interval_sec)

    async def _poll_and_execute(self) -> bool:
        """
        One poll iteration: list queued jobs, try to acquire a lease, and
        spawn execution under the semaphore.

        Returns:
            ``True`` if at least one job was found and a task was spawned.
        """
        # List QUEUED jobs.
        jobs: list[Any] = await asyncio.to_thread(
            self.repository.list_jobs, JobStatus.QUEUED
        )

        if not jobs:
            return False

        found_job = False
        for job in jobs:
            if self._stop_event.is_set():
                break

            # Skip jobs that already have an unexpired lease (defensive).
            if job.lease_expires_at is not None and datetime.now(timezone.utc) < job.lease_expires_at:
                continue

            # Try to acquire a lease — this is the race-guard.
            leased_job = await asyncio.to_thread(
                self.repository.acquire_lease,
                job.id,
                self._owner,
                self.config.lease_duration_sec,
            )

            if leased_job is None:
                # Another worker got it first.
                continue

            _json_log(
                "INFO",
                "Lease acquired",
                job_id=job.id,
                status=JobStatus.LEASED.value,
                extra={"lease_owner": self._owner},
            )

            found_job = True

            # Spawn execution guarded by the semaphore.
            task = asyncio.create_task(
                self._execute_job_with_semaphore(job.id), name=f"exec-{job.id}"
            )
            self._in_flight[job.id] = task
            # Clean up the tracking dict when the task finishes.
            task.add_done_callback(lambda t, jid=job.id: self._in_flight.pop(jid, None))

        return found_job

    async def _execute_job_with_semaphore(self, job_id: str) -> None:
        """Wrap :meth:`_execute_job_core` inside the concurrency semaphore.

        Failure handling runs OUTSIDE the semaphore so that cleanup
        (repository state transitions, re-queuing) does not block the
        next job from acquiring the semaphore.
        """
        try:
            async with self._semaphore:
                await self._execute_job_core(job_id)
        except Exception as exc:
            error_msg = str(exc)
            error_category = self._classify_error(exc)
            _json_log(
                "ERROR",
                f"Job execution failed: {error_msg}",
                job_id=job_id,
                status="failed",
                extra={"error_category": error_category},
            )
            await self._handle_failure(job_id, error_msg, error_category)

    # ------------------------------------------------------------------
    # Single job execution
    # ------------------------------------------------------------------

    async def _execute_job_core(self, job_id: str) -> None:
        """
        Core job execution — runs inside the concurrency semaphore.

        Steps:
            1. Transition ``LEASED`` → ``RUNNING``
            2. Call ``RunService.run_job``
            3. On success → ``SUCCEEDED``
            4. On failure → raise to outer handler
        """
        _json_log("INFO", "Starting job execution", job_id=job_id, status="running")

        try:
            # 1. Non-interactive: expire old approval tickets before execution
            if self.config.non_interactive:
                approval_repo = getattr(self.run_service, "approval_repo", None)
                if approval_repo is not None:
                    approval_repo.expire_tickets()

            # 2. Transition to RUNNING.
            await asyncio.to_thread(
                self.repository.transition_job_status,
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
            run = await self.run_service.run_job(job_id)

            # 3. Handle result.
            if run.status == RunStatus.SUCCEEDED:
                # RunService already transitioned RUNNING -> SUCCEEDED.
                # Worker only reads and logs — no double-push.
                current_job = await asyncio.to_thread(
                    self.repository.get_job, job_id,
                )
                _json_log(
                    "INFO",
                    "Job completed successfully",
                    job_id=job_id,
                    status=current_job.status.value if current_job else "unknown",
                )
            else:
                # run_job has already performed RUNNING -> FAILED -> QUEUED
                # (or DEAD_LETTER).  Check current job state and only raise
                # if the job is still stuck in RUNNING.
                current_job = await asyncio.to_thread(
                    self.repository.get_job, job_id
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
                    # Job still in RUNNING (or other unexpected state) —
                    # let outer handler deal with it.
                    raise RuntimeError(
                        f"Run ended with status {run.status.value}, "
                        f"job status is {current_job.status.value}"
                    )

        except asyncio.CancelledError:
            # Worker is shutting down — try to return the job to the queue.
            _json_log(
                "WARNING",
                "Job execution cancelled (worker shutting down)",
                job_id=job_id,
            )
            try:
                await asyncio.to_thread(
                    self.repository.release_lease, job_id
                )
            except Exception:
                pass
            raise  # Re-raise so asyncio knows the task was cancelled.

        except PendingApprovalError as exc:
            # Agent hit a high-risk tool — pause and wait for approval.
            await asyncio.to_thread(
                self.repository.transition_job_status,
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
            final_status = await self._poll_for_approval(job_id, exc.ticket_id)

            if final_status == JobStatus.RUNNING:
                # Approval granted — re-execute the job.
                # The approved tool call will be recognized by
                # Guardrails.check_and_execute() via find_approved_ticket().
                _json_log(
                    "INFO",
                    f"Approval granted, resuming job",
                    job_id=job_id,
                    status=JobStatus.RUNNING.value,
                )
                # Finalize the prior pending_approval run before starting a new one
                self._finalize_pending_approval_run(
                    job_id, "failed", "Superseded by post-approval re-execution",
                )
                # Loop to handle multi-approval (re-approval after each granted ticket)
                while True:
                    try:
                        run = await self.run_service.run_job(job_id)
                        if run.status == RunStatus.SUCCEEDED:
                            # RunService already transitioned RUNNING -> SUCCEEDED.
                            current_job = await asyncio.to_thread(
                                self.repository.get_job, job_id,
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
                        # Multi-approval: another tool needs approval — loop back.
                        _json_log(
                            "INFO",
                            f"Post-approval execution requires another approval (ticket: {pa_exc.ticket_id})",
                            job_id=job_id,
                            status=JobStatus.PENDING_APPROVAL.value,
                            extra={"ticket_id": pa_exc.ticket_id},
                        )
                        self._finalize_pending_approval_run(
                            job_id, "failed", "Superseded by next approval cycle",
                        )
                        await asyncio.to_thread(
                            self.repository.transition_job_status,
                            job_id, JobStatus.PENDING_APPROVAL,
                        )
                        final_status = await self._poll_for_approval(job_id, pa_exc.ticket_id)
                        if final_status != JobStatus.RUNNING:
                            # Poll handler set terminal state (rejected/expired/canceled)
                            break
                        # Finalize and loop back for another run attempt
                        self._finalize_pending_approval_run(
                            job_id, "failed", "Superseded by next approval cycle",
                        )
                    except Exception as exc2:
                        raise  # Let outer _execute_job_with_semaphore handle it

    async def _handle_failure(
        self, job_id: str, error: str, error_category: str
    ) -> None:
        """
        Handle a job failure by delegating to :meth:`RunService.handle_job_failure`.

        The service decides whether to retry (transition back to ``QUEUED``)
        or dead-letter the job.
        """
        try:
            job = await asyncio.to_thread(self.repository.get_job, job_id)
            if job is None:
                _json_log("ERROR", "Job not found during failure handling", job_id=job_id)
                return

            updated_job = await self.run_service.handle_job_failure(
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
            # Last resort: mark FAILED so it is not stuck in RUNNING.
            try:
                await asyncio.to_thread(
                    self.repository.transition_job_status,
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

    # ------------------------------------------------------------------
    # Approval polling
    # ------------------------------------------------------------------

    def _finalize_pending_approval_run(
        self, job_id: str, run_final_status: str, detail_msg: str,
    ) -> None:
        """Finalize any run stuck in PENDING_APPROVAL status.

        Args:
            run_final_status: Target RunStatus value ("failed", "aborted", etc.)
            detail_msg: Reason for the status change.
        """
        try:
            from control_plane.models import RunStatus as RS
            target_status = RS(run_final_status)
        except ValueError:
            target_status = RunStatus.FAILED
        try:
            runs = self.repository.list_runs_by_job(job_id)
            for run in runs:
                if run.status == RunStatus.PENDING_APPROVAL:
                    run.status = target_status
                    run.completed_at = datetime.now(timezone.utc)
                    run.dag_result = {"error": detail_msg}
                    self.repository.update_run(run)
        except Exception as exc:
            _json_log(
                "WARNING",
                f"Failed to finalize pending-approval run: {exc}",
                job_id=job_id,
            )

    async def _poll_for_approval(
        self,
        job_id: str,
        ticket_id: str,
    ) -> JobStatus:
        """
        Poll for an approval decision on a pending ticket.

        Returns:
            JobStatus.RUNNING if approved (caller should re-execute).
            JobStatus.FAILED/CANCELED/DEAD_LETTER if rejected/expired.
        """
        approval_repo = getattr(self.run_service, "approval_repo", None)
        if approval_repo is None:
            await self._handle_failure(
                job_id, "No approval repository configured", "tool_blocked"
            )
            return JobStatus.FAILED

        poll_interval = 5  # seconds
        timeout = self.run_service.approval_timeout_sec
        elapsed = 0

        while not self._stop_event.is_set() and elapsed < timeout:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

            ticket = await asyncio.to_thread(approval_repo.get_ticket, ticket_id)
            if ticket is None:
                continue

            if ticket.status == TicketStatus.APPROVED:
                # Guard against external cancel while waiting
                job = await asyncio.to_thread(self.repository.get_job, job_id)
                if job and job.status == JobStatus.CANCELED:
                    _json_log(
                        "INFO",
                        "Job was canceled while awaiting approval",
                        job_id=job_id,
                        status=JobStatus.CANCELED.value,
                    )
                    self._finalize_pending_approval_run(
                        job_id, "aborted", "Job canceled during approval wait",
                    )
                    return JobStatus.CANCELED
                # Transition PENDING_APPROVAL -> RUNNING
                await asyncio.to_thread(
                    self.repository.transition_job_status,
                    job_id, JobStatus.RUNNING,
                )
                return JobStatus.RUNNING

            if ticket.status == TicketStatus.REJECTED:
                error_msg = f"Approval rejected for ticket {ticket_id}: {ticket.reason}"
                # Finalize run status: pending_approval -> failed
                self._finalize_pending_approval_run(job_id, "failed", error_msg)
                await asyncio.to_thread(
                    self.repository.transition_job_status,
                    job_id, JobStatus.FAILED,
                    error=error_msg,
                    error_category="tool_blocked",
                )
                job = await asyncio.to_thread(self.repository.get_job, job_id)
                if job:
                    await self.run_service.handle_job_failure(job, error_msg, "tool_blocked")
                    job = await asyncio.to_thread(self.repository.get_job, job_id)
                _json_log("WARNING", f"Approval rejected: {ticket.reason}", job_id=job_id)
                return job.status if job else JobStatus.FAILED

            if ticket.status == TicketStatus.EXPIRED:
                error_msg = f"Approval ticket {ticket_id} expired"
                # Finalize run status: pending_approval -> failed
                self._finalize_pending_approval_run(job_id, "failed", error_msg)
                await asyncio.to_thread(
                    self.repository.transition_job_status,
                    job_id, JobStatus.FAILED,
                    error=error_msg,
                    error_category="approval_timeout",
                )
                job = await asyncio.to_thread(self.repository.get_job, job_id)
                if job:
                    await self.run_service.handle_job_failure(job, error_msg, "approval_timeout")
                    job = await asyncio.to_thread(self.repository.get_job, job_id)
                _json_log("WARNING", f"Approval ticket expired: {ticket_id}", job_id=job_id)
                return job.status if job else JobStatus.FAILED

        # Loop exited — either timeout or worker shutting down.
        if self._stop_event.is_set():
            # Worker shutdown: preserve PENDING_APPROVAL so a future worker
            # can pick it up. Do NOT mark as failed.
            _json_log(
                "INFO",
                "Worker shutting down during approval poll — preserving PENDING_APPROVAL",
                job_id=job_id,
                status=JobStatus.PENDING_APPROVAL.value,
            )
            return JobStatus.PENDING_APPROVAL

        # Timeout reached without a decision
        error_msg = f"Approval polling timed out after {timeout}s for ticket {ticket_id}"
        self._finalize_pending_approval_run(job_id, "failed", error_msg)
        await asyncio.to_thread(
            self.repository.transition_job_status,
            job_id, JobStatus.FAILED,
            error=error_msg,
            error_category="approval_timeout",
        )
        job = await asyncio.to_thread(self.repository.get_job, job_id)
        if job:
            await self.run_service.handle_job_failure(job, error_msg, "approval_timeout")
            job = await asyncio.to_thread(self.repository.get_job, job_id)
        return job.status if job else JobStatus.FAILED

    @staticmethod
    def _classify_error(exc: BaseException) -> str:
        """Map an exception to a coarse error category."""
        name = type(exc).__name__
        if "Timeout" in name or "timeout" in str(exc).lower():
            return "timeout"
        if "Blocked" in name or "blocked" in str(exc).lower():
            return "tool_blocked"
        if "Eval" in name or "eval" in str(exc).lower():
            return "eval_failed"
        return "unknown"

    # ------------------------------------------------------------------
    # Hot reload — project config file change detection
    # ------------------------------------------------------------------

    def _check_config_reload(self) -> None:
        """
        Check known project paths for config file changes.

        Discovers project paths from queued/running jobs, registers them,
        then compares .harness/config.yaml mtime against cached values.
        On change, the config is reloaded in place so future job
        dispatches use the new settings. In-flight jobs are unaffected.
        """
        from pathlib import Path

        # Discover project paths from jobs in the repository
        try:
            from control_plane.models import JobStatus
            jobs = self.repository.list_jobs()
            for job in jobs:
                if job.project_path and job.project_path not in self._config_mtimes:
                    self.register_project_path(job.project_path)
        except Exception:
            pass  # Discovery failure should not break the poll loop

        for project_path_str in list(self._config_mtimes.keys()):
            config_path = Path(project_path_str) / ".harness" / "config.yaml"
            if not config_path.exists():
                continue

            try:
                current_mtime = config_path.stat().st_mtime
            except OSError:
                continue

            last_mtime = self._config_mtimes.get(project_path_str, 0.0)
            if current_mtime > last_mtime:
                self._config_mtimes[project_path_str] = current_mtime
                _json_log(
                    "INFO",
                    "Project config changed — will use new settings for next dispatch",
                    extra={
                        "project_path": project_path_str,
                        "config_file": str(config_path),
                    },
                )

    def register_project_path(self, project_path: str) -> None:
        """Register a project path for hot reload monitoring."""
        from pathlib import Path

        config_path = Path(project_path) / ".harness" / "config.yaml"
        if config_path.exists():
            try:
                self._config_mtimes[project_path] = config_path.stat().st_mtime
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Heartbeat — optional lease refresh
    # ------------------------------------------------------------------

    async def _heartbeat(self) -> None:
        """
        Periodically refresh leases for jobs that are still ``RUNNING``.

        This runs as a background task so long-running jobs do not lose their
        lease before they finish.
        """
        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(self.config.heartbeat_interval_sec)

                if self._stop_event.is_set():
                    return

                # Refresh lease for each in-flight job.
                for job_id in list(self._in_flight.keys()):
                    task = self._in_flight.get(job_id)
                    if task is None or task.done():
                        continue

                    try:
                        # Re-acquire lease extends the lease_expires_at.
                        await asyncio.to_thread(
                            self.repository.acquire_lease,
                            job_id,
                            self._owner,
                            self.config.lease_duration_sec,
                        )
                        _json_log(
                            "DEBUG",
                            "Lease heartbeat refreshed",
                            job_id=job_id,
                        )
                    except Exception as exc:
                        _json_log(
                            "WARNING",
                            f"Failed to refresh lease: {exc}",
                            job_id=job_id,
                        )

            except asyncio.CancelledError:
                return
            except Exception as exc:
                _json_log("ERROR", f"Heartbeat loop error: {exc}")


# ---------------------------------------------------------------------------
# Convenience entry-point
# ---------------------------------------------------------------------------


async def run_worker(
    repository: JobRepository,
    run_service: RunService,
    config: WorkerConfig | None = None,
) -> None:
    """
    Create a :class:`TaskWorker`, wire up signal handlers, and start it.

    This function blocks until the worker is stopped via SIGTERM/SIGINT or
    :meth:`TaskWorker.stop` is called externally.
    """
    worker = TaskWorker(repository, run_service, config)

    loop = asyncio.get_running_loop()

    def _signal_handler(sig: int) -> None:
        """Schedule ``worker.stop()`` on the event loop when a signal arrives."""
        _json_log("INFO", f"Received signal {sig} — initiating graceful shutdown")
        # Use ``call_soon_threadsafe`` because signal handlers may run in a
        # different OS thread on some platforms.
        loop.call_soon_threadsafe(asyncio.create_task, worker.stop())

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _signal_handler, sig)
        except (NotImplementedError, ValueError):
            # Windows does not support add_signal_handler for SIGTERM.
            import signal as sigmod
            sigmod.signal(sig, lambda _s, _f: _signal_handler(sig))

    try:
        await worker.start()
    except asyncio.CancelledError:
        pass
    finally:
        # Remove signal handlers so they do not linger in the event loop.
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.remove_signal_handler(sig)
            except (NotImplementedError, ValueError):
                pass
