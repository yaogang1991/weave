"""
JobRepository — atomic JSON persistence for Job and Run entities.

Key design decisions:
- **Atomic writes**: every mutation is written to a ``.tmp`` sibling file and
  moved into place with ``os.replace()``.  Readers never see a half-written
  file, and on crash the original file remains intact.
- **One file per entity**: ``{job_id}.json`` for jobs,
  ``{job_id}/{run_id}.json`` for runs.  This keeps reads/writes isolated and
  avoids a monolithic database file that would require locking.
- **Status transitions are law**: ``transition_job_status`` validates every
  requested move against an explicit allow-list.  Illegal transitions raise
  ``ValueError`` with a clear message.
- **Lease clock is UTC**: all ``lease_expires_at`` values are stored in UTC.
  Callers (workers) are responsible for ensuring their clocks are reasonably
  accurate.
- **Recovery-oriented**: ``list_active_jobs`` and ``recover_orphan_jobs`` are
  idempotent helpers meant to be called at control-plane startup after a
  crash/restart.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from control_plane.models import Job, Run, JobStatus, RunStatus, RetryPolicy


# =============================================================================
# Valid job status transitions
# =============================================================================

_VALID_TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
    JobStatus.QUEUED: {
        JobStatus.LEASED,
        JobStatus.CANCELED,
    },
    JobStatus.LEASED: {
        JobStatus.RUNNING,
        JobStatus.QUEUED,       # lease expired, return to queue
        JobStatus.CANCELED,
    },
    JobStatus.RUNNING: {
        JobStatus.SUCCEEDED,
        JobStatus.FAILED,
        JobStatus.CANCELED,
    },
    JobStatus.FAILED: {
        JobStatus.QUEUED,       # retry
        JobStatus.DEAD_LETTER,  # exhausted max_attempts
    },
    # Terminal states — no outbound transitions
    JobStatus.SUCCEEDED: set(),
    JobStatus.CANCELED: set(),
    JobStatus.DEAD_LETTER: set(),
}


def _utc_now() -> datetime:
    """Current UTC datetime (timezone-aware)."""
    return datetime.now(timezone.utc)


def _json_dump_atomic(data: dict[str, Any], path: Path) -> None:
    """Write *data* to *path* atomically via a temporary file."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, default=str, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(str(tmp), str(path))
    except Exception:
        # Best-effort cleanup of the temp file on failure
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


class JobRepository:
    """
    Persistent store for :class:`Job` and :class:`Run` objects.

    All mutations are atomic (write-to-temp + rename).  The store is
    *single-writer-safe* when backed by a local POSIX filesystem; concurrent
    writers to the same file are not protected and require external locking.
    """

    def __init__(self, base_path: str = "./data/jobs") -> None:
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def _job_path(self, job_id: str) -> Path:
        return self.base_path / f"{job_id}.json"

    def _run_dir(self, job_id: str) -> Path:
        d = self.base_path / job_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _run_path(self, job_id: str, run_id: str) -> Path:
        return self._run_dir(job_id) / f"{run_id}.json"

    # ------------------------------------------------------------------
    # Serialization helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _job_to_dict(job: Job) -> dict[str, Any]:
        return job.model_dump(mode="json")

    @staticmethod
    def _run_to_dict(run: Run) -> dict[str, Any]:
        return run.model_dump(mode="json")

    @staticmethod
    def _dict_to_job(data: dict[str, Any]) -> Job:
        return Job(**data)

    @staticmethod
    def _dict_to_run(data: dict[str, Any]) -> Run:
        return Run(**data)

    # =================================================================
    # Job CRUD
    # =================================================================

    def create_job(
        self,
        requirement: str,
        project_path: str | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> Job:
        """Create a new job, persist it, and return it."""
        now = _utc_now()
        job = Job(
            id=f"job_{uuid.uuid4().hex[:12]}",
            requirement=requirement,
            project_path=project_path,
            retry_policy=retry_policy or RetryPolicy(),
            created_at=now,
            updated_at=now,
        )
        self._persist_job(job)
        return job

    def get_job(self, job_id: str) -> Job | None:
        """Load a job by ID, or ``None`` if not found."""
        path = self._job_path(job_id)
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return self._dict_to_job(data)

    def update_job(self, job: Job) -> Job:
        """Persist an updated job.  The *updated_at* field is refreshed."""
        job.updated_at = _utc_now()
        self._persist_job(job)
        return job

    def _persist_job(self, job: Job) -> None:
        path = self._job_path(job.id)
        _json_dump_atomic(self._job_to_dict(job), path)

    def list_jobs(self, status: JobStatus | None = None) -> list[Job]:
        """Return all jobs, optionally filtered by status."""
        jobs: list[Job] = []
        for path in self.base_path.glob("*.json"):
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            job = self._dict_to_job(data)
            if status is None or job.status == status:
                jobs.append(job)
        # Sort by creation time for stable ordering
        jobs.sort(key=lambda j: j.created_at)
        return jobs

    # =================================================================
    # Run CRUD
    # =================================================================

    def create_run(self, job_id: str, session_id: str) -> Run:
        """Create a new run for the given job, persist it, and return it."""
        now = _utc_now()
        run = Run(
            id=f"run_{uuid.uuid4().hex[:12]}",
            job_id=job_id,
            session_id=session_id,
            started_at=now,
            created_at=now,
            updated_at=now,
        )
        self._persist_run(run)
        return run

    def get_run(self, run_id: str) -> Run | None:
        """Load a run by ID, or ``None`` if not found."""
        # Search across all job run directories
        for run_dir in self.base_path.iterdir():
            if not run_dir.is_dir():
                continue
            path = run_dir / f"{run_id}.json"
            if path.exists():
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                return self._dict_to_run(data)
        return None

    def update_run(self, run: Run) -> Run:
        """Persist an updated run.  The *updated_at* field is refreshed."""
        run.updated_at = _utc_now()
        self._persist_run(run)
        return run

    def _persist_run(self, run: Run) -> None:
        path = self._run_path(run.job_id, run.id)
        _json_dump_atomic(self._run_to_dict(run), path)

    def list_runs_by_job(self, job_id: str) -> list[Run]:
        """Return all runs associated with *job_id*."""
        runs: list[Run] = []
        run_dir = self._run_dir(job_id)
        for path in run_dir.glob("*.json"):
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            runs.append(self._dict_to_run(data))
        runs.sort(key=lambda r: r.created_at)
        return runs

    # =================================================================
    # Status transitions
    # =================================================================

    def transition_job_status(
        self,
        job_id: str,
        to_status: JobStatus,
        error: str = "",
        error_category: str = "",
    ) -> Job:
        """
        Move *job_id* to *to_status*.

        Raises:
            ValueError: If the transition is not allowed or the job does not exist.
        """
        job = self.get_job(job_id)
        if job is None:
            raise ValueError(f"Job not found: {job_id}")

        from_status = job.status

        if to_status not in _VALID_TRANSITIONS.get(from_status, set()):
            raise ValueError(
                f"Illegal status transition: {from_status.value} -> {to_status.value} "
                f"(job_id={job_id})"
            )

        job.status = to_status
        job.updated_at = _utc_now()

        if error:
            job.last_error = error
        if error_category:
            job.error_category = error_category

        # Special handling for retry transitions
        if from_status == JobStatus.FAILED and to_status == JobStatus.QUEUED:
            job.bump_attempt()
            job.last_error = ""
            job.error_category = ""
            job.lease_owner = None
            job.lease_expires_at = None

        # Terminal failure -> dead_letter
        if to_status == JobStatus.DEAD_LETTER:
            job.lease_owner = None
            job.lease_expires_at = None

        self._persist_job(job)
        return job

    # =================================================================
    # Lease management
    # =================================================================

    def acquire_lease(
        self,
        job_id: str,
        owner: str,
        lease_duration_sec: int = 60,
    ) -> Job | None:
        """
        Attempt to lease *job_id* for *owner*.

        Returns:
            The updated Job if the lease was acquired, ``None`` if the job is
            not in ``QUEUED`` status or if an active lease already exists.
        """
        job = self.get_job(job_id)
        if job is None:
            return None

        # Only QUEUED (or expired LEASED) jobs can be leased
        if job.status == JobStatus.LEASED:
            # Allow lease takeover only if the current lease has expired
            if job.lease_expires_at is not None and _utc_now() < job.lease_expires_at:
                return None
        elif job.status != JobStatus.QUEUED:
            return None

        # Check for an unexpired lease on a QUEUED job (defensive)
        if job.lease_expires_at is not None and _utc_now() < job.lease_expires_at:
            return None

        job.status = JobStatus.LEASED
        job.lease_owner = owner
        job.lease_expires_at = _utc_now() + timedelta(seconds=lease_duration_sec)
        job.updated_at = _utc_now()
        self._persist_job(job)
        return job

    def release_lease(self, job_id: str) -> Job:
        """
        Release the lease on *job_id*, setting status back to ``QUEUED``.

        Raises:
            ValueError: If the job does not exist or is not in ``LEASED`` status.
        """
        job = self.get_job(job_id)
        if job is None:
            raise ValueError(f"Job not found: {job_id}")
        if job.status != JobStatus.LEASED:
            raise ValueError(
                f"Cannot release lease: job {job_id} is {job.status.value}, "
                f"expected {JobStatus.LEASED.value}"
            )

        job.status = JobStatus.QUEUED
        job.lease_owner = None
        job.lease_expires_at = None
        job.updated_at = _utc_now()
        self._persist_job(job)
        return job

    def get_stale_leases(self, max_age_sec: int = 120) -> list[Job]:
        """
        Return all jobs in ``LEASED`` status whose lease has expired by
        more than *max_age_sec* seconds.
        """
        cutoff = _utc_now() - timedelta(seconds=max_age_sec)
        stale: list[Job] = []
        for job in self.list_jobs(status=JobStatus.LEASED):
            if job.lease_expires_at is not None and job.lease_expires_at < cutoff:
                stale.append(job)
        return stale

    # =================================================================
    # Recovery helpers
    # =================================================================

    def list_active_jobs(self) -> list[Job]:
        """Return jobs that are queued, leased, or running."""
        active: list[Job] = []
        for status in (JobStatus.QUEUED, JobStatus.LEASED, JobStatus.RUNNING):
            active.extend(self.list_jobs(status=status))
        active.sort(key=lambda j: j.created_at)
        return active

    def recover_orphan_jobs(self) -> list[Job]:
        """
        Return jobs that appear to be orphaned (lease expired while leased
        or running).  Callers should decide how to handle them — typically
        transition back to ``QUEUED`` or mark ``FAILED``.
        """
        now = _utc_now()
        orphaned: list[Job] = []

        # Leased jobs with expired leases
        for job in self.list_jobs(status=JobStatus.LEASED):
            if job.lease_expires_at is not None and job.lease_expires_at < now:
                orphaned.append(job)

        # Running jobs with an expired lease (worker died mid-run)
        for job in self.list_jobs(status=JobStatus.RUNNING):
            if job.lease_expires_at is not None and job.lease_expires_at < now:
                orphaned.append(job)

        orphaned.sort(key=lambda j: j.created_at)
        return orphaned
