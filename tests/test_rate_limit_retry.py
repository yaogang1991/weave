"""Tests for #351: rate limit errors don't consume retry budget.

When a 429 rate limit error occurs, the job should be re-queued without
incrementing the attempt counter, preserving the full retry budget for
actual implementation failures.
"""

from __future__ import annotations

from datetime import datetime, timezone

from control_plane.models import Job, JobStatus, RetryPolicy


def _utc_now():
    return datetime.now(timezone.utc)


def _make_job(attempt=1, max_attempts=3):
    """Create a test job."""
    return Job(
        id="test-job",
        requirement="test requirement",
        status=JobStatus.FAILED,
        attempt=attempt,
        retry_policy=RetryPolicy(max_attempts=max_attempts),
        created_at=_utc_now(),
        updated_at=_utc_now(),
    )


def test_classify_error_rate_limit_429():
    from control_plane.service import _classify_error
    assert _classify_error("429 rate limit exceeded") == "rate_limit"


def test_classify_error_rate_limit_anthropic():
    from control_plane.service import _classify_error
    assert _classify_error(
        "anthropic.RateLimitError: rate limit exceeded"
    ) == "rate_limit"


def test_classify_error_rate_limit_phrase():
    from control_plane.service import _classify_error
    assert _classify_error("API rate limit hit") == "rate_limit"


def test_classify_error_non_rate():
    from control_plane.service import _classify_error
    assert _classify_error("timeout exceeded") == "timeout"
    assert _classify_error("unknown error") == "unknown"


def test_rate_limit_does_not_bump_attempt(tmp_path):
    """Re-queuing after rate limit should NOT increment attempt."""
    from control_plane.repository import JobRepository

    repo = JobRepository(base_path=str(tmp_path))
    job = repo.create_job("test requirement")
    # Simulate it's been running and failed
    job.status = JobStatus.FAILED
    job.attempt = 1
    repo._persist_job(job)

    updated = repo.transition_job_status(
        job.id,
        JobStatus.QUEUED,
        error="429 rate limit",
        error_category="rate_limit",
        skip_attempt_bump=True,
    )
    assert updated.attempt == 1  # NOT incremented
    assert updated.status == JobStatus.QUEUED


def test_normal_failure_bumps_attempt(tmp_path):
    """Non-rate-limit failures SHOULD increment attempt."""
    from control_plane.repository import JobRepository

    repo = JobRepository(base_path=str(tmp_path))
    job = repo.create_job("test requirement")
    job.status = JobStatus.FAILED
    job.attempt = 1
    repo._persist_job(job)

    updated = repo.transition_job_status(
        job.id,
        JobStatus.QUEUED,
        error="timeout",
        error_category="timeout",
    )
    assert updated.attempt == 2  # Incremented


def test_rate_limit_preserves_error_info(tmp_path):
    """Rate limit re-queue should keep error info for visibility."""
    from control_plane.repository import JobRepository

    repo = JobRepository(base_path=str(tmp_path))
    job = repo.create_job("test requirement")
    job.status = JobStatus.FAILED
    job.attempt = 1
    repo._persist_job(job)

    updated = repo.transition_job_status(
        job.id,
        JobStatus.QUEUED,
        error="429 - quota resets at 2026-05-15 06:29:04",
        error_category="rate_limit",
        skip_attempt_bump=True,
    )
    assert updated.last_error == "429 - quota resets at 2026-05-15 06:29:04"
    assert updated.error_category == "rate_limit"


def test_error_category_validator_accepts_rate_limit():
    """Job.error_category validator should accept 'rate_limit'."""
    job = Job(
        id="test",
        requirement="test",
        error_category="rate_limit",
        created_at=_utc_now(),
        updated_at=_utc_now(),
    )
    assert job.error_category == "rate_limit"
