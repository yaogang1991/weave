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
import signal
import socket
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from control_plane.repository import JobRepository  # noqa: E402
from control_plane.service import RunService  # noqa: E402
from control_plane.models import JobStatus  # noqa: E402
from control_plane.worker_recovery import (  # noqa: E402
    _json_log,
    recover_orphan_jobs,
    recover_pending_tickets,
)
from control_plane.worker_executor import (  # noqa: E402
    classify_error,
    execute_job_core,
    finalize_pending_approval_run,
    handle_failure,
    poll_for_approval,
)


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
    1. **Startup recovery** — orphan jobs are returned to ``QUEUED``.
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

        self._owner = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
        self._semaphore = asyncio.Semaphore(self.config.concurrency)
        self._stop_event = asyncio.Event()
        self._main_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._config_mtimes: dict[str, float] = {}
        self._in_flight: dict[str, asyncio.Task[None]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the worker: recover orphans, then enter the poll loop."""
        _json_log("INFO", "Worker starting", extra={"owner": self._owner})

        recovered = await recover_orphan_jobs(self.repository, self.run_service)
        _json_log(
            "INFO",
            f"Recovered {len(recovered)} orphan job(s)",
            extra={"recovered_ids": recovered},
        )

        recovered_tickets = await recover_pending_tickets(self.repository, self.run_service)
        _json_log(
            "INFO",
            f"Recovered {len(recovered_tickets)} pending ticket(s)",
            extra={"recovered_ticket_ids": recovered_tickets},
        )

        self._heartbeat_task = asyncio.create_task(
            self._heartbeat(), name="worker-heartbeat"
        )

        self._main_task = asyncio.create_task(self._poll_loop(), name="worker-poll")
        await self._main_task

        _json_log("INFO", "Worker stopped")

    async def stop(self) -> None:
        """Signal the worker to stop gracefully."""
        if self._stop_event.is_set():
            return
        _json_log("INFO", "Worker stop requested — shutting down gracefully")
        self._stop_event.set()

        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._heartbeat_task

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

        if self._main_task and not self._main_task.done():
            self._main_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._main_task

    # ------------------------------------------------------------------
    # Poll loop
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """Continuously poll for QUEUED jobs until stop() is called."""
        empty_polls = 0
        backoff = self.config.poll_interval_sec

        while not self._stop_event.is_set():
            try:
                self._check_config_reload()
                found_job = await self._poll_and_execute()

                if found_job:
                    empty_polls = 0
                    backoff = self.config.poll_interval_sec
                else:
                    empty_polls += 1
                    backoff = min(
                        self.config.poll_interval_sec * (2 ** (empty_polls // 3)),
                        self.config.max_poll_backoff_sec,
                    )

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
        """One poll iteration: list queued jobs, try to acquire a lease."""
        jobs: list[Any] = await asyncio.to_thread(
            self.repository.list_jobs, JobStatus.QUEUED
        )

        if not jobs:
            return False

        found_job = False
        for job in jobs:
            if self._stop_event.is_set():
                break

            if (
                job.lease_expires_at is not None
                and datetime.now(timezone.utc) < job.lease_expires_at
            ):
                continue

            leased_job = await asyncio.to_thread(
                self.repository.acquire_lease,
                job.id,
                self._owner,
                self.config.lease_duration_sec,
            )

            if leased_job is None:
                continue

            _json_log(
                "INFO",
                "Lease acquired",
                job_id=job.id,
                status=JobStatus.LEASED.value,
                extra={"lease_owner": self._owner},
            )

            found_job = True

            task = asyncio.create_task(
                self._execute_job_with_semaphore(job.id), name=f"exec-{job.id}"
            )
            self._in_flight[job.id] = task
            task.add_done_callback(lambda t, jid=job.id: self._in_flight.pop(jid, None))

        return found_job

    async def _execute_job_with_semaphore(self, job_id: str) -> None:
        """Wrap execute_job_core inside the concurrency semaphore."""
        try:
            async with self._semaphore:
                await execute_job_core(
                    self.repository, self.run_service,
                    job_id, self.config.non_interactive,
                )
        except Exception as exc:
            error_msg = str(exc)
            error_category = classify_error(exc)
            _json_log(
                "ERROR",
                f"Job execution failed: {error_msg}",
                job_id=job_id,
                status="failed",
                extra={"error_category": error_category},
            )
            await handle_failure(
                self.repository, self.run_service, job_id, error_msg, error_category,
            )

    # ------------------------------------------------------------------
    # Hot reload — project config file change detection
    # ------------------------------------------------------------------

    def _check_config_reload(self) -> None:
        """Check known project paths for config file changes."""
        from pathlib import Path

        try:
            jobs = self.repository.list_jobs()
            for job in jobs:
                if job.project_path and job.project_path not in self._config_mtimes:
                    self.register_project_path(job.project_path)
        except Exception:
            pass

        for project_path_str in list(self._config_mtimes.keys()):
            config_path = Path(project_path_str) / ".weave" / "config.yaml"
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

        config_path = Path(project_path) / ".weave" / "config.yaml"
        if config_path.exists():
            try:
                self._config_mtimes[project_path] = config_path.stat().st_mtime
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Heartbeat — optional lease refresh
    # ------------------------------------------------------------------

    async def _heartbeat(self) -> None:
        """Periodically refresh leases for jobs that are still RUNNING."""
        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(self.config.heartbeat_interval_sec)

                if self._stop_event.is_set():
                    return

                for job_id in list(self._in_flight.keys()):
                    task = self._in_flight.get(job_id)
                    if task is None or task.done():
                        continue

                    try:
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

    # ------------------------------------------------------------------
    # Backward-compat: keep old method names as thin wrappers
    # ------------------------------------------------------------------

    async def _recover_orphan_jobs(self) -> list[str]:
        return await recover_orphan_jobs(self.repository, self.run_service)

    async def _recover_pending_tickets(self) -> list[str]:
        return await recover_pending_tickets(self.repository, self.run_service)

    async def _execute_job_core(self, job_id: str) -> None:
        await execute_job_core(
            self.repository, self.run_service,
            job_id, self.config.non_interactive,
        )

    async def _handle_failure(self, job_id: str, error: str, error_category: str) -> None:
        await handle_failure(
            self.repository, self.run_service, job_id, error, error_category,
        )

    def _finalize_pending_approval_run(
        self, job_id: str, run_final_status: str, detail_msg: str,
    ) -> None:
        finalize_pending_approval_run(self.repository, job_id, run_final_status, detail_msg)

    async def _poll_for_approval(self, job_id: str, ticket_id: str) -> JobStatus:
        return await poll_for_approval(
            self.repository, self.run_service, job_id, ticket_id,
            stop_event=self._stop_event,
        )

    @staticmethod
    def _classify_error(exc: BaseException) -> str:
        return classify_error(exc)

    def _log_event(self, event_type: str, job_id: str, payload: dict[str, Any]) -> None:
        from control_plane.worker_recovery import log_event
        log_event(event_type, job_id, payload)


# ---------------------------------------------------------------------------
# Convenience entry-point
# ---------------------------------------------------------------------------


async def run_worker(
    repository: JobRepository,
    run_service: RunService,
    config: WorkerConfig | None = None,
) -> None:
    """Create a TaskWorker, wire up signal handlers, and start it."""
    worker = TaskWorker(repository, run_service, config)

    loop = asyncio.get_running_loop()

    def _signal_handler(sig: int) -> None:
        _json_log("INFO", f"Received signal {sig} — initiating graceful shutdown")
        loop.call_soon_threadsafe(asyncio.create_task, worker.stop())

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _signal_handler, sig)
        except (NotImplementedError, ValueError):
            import signal as sigmod
            sigmod.signal(sig, lambda _s, _f: _signal_handler(sig))

    try:
        await worker.start()
    except asyncio.CancelledError:
        pass
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.remove_signal_handler(sig)
            except (NotImplementedError, ValueError):
                pass
