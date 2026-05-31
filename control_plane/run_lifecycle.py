"""
RunLifecycleManager — extracted from RunService (#177 PR 1).

Manages Run status transitions: succeeded, failed, timed_out, canceled,
pending_approval.  Each method returns the updated Run so the caller can
chain or return directly.

Uses model_copy() for immutable state transitions — the original Run
object is never mutated in-place, so a failed update_run() cannot leave
the caller with a corrupted in-memory object.
"""
from __future__ import annotations

import logging
from typing import Any

from control_plane.models import Job, JobStatus, Run, RunStatus
from control_plane.repository import JobRepository

logger = logging.getLogger(__name__)


class RunLifecycleManager:
    """Centralized Run status transitions.

    Extracts all ``run.status = ...`` mutations from RunService so
    status logic lives in one place and is independently testable.

    All methods use model_copy() to produce a new Run instance without
    mutating the original, consistent with the project's immutability
    convention.
    """

    def __init__(self, repository: JobRepository) -> None:
        self.repository = repository

    def mark_succeeded(self, run: Run, dag_result: dict[str, Any]) -> Run:
        """Transition run to SUCCEEDED and persist."""
        updated = run.model_copy(update={
            "status": RunStatus.SUCCEEDED,
            "dag_result": dag_result,
            "completed_at": self._utc_now(),
        })
        self.repository.update_run(updated)
        return updated

    def mark_failed(
        self,
        run: Run,
        dag_result: dict[str, Any] | None = None,
    ) -> Run:
        """Transition run to FAILED and persist."""
        updates: dict[str, Any] = {
            "status": RunStatus.FAILED,
            "completed_at": self._utc_now(),
        }
        if dag_result is not None:
            updates["dag_result"] = dag_result
        updated = run.model_copy(update=updates)
        self.repository.update_run(updated)
        return updated

    def mark_timed_out(self, run: Run, timeout_seconds: int) -> Run:
        """Transition run to TIMED_OUT and persist."""
        updated = run.model_copy(update={
            "status": RunStatus.TIMED_OUT,
            "completed_at": self._utc_now(),
            "dag_result": {
                "error": "timeout",
                "reason": f"Exceeded {timeout_seconds}s",
            },
        })
        self.repository.update_run(updated)
        return updated

    def mark_canceled(self, run: Run, reason: str = "") -> Run:
        """Transition run to ABORTED (canceled) and persist."""
        updated = run.model_copy(update={
            "status": RunStatus.ABORTED,
            "completed_at": self._utc_now(),
            "dag_result": {"error": "canceled", "reason": reason},
        })
        self.repository.update_run(updated)
        return updated

    def mark_pending_approval(
        self,
        run: Run,
        ticket_id: str,
    ) -> Run:
        """Transition run to PENDING_APPROVAL and persist."""
        updated = run.model_copy(update={
            "status": RunStatus.PENDING_APPROVAL,
            "dag_result": {
                "status": "pending_approval",
                "ticket_id": ticket_id,
            },
        })
        self.repository.update_run(updated)
        return updated

    def resolve_external_status(self, run: Run, job: Job) -> Run | None:
        """Check if job was externally canceled/requeued while running.

        If the job is no longer RUNNING, update run status accordingly
        and return the updated run.  Returns None if job is still RUNNING.
        """
        if job.status == JobStatus.RUNNING:
            return None

        if job.status == JobStatus.CANCELED:
            return self.mark_canceled(run, "Job canceled externally")
        else:
            return self.mark_failed(run, {"error": "external", "reason": "Job externally requeued"})

    @staticmethod
    def _utc_now():
        from datetime import datetime, timezone
        return datetime.now(timezone.utc)
