"""
Control Plane Models — Job queue, Run tracking, and RetryPolicy.

Key design decisions:
- Pydantic v2 BaseModel for all structures, enabling JSON (de)serialization
- Status enums inherit from ``str`` so they serialize to plain strings
- ``RetryPolicy`` is embedded in ``Job`` rather than global so each job can
  customise its own retry behaviour.
- Timestamps are required on creation (no auto-default) so callers must be
  explicit about when a job/run was created — this avoids clock-skew surprises
  across distributed workers.
- ``Run`` keeps a lightweight ``dag_result`` dict rather than the full DAG
  object to avoid circular imports and keep the control-plane boundary clean.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator


# =============================================================================
# Status enums
# =============================================================================


class JobStatus(str, Enum):
    """Lifecycle states of a Job in the queue."""

    QUEUED = "queued"
    LEASED = "leased"       # acquired by a worker but not yet running
    RUNNING = "running"     # worker is actively executing the job
    PENDING_APPROVAL = "pending_approval"  # paused, awaiting human approval
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"
    DEAD_LETTER = "dead_letter"


class RunStatus(str, Enum):
    """Lifecycle states of a Run (a single execution attempt)."""

    RUNNING = "running"
    PENDING_APPROVAL = "pending_approval"  # paused for human approval
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ABORTED = "aborted"
    TIMED_OUT = "timed_out"


class TicketStatus(str, Enum):
    """Lifecycle states of an Approval Ticket."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


# =============================================================================
# Data models
# =============================================================================


class Ticket(BaseModel):
    """An approval ticket for a tool invocation requiring human review."""

    id: str
    job_id: str
    tool_name: str
    status: TicketStatus = TicketStatus.PENDING
    risk_level: str = "medium"   # low / medium / high / critical
    args_preview: str = ""       # truncated preview of tool arguments
    reason: str = ""             # human-provided reason for approve/reject
    requested_at: datetime
    expires_at: datetime | None = None
    resolved_at: datetime | None = None

    @field_validator("status", mode="before")
    @classmethod
    def _reject_invalid_ticket_status(cls, v: Any) -> Any:
        if isinstance(v, str) and v not in {m.value for m in TicketStatus}:
            raise ValueError(f"Invalid TicketStatus: {v!r}")
        return v

    def is_expired(self) -> bool:
        """Return True if the ticket has passed its expiry time."""
        if self.expires_at is None:
            return False
        return datetime.now(timezone.utc) > self.expires_at


class RetryPolicy(BaseModel):
    """Retry configuration for a Job."""

    max_attempts: int = 3
    backoff_sec: int = 5   # exponential backoff base

    @field_validator("max_attempts")
    @classmethod
    def _max_attempts_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("max_attempts must be >= 1")
        return v

    @field_validator("backoff_sec")
    @classmethod
    def _backoff_sec_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("backoff_sec must be >= 1")
        return v


class Job(BaseModel):
    """A unit of work submitted to the Weave control plane."""

    id: str
    requirement: str
    status: JobStatus = JobStatus.QUEUED
    project_path: str | None = None
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    attempt: int = 0
    last_error: str = ""
    error_category: str = ""   # timeout / eval_failed / tool_blocked / unknown
    created_at: datetime
    updated_at: datetime
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("status", mode="before")
    @classmethod
    def _reject_invalid_job_status(cls, v: Any) -> Any:
        if isinstance(v, str) and v not in {m.value for m in JobStatus}:
            raise ValueError(f"Invalid JobStatus: {v!r}")
        return v

    @field_validator("error_category")
    @classmethod
    def _valid_error_category(cls, v: str) -> str:
        allowed = {
            "", "timeout", "eval_failed", "tool_blocked",
            "unknown", "watchdog", "approval_timeout", "rate_limit",
            "coverage_low", "naming_mismatch", "runtime_error",
            "budget_exhausted",
        }
        if v not in allowed:
            raise ValueError(f"Invalid error_category: {v!r}")
        return v

    def bump_attempt(self) -> None:
        """Increment the attempt counter."""
        self.attempt += 1

    def is_terminal(self) -> bool:
        """Return True if the job has reached a terminal state."""
        return self.status in {
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.CANCELED,
            JobStatus.DEAD_LETTER,
        }

    def is_active(self) -> bool:
        """Return True if the job may still be executed."""
        return self.status in {
            JobStatus.QUEUED,
            JobStatus.LEASED,
            JobStatus.RUNNING,
            JobStatus.PENDING_APPROVAL,
        }


class Run(BaseModel):
    """A single execution attempt of a Job, backed by a Session."""

    id: str
    job_id: str
    session_id: str
    status: RunStatus = RunStatus.RUNNING
    dag_result: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime
    completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("status", mode="before")
    @classmethod
    def _reject_invalid_run_status(cls, v: Any) -> Any:
        if isinstance(v, str) and v not in {m.value for m in RunStatus}:
            raise ValueError(f"Invalid RunStatus: {v!r}")
        return v

    def is_terminal(self) -> bool:
        """Return True if the run has finished (for any reason)."""
        return self.status in {
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.ABORTED,
            RunStatus.TIMED_OUT,
        }

    def is_active(self) -> bool:
        """Return True if the run is still in progress (including pending approval)."""
        return self.status in {
            RunStatus.RUNNING,
            RunStatus.PENDING_APPROVAL,
        }
